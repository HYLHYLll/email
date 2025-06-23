# app.py (使用 SQLite 缓存和后台同步 - 最终版)
from googletrans import Translator # 导入 googletrans 库
from flask import Flask, render_template, request, redirect, url_for, flash
import os
import torch
from transformers import BertTokenizer, BertForSequenceClassification
import base64
import re
import time
import threading
import sqlite3
import traceback
import time
from bs4 import BeautifulSoup # 导入 BeautifulSoup


# 导入你的自定义模块
import gmail_client
import rules_manager
import database
import sync_manager
from sync_manager import determine_local_category # 导入分类判断函数
# --- 全局初始化翻译器 ---
translator = None # 先设置为 None
try:
    # 尝试初始化 googletrans (或其他库)
    translator = Translator()
    # 尝试一个简单的检测来验证服务是否可用 (可选但推荐)
    _ = translator.detect("test")
    print("[Translator] 翻译服务初始化成功。")
except Exception as e:
    print(f"[Translator Error] 初始化翻译服务失败: {e}")
    print("[Translator Warning] 翻译功能将不可用，模型将使用原始文本。")
    translator = None # 确认在失败时设为 None
# --- /全局初始化翻译器 ---
# --- 模型加载 ---
print("正在加载模型...")
# 全局变量存储模型和 tokenizer
model = None
tokenizer = None
device = None
try:
    MODEL_PATH = "./bert_model/final_model"
    # 确保使用你训练时或模型兼容的 Tokenizer 名称
    TOKENIZER_NAME = "bert-base-uncased"

    if not os.path.isdir(MODEL_PATH):
        raise FileNotFoundError(f"模型路径 '{MODEL_PATH}' 不存在。请确保模型文件在此路径下。")

    tokenizer = BertTokenizer.from_pretrained(TOKENIZER_NAME)
    model = BertForSequenceClassification.from_pretrained(MODEL_PATH)
    model.eval() # 设置为评估模式

    # --- 设备选择 ---
    if torch.cuda.is_available():
        device = torch.device("cuda")
        print("检测到 CUDA，模型将使用 GPU。")
    # 检查 MPS (Apple Silicon GPU) 可用性
    elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        # 某些 PyTorch 版本可能还需要额外配置才能在 MPS 上稳定运行
        try:
            device = torch.device("mps")
            # 尝试一个简单的操作来验证 MPS 是否工作
            _ = torch.tensor([1.0, 2.0]).to(device)
            print("检测到 MPS，模型将使用 Apple Silicon GPU。")
        except Exception as mps_err:
            print(f"初始化 MPS 时出错 ({mps_err})，回退到 CPU。")
            device = torch.device("cpu")
    else:
        device = torch.device("cpu")
        print("未检测到兼容的 GPU，模型将使用 CPU。")

    model.to(device)
    print("模型加载完成并移动到设备:", device)

except FileNotFoundError as fnf_error:
    print(f"错误: {fnf_error}")
    print("无法启动应用。")
    exit()
except Exception as e:
    print(f"加载模型时发生未知错误: {e}")
    print("无法启动应用。")
    exit()
# --- /模型加载 ---

# --- 模型预测函数 (修改为接收清洗后文本，并在内部翻译) ---
def predict_email_local(cleaned_text: str):
    """
    使用加载好的本地模型进行预测。
    接收清洗后的文本，进行翻译（如果需要），然后预测。
    """
    global model, tokenizer, device
    if not model or not tokenizer:
        print("错误：模型或 Tokenizer 未初始化！")
        return "预测出错 (模型未加载)", [0.0, 0.0]
    if not cleaned_text or not isinstance(cleaned_text, str):
        return "无法判断 (无效文本)", [0.5, 0.5]

    try:
        # 1. 翻译文本为英文 (如果模型是英文模型)
        text_to_predict = translate_to_english(cleaned_text)
        if not text_to_predict: # 如果翻译结果为空
             return "无法判断 (翻译后为空)", [0.5, 0.5]

        # 2. 使用翻译后的英文文本进行预测
        inputs = tokenizer(text_to_predict, return_tensors="pt", truncation=True, padding=True, max_length=512)
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model(**inputs)
            logits = outputs.logits
            probabilities = torch.softmax(logits, dim=1)[0]
            predicted_class_id = torch.argmax(probabilities).item()
            probs_list = probabilities.cpu().tolist()

        if predicted_class_id == 0:
            return "正常邮件", probs_list
        else:
            return "垃圾邮件", probs_list

    except Exception as e:
        print(f"模型预测时出错: {e}")
        traceback.print_exc()
        return "预测出错", [0.0, 0.0]
