# sync_manager.py (处理邮件同步逻辑 - 带清洗)
import re
import time
import traceback


from googleapiclient.errors import HttpError
import database # 导入数据库交互模块
import rules_manager # 导入规则管理模块
# 注意：这里不再直接导入 determine_local_category，因为它在 app.py 中定义并作为参数传递进来
# 或者，如果 determine_local_category 不依赖 Flask app，可以移到这里或 utils.py

# --- 同步常量 ---
SYNC_INTERVAL_SECONDS = 300 # 同步间隔 (例如 5 分钟)
INITIAL_SYNC_MAX_RESULTS = 100 # 首次同步获取的邮件数量上限
REGULAR_SYNC_MAX_RESULTS = 50 # 常规同步获取的邮件数量上限
FETCH_BATCH_SIZE = 15 # 批量获取邮件详情的大小

# --- 从 app.py 导入的 determine_local_category ---
# (或者定义在这里/utils.py，如果它不依赖 app 上下文)
def determine_local_category(gmail_labels, custom_rule_result, model_prediction):
    """
    根据 Gmail 标签、自定义规则和模型预测确定最终的本地分类。
    优先级顺序:
    1. Gmail Spam (Gmail 标记的垃圾邮件)
    2. Custom Rule: Filtered (自定义规则：阻止)
    3. Custom Rule: Normal (自定义规则：允许) <-- 这是关键，允许规则可以覆盖模型判断
    4. Model Spam (模型识别的垃圾邮件)
    5. Unread (未读邮件，如果前面都不是)
    6. Inbox (收件箱，如果前面都不是且已读)
    """
    if 'SPAM' in gmail_labels:
        return 'GmailSpam'
    if custom_rule_result == 'Filtered':  # 自定义规则阻止了它
        return 'CustomBlocked'
    if custom_rule_result == 'Normal':    # 自定义规则明确允许它
        # 即使模型可能认为是垃圾邮件，这条规则也优先
        if 'UNREAD' in gmail_labels:
            return 'Unread'
        return 'Inbox'
    # 如果没有自定义规则决策 (结果为 None) 或者规则决策不是 Filtered/Normal
    if model_prediction == '垃圾邮件':
        return 'ModelSpam'
    # 如果以上都不是垃圾邮件或被阻止
    if 'UNREAD' in gmail_labels:
        return 'Unread'
    return 'Inbox' # 包含已读的、非垃圾/非阻止的邮件