# --- /模型预测函数 ---

# --- HTML 清洗函数 ---
def clean_html_body(html_content: str) -> str:
    """使用 BeautifulSoup 清洗 HTML，提取纯文本。"""
    if not html_content or not isinstance(html_content, str):
        return "" # 返回空字符串而不是 None
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        # 移除脚本和样式标签
        for script_or_style in soup(['script', 'style']):
            script_or_style.decompose()
        # 获取文本，用空格分隔，并去除多余空白
        text = soup.get_text(separator=' ', strip=True)
        # 进一步清理，移除连续的多个空格或换行符
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except Exception as e:
        print(f"清洗 HTML 时出错: {e}")
        # 在出错时返回原始输入的子集或特定错误标记可能比返回空字符串更好
        # return "[HTML 清洗失败]"
        # 这里我们选择尽可能返回一些文本
        if isinstance(html_content, str):
             # 尝试一个非常基础的标签移除，以防 BS4 失败
             text_fallback = re.sub('<[^>]*>', ' ', html_content)
             text_fallback = re.sub(r'\s+', ' ', text_fallback).strip()
             return text_fallback[:1000] # 限制长度以防万一
        return "[HTML 清洗失败]"
# --- /HTML 清洗函数 ---

# --- 翻译函数占位符 ---
# translator = Translator() # 示例：初始化 googletrans

def translate_to_english(text: str) -> str:
    """
    将文本翻译成英文。
    """
    global translator # 明确使用全局变量
    # 检查文本是否为空，或者翻译器是否初始化失败
    if not text or not translator:
        if not translator and text: # 仅在翻译器失败时打印警告
             print("[Translator Warning] 翻译器未初始化，跳过翻译。")
        return text # 返回原始文本

    # --- 实现翻译逻辑 (使用全局 translator) ---
    try:
        lang_detection = translator.detect(text[:500])
        if lang_detection.lang != 'en':
            # print(f"检测到语言: {lang_detection.lang}, 正在翻译为英文...") # 日志可能过于频繁
            translated = translator.translate(text, src=lang_detection.lang, dest='en')
            # print("翻译完成。")
            return translated.text
        else:
            return text # 已经是英文
    except Exception as e:
        print(f"翻译文本时出错: {e}")
        return text # 翻译失败时返回原始文本
    # --- /翻译逻辑 --

# --- /翻译函数占位符 ---


# --- 辅助函数：从 payload 解码邮件正文 (供 sync_manager 调用) ---
def get_body_from_payload(payload):
    """从 payload 中提取并解码邮件正文 (递归处理)。"""
    body = ""
    if not payload: return "[Payload 为空]"

    mimeType = payload.get('mimeType', '')
    part_body_dict = payload.get('body', {})
    data = part_body_dict.get('data')

    # 优先 text/html
    if mimeType == 'text/html' and data:
        try:
            decoded_bytes = base64.urlsafe_b64decode(data.encode('ASCII'))
            body = decoded_bytes.decode('utf-8', errors='replace')
            if body: return body # 找到 HTML 直接返回
        except Exception as e:
            print(f"解码 HTML body 时出错: {e}")

    # 其次 text/plain
    if mimeType == 'text/plain' and data:
        try:
            decoded_bytes = base64.urlsafe_b64decode(data.encode('ASCII'))
            plain_body = decoded_bytes.decode('utf-8', errors='replace')
            if plain_body and not body: # 只有在还没找到 body 时才用 plain text
                body = plain_body
        except Exception as e:
            print(f"解码 Plain text body 时出错: {e}")

    # 如果有 parts，递归查找
    if 'parts' in payload:
        html_body_found_in_parts = ""
        plain_body_found_in_parts = ""
        for part in payload['parts']:
            nested_body = get_body_from_payload(part) # Recursive call
            nested_mime = part.get('mimeType', '').lower()
            if nested_body and nested_body != "[无可见文本内容]" and nested_body != "[Payload 为空]":
                 if 'text/html' in nested_mime:
                      html_body_found_in_parts = nested_body
                      break # 找到 HTML 优先返回
                 elif 'text/plain' in nested_mime and not plain_body_found_in_parts:
                      plain_body_found_in_parts = nested_body

        # 优先返回 parts 中的 HTML，其次是 Plain Text
        if html_body_found_in_parts: return html_body_found_in_parts
        if plain_body_found_in_parts and not body: body = plain_body_found_in_parts


    # 回退检查主 body（如果没有 parts 或 parts 中没找到）
    if not body and data and ('text/plain' in mimeType or 'text/html' in mimeType):
         try:
             decoded_bytes = base64.urlsafe_b64decode(data.encode('ASCII'))
             body = decoded_bytes.decode('utf-8', errors='replace')
         except Exception as e:
             print(f"解码回退 body 时出错: {e}")
             body = "[无法解码的邮件内容]"

    return body if body else "[无可见文本内容]"
# --- /邮件正文解码 ---


# --- Flask 应用设置 ---
app = Flask(__name__)
app.secret_key = os.urandom(24) # 用于 flash 消息
# 设置邮件显示数量上限 (可以在 config 文件中设置)
MAX_EMAILS_PER_PAGE = 25
# --- /Flask 应用设置 ---

# --- 后台同步任务 ---
background_sync_thread = None
stop_sync_event = threading.Event() # 用于优雅地停止线程

def run_background_sync():
    """后台线程运行的函数，定期执行同步"""
    global stop_sync_event
    print("[Background Sync] 后台同步线程启动...")
    is_first_run_check = True # 标记是否是首次运行检查

    while not stop_sync_event.is_set():
        current_run_start_time = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] [Background Sync] 准备执行同步...")
        service = None
        force_initial_sync = False # 是否需要强制执行 initial_sync

        # 仅在首次检查时判断数据库是否为空
        if is_first_run_check:
             try:
                 conn = database.get_db_connection()
                 cursor = conn.cursor()
                 cursor.execute("SELECT COUNT(*) FROM emails")
                 count = cursor.fetchone()[0]
                 conn.close()
                 if count == 0:
                     print("[Background Sync] 检测到数据库为空，将执行首次同步。")
                     force_initial_sync = True
                 else:
                     print(f"[Background Sync] 数据库中已有 {count} 条记录。")
                 is_first_run_check = False # 不再进行首次运行检查
             except Exception as db_check_err:
                 print(f"[Background Sync] 检查数据库时出错: {db_check_err}。假设需要首次同步。")
                 force_initial_sync = True
                 is_first_run_check = False # 出错也标记为已检查

        # 获取 Gmail 服务
        try:
            service = gmail_client.get_gmail_service()
            if service:
                 # 决定本次运行是否是 initial_sync
                 run_as_initial = force_initial_sync
                 sync_type_log = "[Initial Sync]" if run_as_initial else "[Regular Sync]"
                 print(f"[{time.strftime('%H:%M:%S')}] [Background Sync] 执行 {sync_type_log}...")

                 # 执行同步，将依赖的函数传递进去
                 sync_manager.sync_gmail_to_db(
                     service,
                     predict_email_local,  # 预测函数
                     get_body_from_payload,  # 原始正文提取函数
                     clean_html_body,  # <<< 添加清洗函数参数
                     initial_sync=run_as_initial
                 )
                 print(f"[{time.strftime('%H:%M:%S')}] [Background Sync] {sync_type_log} 完成。")

            else:
                 print(f"[{time.strftime('%H:%M:%S')}] [Background Sync] 无法获取 Gmail 服务，跳过本次同步。")

        except gmail_client.HttpError as auth_err: # 特别捕获认证错误
             print(f"[{time.strftime('%H:%M:%S')}] [Background Sync] 同步时发生 HttpError: {auth_err}")
             if hasattr(auth_err, 'resp') and auth_err.resp.status in [401, 403]:
                 print(f"[{time.strftime('%H:%M:%S')}] [Background Sync] !!!! 认证/授权错误 ({auth_err.resp.status})，后台同步线程停止 !!!!")
                 stop_sync_event.set() # 停止后台线程
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] [Background Sync] 同步过程中发生未知错误: {e}")
            import traceback
            traceback.print_exc()

        # 等待指定间隔或直到停止事件被设置
        wait_time = sync_manager.SYNC_INTERVAL_SECONDS
        current_run_duration = time.time() - current_run_start_time
        # 确保等待时间非负数
        actual_wait = max(0, wait_time - current_run_duration)
        print(f"[{time.strftime('%H:%M:%S')}] [Background Sync] 本次运行耗时 {current_run_duration:.2f} 秒。下次同步将在约 {actual_wait:.1f} 秒后进行...")
        # 使用 wait 方法，它会在超时或事件被设置时返回
        stop_sync_event.wait(actual_wait)

    print("[Background Sync] 后台同步线程已收到停止信号并退出。")