# --- 主同步函数 ---
def sync_gmail_to_db(service, predict_email_func, get_body_func, clean_body_func, initial_sync=False):
    """
    从 Gmail 同步邮件到本地 SQLite 数据库。
    Args:
        service: 已认证的 Gmail API 服务实例。
        predict_email_func: 用于预测邮件类别的函数 (接收 cleaned_body)。
        get_body_func: 用于从邮件 payload 提取原始正文的函数。
        clean_body_func: 用于清洗 HTML/文本正文的函数。 <<< 新增
        initial_sync: 是否为首次同步 (获取更多邮件)。
    """
    sync_start_time = time.time()
    print(f"[Sync Manager] {'首次' if initial_sync else '常规'}同步开始...")
    max_results = INITIAL_SYNC_MAX_RESULTS if initial_sync else REGULAR_SYNC_MAX_RESULTS
    processed_count = 0
    added_count = 0
    updated_count = 0
    failed_count = 0

    try:
        # 1. 获取邮件列表 (仅 ID 和 threadId)
        # 可以考虑使用 q 参数来过滤，例如只获取最近 N 天的邮件 'q': 'newer_than:7d'
        # 或者只获取 INBOX 中的邮件 'labelIds': ['INBOX']
        # 但为了捕获所有邮件（包括可能被误判到 SPAM 的），先获取所有
        list_request = service.users().messages().list(
            userId='me',
            maxResults=max_results,
            # labelIds=['INBOX', 'SPAM'] # 可以指定标签，但可能漏掉其他地方的
            includeSpamTrash=True # 包含垃圾邮件和已删除邮件中的信息（如果需要）
        )
        response = list_request.execute()
        messages = response.get('messages', [])
        print(f"[Sync Manager] 从 Gmail 获取了 {len(messages)} 封邮件元数据。")

        if not messages:
            print("[Sync Manager] 没有找到新的邮件元数据。")
            # 更新最后同步时间戳（即使没有新邮件）
            # database.update_last_sync_time(sync_start_time) # 需要一个专门记录同步时间的表或机制
            return

        # 2. 分批获取邮件详细信息
        message_ids = [msg['id'] for msg in messages]
        rules = rules_manager.load_rules() # 加载当前规则

        for i in range(0, len(message_ids), FETCH_BATCH_SIZE):
            batch_ids = message_ids[i:i + FETCH_BATCH_SIZE]
            batch_request = service.new_batch_http_request(callback=process_batch_response)

            print(f"[Sync Manager] 准备批量获取批次 {i//FETCH_BATCH_SIZE + 1} ({len(batch_ids)} 封邮件) 的详情...")
            for msg_id in batch_ids:
                 # 请求 'full' 格式以获取 payload, headers, labels 等
                 batch_request.add(service.users().messages().get(userId='me', id=msg_id, format='full'))

            batch_results = [] # 用于收集这个批次处理结果的回调数据
            try:
                # 使用全局变量或闭包传递 batch_results, get_body_func, clean_body_func, predict_email_func, rules
                # 这里简化处理，假设 process_message_details 能访问到它们
                # 或者在 process_batch_response 中处理
                global shared_batch_data # 声明一个全局变量来传递数据（不是最佳实践，但用于示例）
                shared_batch_data = {
                    "results": batch_results,
                    "get_body_func": get_body_func,
                    "clean_body_func": clean_body_func,
                    "predict_email_func": predict_email_func,
                    "rules": rules
                }
                batch_request.execute()
            except HttpError as batch_err:
                 print(f"[Sync Manager] 批量获取邮件详情时出错: {batch_err}")
                 # 即使批处理失败，也可能部分成功，检查 batch_results
            except Exception as general_batch_err:
                 print(f"[Sync Manager] 批量处理中发生意外错误: {general_batch_err}")
                 traceback.print_exc()


            # --- 处理从回调函数收集的结果 ---
            batch_processed_count = 0
            for result in batch_results: # batch_results 由 process_batch_response 填充
                if result['success']:
                    email_details = result['data']
                    message_id = email_details['message_id']
                    # 检查数据库中是否已存在以及 last_synced 时间
                    # local_last_sync = database.get_email_last_sync(message_id)
                    # # 如果本地记录的同步时间更新，则跳过（避免重复处理）
                    # if local_last_sync >= email_details['last_synced']:
                    #     # print(f"[Sync Manager] 邮件 {message_id} 本地已是最新，跳过。")
                    #     processed_count += 1
                    #     continue

                    # 添加或更新数据库
                    if database.add_or_update_email(email_details):
                        # 这里可以根据 add_or_update_email 的返回值判断是新增还是更新
                        # 但 INSERT OR REPLACE 使得区分困难，简单计数处理量
                        # 更好的方法是先查询是否存在
                        # exists = database.is_message_in_db(message_id)
                        # if database.add_or_update_email(email_details):
                        #    if exists: updated_count +=1
                        #    else: added_count += 1
                        #    processed_count += 1
                        # else: failed_count +=1
                        # --- 简化计数 ---
                        processed_count += 1 # 假设 add_or_update 总是尝试处理
                        # 无法精确区分 add/update，统一记录为 processed

                    else:
                        failed_count += 1
                else:
                    print(f"[Sync Manager] 处理邮件 {result.get('id', 'N/A')} 失败: {result.get('error', '未知错误')}")
                    failed_count += 1
                batch_processed_count += 1

            print(f"[Sync Manager] 批次 {i//FETCH_BATCH_SIZE + 1} 处理完成。成功处理: {batch_processed_count - failed_count}, 失败: {failed_count}") # 这里计数可能不准

    except HttpError as error:
        print(f'[Sync Manager] Gmail API 请求错误: {error}')
        traceback.print_exc()
        # 如果是认证错误，可能需要停止后台任务
        if error.resp.status in [401, 403]:
             print("[Sync Manager] !!! 认证/授权错误，请检查 token.json 或重新授权。")
             # 这里可以触发停止事件，让 app.py 中的线程退出
             # raise error # 重新抛出，让 run_background_sync 捕获并停止
    except Exception as e:
        print(f"[Sync Manager] 同步过程中发生未知错误: {e}")
        traceback.print_exc()

    sync_duration = time.time() - sync_start_time
    # 这里的 added/updated 计数不准确，因为 INSERT OR REPLACE
    print(f"[Sync Manager] 同步结束。总耗时: {sync_duration:.2f} 秒。处理邮件数: {processed_count} (近似), 失败数: {failed_count}。")
    # database.update_last_sync_time(time.time()) # 更新全局同步时间