# --- /后台同步任务 ---

# --- Flask 路由 ---

# == 页面一：设置与准确性测试 ==
@app.route('/', methods=['GET', 'POST']) # 允许 POST 用于手动同步
def settings_page():
    global background_sync_thread, stop_sync_event

    if request.method == 'POST':
        if 'sync_now' in request.form:
             print("[Manual Sync] 用户请求立即执行后台同步...")
             service = gmail_client.get_gmail_service()
             if service:
                 try:
                     # 手动同步总是执行常规同步 (initial_sync=False)
                     print("[Manual Sync] 启动手动同步线程...")
                     manual_sync_thread = threading.Thread(
                         target=sync_manager.sync_gmail_to_db,
                         args=(
                             service,
                             predict_email_local,
                             get_body_from_payload,
                             clean_html_body,  # <<< 添加清洗函数参数
                             False  # initial_sync=False
                         ),
                         daemon=True) # 使用 daemon 线程
                     manual_sync_thread.start()
                     flash("已触发后台立即同步邮件 (请稍后查看日志或刷新页面)。", "info")
                 except Exception as e:
                     flash(f"触发同步时出错: {e}", "error")
                     print(f"[Manual Sync] 触发同步时出错: {e}")
             else:
                 flash("无法连接到 Gmail，无法触发同步。", "error")
             # 避免表单重复提交，重定向回 GET 请求
             return redirect(url_for('settings_page'))

    # --- 处理 GET 请求 ---
    rules = rules_manager.load_rules()

    # 检查后台线程状态
    sync_status = "未知"
    if background_sync_thread:
         # 检查线程是否存活
         if background_sync_thread.is_alive():
             sync_status = "正在运行"
         else:
              # 检查是否是正常停止
              if stop_sync_event.is_set():
                  sync_status = "已停止 (需要重启应用)"
              else:
                   sync_status = "已停止 (异常? 检查日志)" # 线程不在但事件未设置
    else:
        sync_status = "未启动 (应用可能正在启动)"

    # 获取数据库中的邮件总数
    db_count = "查询中..." # 初始值
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM emails")
        result = cursor.fetchone()
        if result:
            db_count = result[0]
        else:
            db_count = 0 # 如果表为空或查询失败
        conn.close()
    except Exception as e:
        print(f"查询邮件总数时出错: {e}")
        db_count = "查询错误" # 在页面上显示错误

    # 从 URL 参数获取准确性测试结果（如果有的话）
    accuracy_result = request.args.get('accuracy_result')

    return render_template('page1_settings.html',
                           rules=rules,
                           accuracy_result=accuracy_result,
                           sync_status=sync_status,
                           db_email_count=db_count)

# --- 测试准确性路由 (现在使用清洗+翻译逻辑) ---
@app.route('/test_accuracy', methods=['POST'])
def test_accuracy():
    """处理准确性测试请求 (使用清洗+翻译逻辑)"""
    text_to_test_raw = request.form.get('text_to_test') # 获取原始输入
    result_message = "请输入要测试的文本。"
    if text_to_test_raw:
        # 1. 清洗输入文本 (假设测试输入也可能是 HTML 或需要清理)
        cleaned_text = clean_html_body(text_to_test_raw)
        # 2. 使用 predict_email_local (它内部会处理翻译)
        label, probs = predict_email_local(cleaned_text) # 传递清洗后的文本
        result_message = f"预测结果: {label}"
        if probs:
            result_message += f" (P(正常)={probs[0]:.2f}, P(垃圾)={probs[1]:.2f})"
        else:
             result_message += " (无法获取概率)"
    else:
        flash("请输入文本进行测试。", "warning")
        return redirect(url_for('settings_page'))

    return redirect(url_for('settings_page', accuracy_result=result_message))
# --- 更新规则路由 (修改重新评估逻辑) ---
@app.route('/update_rules', methods=['POST'])
def update_rules():
    """处理添加或删除规则的请求，并在规则变化可能影响分类时重新评估邮件"""
    rules = rules_manager.load_rules()
    action = request.form.get('action')
    rule_type = request.form.get('rule_type') # 例如 'allowedSenders', 'blockedKeywords'
    value = request.form.get('value')

    if not all([action, rule_type, value]):
         flash("缺少更新规则所需的数据。", "error")
         return redirect(url_for('settings_page'))

    value = value.strip().lower()
    if rule_type not in rules: rules[rule_type] = []
    if not isinstance(rules.get(rule_type), list):
        flash(f"规则列表 '{rule_type}' 格式错误。", "error")
        return redirect(url_for('settings_page'))

    rule_list = rules[rule_type]
    original_rule_list_copy = list(rule_list) # 创建副本用于比较是否真的添加了新规则
    rule_changed_effectively = False # 标记规则是否真的发生了有效变化（新增或删除）

    needs_re_evaluation = False
    # 更明确的类型: 'block_rule_added', 'block_rule_removed', 'allow_rule_added', 'allow_rule_removed'
    re_evaluation_fetch_type = None

    try:
        if action == 'add':
            if value not in original_rule_list_copy: # 检查是否是真正的新增
                rule_list.append(value) # 添加到当前操作的列表
                flash(f"已添加 '{value}' 到 {rule_type}。", "success")
                rule_changed_effectively = True
                if rule_type in ['allowedSenders', 'allowedKeywords']:
                    needs_re_evaluation = True
                    re_evaluation_fetch_type = 'allow_rule_added'
                elif rule_type in ['blockedSenders', 'blockedKeywords']:
                    needs_re_evaluation = True
                    re_evaluation_fetch_type = 'block_rule_added' # 新增的类型
            else:
                flash(f"'{value}' 已存在于 {rule_type}。", "warning")
                # rule_changed_effectively 保持 False

        elif action == 'remove':
             if value in original_rule_list_copy: # 检查是否真的能被移除
                 if value in rule_list: # 确保它仍在当前操作的列表中（理论上应该在）
                    rule_list.remove(value)
                 flash(f"已从 {rule_type} 移除 '{value}'。", "success")
                 rule_changed_effectively = True
                 if rule_type in ['blockedSenders', 'blockedKeywords']:
                     needs_re_evaluation = True
                     re_evaluation_fetch_type = 'block_rule_removed' # 原来的类型名修改一下更清晰
                 elif rule_type in ['allowedSenders', 'allowedKeywords']:
                     needs_re_evaluation = True
                     re_evaluation_fetch_type = 'allow_rule_removed'
             else:
                 flash(f"在 {rule_type} 中未找到 '{value}' 以移除。", "warning")
                 # rule_changed_effectively 保持 False
        else:
            flash("无效的操作。", "error")

        # 只有当规则列表真的发生变化时才保存并进行后续操作
        if rule_changed_effectively:
            if rules_manager.save_rules(rules): # rules 变量已经被修改
                if needs_re_evaluation:
                    print(f"[Re-evaluation] 检测到规则有效变更 (类型: {re_evaluation_fetch_type}), 开始重新评估...")

                    emails_to_reclassify = []
                    if re_evaluation_fetch_type == 'block_rule_added':
                        print("[Re-evaluation] 获取非 CustomBlocked 邮件进行重新评估 (因阻止规则添加)。")
                        emails_to_reclassify = database.get_emails_not_yet_custom_blocked()
                    elif re_evaluation_fetch_type == 'block_rule_removed': # 原來的 custom_blocked
                        print("[Re-evaluation] 获取 'CustomBlocked' 邮件进行重新评估 (因阻止规则移除)。")
                        emails_to_reclassify = database.get_custom_blocked_emails_data()
                    elif re_evaluation_fetch_type == 'allow_rule_added':
                        print("[Re-evaluation] 获取 'ModelSpam', 'GmailSpam' 邮件进行重新评估 (因允许规则添加)。")
                        emails_to_reclassify = database.get_emails_for_reclassification_by_categories(['ModelSpam', 'GmailSpam'])
                    elif re_evaluation_fetch_type == 'allow_rule_removed':
                        print("[Re-evaluation] 获取可能受允许规则移除影响的邮件 ('Inbox', 'Unread' 且 custom_rule_result='Normal')。")
                        emails_to_reclassify = database.get_emails_potentially_affected_by_allow_rule_removal()

                    if emails_to_reclassify:
                        print(f"[Re-evaluation] 找到 {len(emails_to_reclassify)} 封邮件需要重新评估。")
                        updated_count = 0
                        failed_count = 0
                        current_rules = rules_manager.load_rules() # 加载最新的规则

                        for email_data in emails_to_reclassify:
                            message_id = email_data['message_id']
                            apply_rules_data = {
                                'from': email_data.get('sender', '').lower(),
                                'subject': email_data.get('subject', '').lower(),
                                'body': email_data.get('body', '').lower()
                            }
                            labels_str = email_data.get('labels', '')
                            gmail_labels = set(labels_str.split(',')) if labels_str else set()

                            rule_match = rules_manager.apply_rules(apply_rules_data, current_rules)
                            new_rule_result, new_rule_reason = rule_match if rule_match else (None, None)

                            model_pred, model_prob_n, model_prob_s = None, None, None
                            if new_rule_result != 'Filtered':
                                text_for_model = f"{email_data.get('subject', '')}\n\n{email_data.get('body_cleaned', '')}"
                                if text_for_model.strip():
                                    pred_label, probs = predict_email_local(text_for_model)
                                    if "预测出错" not in pred_label and "无效文本" not in pred_label:
                                        model_pred = pred_label
                                        model_prob_n = probs[0] if probs and len(probs) > 0 else None
                                        model_prob_s = probs[1] if probs and len(probs) > 1 else None

                            new_local_cat = determine_local_category(gmail_labels, new_rule_result, model_pred)

                            # 仅当新的本地分类与邮件数据中记录的（如果有的话，这里没直接获取旧分类）不同，
                            # 或者规则结果/模型结果有变化时，才真正执行更新。
                            # 但为了简化，这里直接尝试更新，数据库的 update_email_classification
                            # 如果数据没变，rowcount 可能是0，但不会出错。
                            # 或者更精确地，可以先获取旧的 local_category, rule_result 等进行比较。
                            # 目前的逻辑是：只要进入这个循环，就认为需要更新相关字段。

                            update_data = {
                                'local_category': new_local_cat,
                                'custom_rule_result': new_rule_result,
                                'custom_rule_reason': new_rule_reason,
                                'model_prediction': model_pred,
                                'model_prob_spam': model_prob_s,
                                'model_prob_normal': model_prob_n,
                                'last_synced': int(time.time())
                            }

                            if database.update_email_classification(message_id, update_data):
                                updated_count += 1
                            else:
                                failed_count += 1
                                print(f"[Re-evaluation Error] 更新邮件 {message_id} 失败。")

                        flash_message_base = ""
                        if action == 'add': flash_message_base = "规则已添加。"
                        elif action == 'remove': flash_message_base = "规则已移除。"

                        if len(emails_to_reclassify) > 0:
                             flash_message = f"{flash_message_base} 重新评估了 {len(emails_to_reclassify)} 封相关邮件，成功更新 {updated_count} 封。"
                             if failed_count > 0:
                                 flash_message += f" {failed_count} 封更新失败。"
                             flash(flash_message, "info" if failed_count == 0 else "warning")
                        print(f"[Re-evaluation] 完成。评估: {len(emails_to_reclassify)}, 成功更新: {updated_count}, 失败: {failed_count}")
                    else: # 没有找到邮件进行重新评估
                        flash_message_base = ""
                        if action == 'add': flash_message_base = "规则已添加。"
                        elif action == 'remove': flash_message_base = "规则已移除。"
                        flash(f"{flash_message_base} 没有找到受此规则变更影响的邮件进行重新评估。", "info")
                else: # 规则有效更改，但不需要重新评估（例如，值已存在被阻止了）
                    # 这个分支应该不会进入，因为 needs_re_evaluation 和 rule_changed_effectively 会同步
                    pass
            else: # 保存规则失败
                 flash("保存规则时出错。", "error")
        # else: 规则没有有效变化（例如尝试添加已存在的，或移除不存在的），不执行保存和重新评估

    except Exception as e:
        flash(f"更新规则时出错: {e}", "error")
        print(f"[Error] 更新规则时发生错误: Action={action}, Type={rule_type}, Value={value}, Error={e}")
        traceback.print_exc()

    return redirect(url_for('settings_page'))