# --- 全局变量用于回调传递数据 (不推荐，仅为示例) ---
shared_batch_data = {}

# --- 批量请求的回调函数 ---
def process_batch_response(request_id, response, exception):
    """处理批量 API 请求中每个单独响应的回调函数。"""
    # 从全局变量获取需要的函数和数据 (不推荐的方式)
    global shared_batch_data
    results_list = shared_batch_data.get("results", [])
    get_body_func = shared_batch_data.get("get_body_func")
    clean_body_func = shared_batch_data.get("clean_body_func")
    predict_email_func = shared_batch_data.get("predict_email_func")
    rules = shared_batch_data.get("rules")

    message_id = "N/A" # 默认值
    # 从请求 ID 或其他方式尝试获取原始 message_id (这比较困难，批量API的回调通常不直接提供)
    # 更好的方式是在请求时附加元数据，但这需要更复杂的库或手动构建请求
    # 这里我们只能从响应中获取 message_id

    if exception:
        # 处理 API 调用错误
        print(f"[Batch Callback] 请求 {request_id} 失败: {exception}")
        # 尝试从异常中提取信息，如果可能的话
        error_details = str(exception)
        # 无法可靠获取 message_id 时，记录通用错误
        results_list.append({'id': message_id, 'success': False, 'error': error_details})
    else:
        # 处理成功的响应
        message_id = response.get('id')
        if not message_id:
             print(f"[Batch Callback] 请求 {request_id} 成功但响应中缺少 message ID。")
             results_list.append({'id': 'N/A', 'success': False, 'error': '响应缺少 message ID'})
             return

        # print(f"[Batch Callback] 处理邮件 ID: {message_id}") # 日志可能过于频繁
        try:
            # --- 在这里处理单个邮件的逻辑 ---
            email_details = process_message_details(response, get_body_func, clean_body_func, predict_email_func, rules)
            if email_details:
                results_list.append({'id': message_id, 'success': True, 'data': email_details})
            else:
                # process_message_details 内部应打印错误
                results_list.append({'id': message_id, 'success': False, 'error': '处理邮件详情失败 (详见内部日志)'})

        except Exception as e:
            print(f"[Batch Callback] 处理邮件 {message_id} 时发生内部错误: {e}")
            traceback.print_exc()
            results_list.append({'id': message_id, 'success': False, 'error': f'内部处理错误: {e}'})