# == 页面二：邮件列表与查看 (从数据库读取) ==
@app.route('/emails', defaults={'category': 'inbox'})
@app.route('/emails/<category>')
def emails_page(category):
    """渲染页面二：从本地数据库获取并显示邮件列表。"""
    print(f"[{time.strftime('%H:%M:%S')}] 请求邮件列表，分类: '{category}' (从数据库)")
    emails = []
    error_message = None
    category_counts = {} # 初始化为空字典

    # 页面标题映射 (保持不变)
    page_title_map = {
        'inbox': "收件箱 (所有非过滤邮件)",
        'read': "已读邮件 (本地缓存)",
        'unread': "未读邮件 (本地缓存)",
        'spam': "垃圾邮件 (Gmail 标记, 本地缓存)",
        'custom': "自定义规则屏蔽 (本地缓存)",
        'model_spam': "模型识别垃圾邮件 (本地缓存)"
    }
    page_title = page_title_map.get(category, "邮件列表 (本地缓存)")

    try:
        # 1. 获取当前分类的邮件列表
        start_query = time.time()
        emails = database.get_emails_by_category(category, limit=MAX_EMAILS_PER_PAGE)
        end_query = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] 数据库查询邮件列表耗时: {end_query - start_query:.4f} 秒，获取 {len(emails)} 封邮件。")

        # 2. 获取所有分类的邮件计数
        start_count_query = time.time()
        category_counts = database.get_category_counts() # 调用新函数
        end_count_query = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] 数据库查询分类计数耗时: {end_count_query - start_count_query:.4f} 秒。")
        print(f"[Debug] Category Counts: {category_counts}") # 打印计数结果用于调试

        # ... (Flash 消息逻辑保持不变)
        last_sync = database.get_last_sync_timestamp()
        # ... (Flash 消息逻辑保持不变)

    except Exception as e:
        error_message = f"查询本地数据库时出错: {e}"
        print(f"[{time.strftime('%H:%M:%S')}] 查询本地数据库时出错: {e}")
        import traceback
        traceback.print_exc()
        # 如果出错，确保 category_counts 至少是一个空字典或默认值
        if not category_counts:
             category_counts = {cat: 0 for cat in page_title_map.keys()}


    return render_template('page2_emails.html',
                           emails=emails,
                           current_category=category,
                           page_title=page_title,
                           error_message=error_message,
                           category_counts=category_counts) # 将计数传递给模板

# == 查看单封邮件 (从数据库读取) ==
@app.route('/view_email/<message_id>')
def view_email(message_id):
    """
    从本地数据库获取并显示单封邮件的详情。
    如果邮件是未读的，则在显示后尝试将其在本地数据库和 Gmail 上都标记为已读。
    """
    print(f"[{time.strftime('%H:%M:%S')}] 请求查看邮件详情 (ID: {message_id}) (从数据库)")
    email_content = None # 初始化为 None
    try:
        start_query = time.time()
        # 1. 从数据库获取邮件详情
        email_content = database.get_email_by_id(message_id)
        end_query = time.time()

        # 检查是否成功获取邮件内容且没有错误
        if email_content and not email_content.get('error'):
            print(f"[{time.strftime('%H:%M:%S')}] 数据库查询耗时: {end_query - start_query:.4f} 秒。")

            # --- === 添加标记已读的逻辑 (本地 + Gmail) === ---
            # 检查邮件是否是从数据库成功获取的，并且其状态是未读 (is_unread=1)
            if email_content.get('is_unread') == 1:
                print(f"[{time.strftime('%H:%M:%S')}] 邮件 {message_id} 当前为未读，开始处理标记已读...")

                # 2. 首先，在本地数据库中标记为已读
                marked_local = database.mark_email_as_read_locally(message_id)
                if marked_local:
                    print(f"[{time.strftime('%H:%M:%S')}] 成功在本地将邮件 {message_id} 标记为已读。")

                    # 3. 然后，尝试在 Gmail 上标记为已读
                    print(f"[{time.strftime('%H:%M:%S')}] 尝试获取 Gmail 服务以同步标记已读状态...")
                    service = gmail_client.get_gmail_service() # 获取 Gmail 服务实例
                    if service:
                        print(f"[{time.strftime('%H:%M:%S')}] 正在尝试通过 API 在 Gmail 上将邮件 {message_id} 标记为已读...")
                        marked_gmail = gmail_client.mark_message_as_read_on_gmail(service, message_id)
                        if marked_gmail:
                            print(f"[{time.strftime('%H:%M:%S')}] 成功通过 API 在 Gmail 上将邮件 {message_id} 标记为已读。")
                        else:
                            print(f"[{time.strftime('%H:%M:%S')}] [警告] 未能通过 API 在 Gmail 上将邮件 {message_id} 标记为已读（详见 API 错误日志）。本地状态仍为已读。")
                    else:
                        print(f"[{time.strftime('%H:%M:%S')}] [警告] 无法获取 Gmail 服务，跳过在 Gmail 上标记已读的操作。本地状态仍为已读。")

                else:
                    print(f"[{time.strftime('%H:%M:%S')}] [警告] 未能将邮件 {message_id} 在本地标记为已读 (可能是数据库出错)。不同步 Gmail 状态。")

            # --- === 结束标记已读的逻辑 === ---

        # ... (处理错误和返回模板的部分保持不变)
        elif email_content and email_content.get('error'):
            print(f"[{time.strftime('%H:%M:%S')}] 获取邮件详情失败: {email_content.get('error')}")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] 数据库未找到邮件 {message_id} 或返回意外结果。")
            email_content = {'id': message_id, 'error': '在本地数据库中未找到该邮件。可能尚未同步或已被删除。', 'subject':'未找到', 'body':''}


    except Exception as e:
        error_message = f"查询邮件 {message_id} 详情时发生严重错误: {e}"
        print(f"[{time.strftime('%H:%M:%S')}] {error_message}")
        import traceback
        traceback.print_exc()
        email_content = {'id': message_id, 'error': error_message, 'subject':'查询错误', 'body':'查询错误'}

    return render_template('view_email_content.html', email=email_content)