def process_message_details(message, get_body_func, clean_body_func, predict_email_func, rules):
    """
    处理从 Gmail API 获取的单个邮件详细信息。
    提取所需字段，进行清洗、规则匹配和模型预测。
    """
    try:
        message_id = message.get('id')
        thread_id = message.get('threadId')
        payload = message.get('payload', {})
        headers = payload.get('headers', [])
        labels = message.get('labelIds', [])
        snippet = message.get('snippet', '')
        # Gmail 的 internalDate 是毫秒级时间戳字符串
        internal_date_ms = message.get('internalDate')
        date_received = int(internal_date_ms) // 1000 if internal_date_ms else int(time.time()) # 转秒级时间戳

        subject = ''
        sender = ''
        recipient = '' # 主要收件人 (To)

        for header in headers:
            name = header.get('name', '').lower()
            if name == 'subject':
                subject = header.get('value', '')
            elif name == 'from':
                sender = header.get('value', '')
                # 提取邮箱地址
                match =re.search(r'[\w\.-]+@[\w\.-]+', sender)
                if match: sender = match.group(0)
            elif name == 'to':
                 # "To" 可能包含多个地址，取第一个作为代表或全部存储
                 recipient = header.get('value', '').split(',')[0].strip() # 取第一个


        # 1. 获取原始邮件正文 (优先 HTML)
        original_body = get_body_func(payload) if get_body_func else "[无法获取正文]"

        # 2. 清洗邮件正文以获取纯文本
        cleaned_body = clean_body_func(original_body) if clean_body_func else ""

        # 3. 应用自定义规则 (使用原始数据的小写版本进行匹配)
        apply_rules_data = {
            'from': sender.lower(),
            'subject': subject.lower(),
            'body': original_body.lower() # 规则匹配仍在原始 body 上进行
        }
        rule_match = rules_manager.apply_rules(apply_rules_data, rules)
        rule_result, rule_reason = rule_match if rule_match else (None, None)

        # 4. 如果规则未过滤，则进行模型预测 (使用清洗后的文本)
        model_pred = None
        model_prob_n = None
        model_prob_s = None
        if rule_result != 'Filtered':
             # 准备模型输入：主题 + 清洗后的正文
             text_for_model = f"{subject}\n\n{cleaned_body}"
             if text_for_model.strip() and predict_email_func:
                 pred_label, probs = predict_email_func(text_for_model) # 调用预测函数 (内部会翻译)
                 if "预测出错" not in pred_label and "无效文本" not in pred_label:
                     model_pred = pred_label
                     model_prob_n = probs[0] if probs and len(probs) > 0 else None
                     model_prob_s = probs[1] if probs and len(probs) > 1 else None
             # else: print(f"[Sync Manager] 邮件 {message_id} 无有效文本或无预测函数，跳过模型预测。")
        # else: print(f"[Sync Manager] 邮件 {message_id} 被规则 '{rule_reason}' 过滤，跳过模型预测。")


        # 5. 确定最终本地分类
        gmail_label_set = set(labels) # 转换为集合以便快速查找
        is_unread = 'UNREAD' in gmail_label_set
        # 调用分类决策函数 (确保它在此处可用)
        local_cat = determine_local_category(gmail_label_set, rule_result, model_pred)

        # 6. 准备要存入数据库的数据字典
        email_details = {
            'message_id': message_id,
            'thread_id': thread_id,
            'subject': subject,
            'sender': sender,
            'recipient': recipient,
            'date_received': date_received,
            'snippet': snippet,
            'body': original_body, # 存储原始正文
            'body_cleaned': cleaned_body, # 存储清洗后的正文
            'labels': ",".join(labels), # 将标签列表转为逗号分隔的字符串
            'is_unread': 1 if is_unread else 0,
            'custom_rule_result': rule_result,
            'custom_rule_reason': rule_reason,
            'model_prediction': model_pred,
            'model_prob_spam': model_prob_s,
            'model_prob_normal': model_prob_n,
            'local_category': local_cat,
            'last_synced': int(time.time()) # 使用当前时间作为同步时间戳
        }
        return email_details

    except Exception as e:
        print(f"[Sync Manager] 处理邮件 {message.get('id', 'N/A')} 详情时出错: {e}")
        traceback.print_exc()
        return None