# --- 运行应用 ---
if __name__ == '__main__':
    print("执行数据库初始化检查...")
    database.init_db() # 确保数据库和表已创建

    # 再次检查模型是否已加载
    if not model or not tokenizer:
        print("错误：模型或 Tokenizer 未加载，无法启动应用。")
        exit()

    print("准备启动后台同步线程...")
    stop_sync_event.clear() # 确保停止事件未被设置
    # 创建并启动后台线程
    background_sync_thread = threading.Thread(target=run_background_sync, daemon=True)
    background_sync_thread.start()

    print(f"启动 Flask 应用 (http://127.0.0.1:5000)... 按 CTRL+C 停止。")
    # 使用 try...finally 确保线程可以被通知停止
    try:
         # use_reloader=False 非常重要，防止 Flask 在 debug 模式下启动两次应用和线程
         # host='0.0.0.0' 允许局域网访问，如果需要的话
         app.run(debug=True, port=5000, use_reloader=False)#, host='0.0.0.0')
    finally:
         # 应用退出时尝试停止后台线程
         print("\nFlask 应用正在退出，尝试停止后台同步线程...")
         stop_sync_event.set() # 设置停止事件，通知线程退出循环
         if background_sync_thread and background_sync_thread.is_alive():
             print("等待后台线程结束 (最多10秒)...")
             background_sync_thread.join(timeout=10) # 等待线程结束，设置超时
             if background_sync_thread.is_alive():
                 print("警告：后台同步线程在超时后仍未结束。")
             else:
                  print("后台同步线程已成功结束。")
         else:
              print("后台同步线程未运行或已结束。")
         print("应用程序退出。")