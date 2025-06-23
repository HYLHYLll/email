# gmail_client.py (包含代理和必要的 API 调用函数 - 最终版)
import os
import os.path
import base64
import time
from email.header import decode_header, make_header
import ssl # 导入 ssl 以处理潜在的 SSL 验证问题
import json # 用于解析 API 错误详情

# 核心库导入
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# 代理和传输库
import requests
import httplib2
# httplib2 的代理依赖 socks 库
try:
    import socks
except ImportError:
    print("错误：找不到 socks 库。请运行 'pip install PySocks' 安装它。")
    # 可以在这里决定是退出还是不使用代理继续
    socks = None # 标记 socks 未找到
    print("警告：socks 库未安装，httplib2 代理功能将不可用。")
    # exit() # 如果代理是必须的，则取消注释这行
from google_auth_httplib2 import AuthorizedHttp # 用于包装 httplib2 和凭证

# --- V2RayN 代理设置 ---
# 从环境变量读取代理设置，如果未设置则使用默认值
PROXY_ENABLED = os.environ.get('PROXY_ENABLED', 'true').lower() == 'true' # 默认启用代理
PROXY_HOST = os.environ.get('PROXY_HOST', "127.0.0.1")
PROXY_PORT_HTTP = int(os.environ.get('PROXY_PORT_HTTP', 10809)) # HTTP 代理端口
PROXY_PORT_SOCKS = int(os.environ.get('PROXY_PORT_SOCKS', 10808)) # SOCKS 代理端口
# 优先使用 HTTP 代理，如果 socks 库可用且配置了 SOCKS 端口
PROXY_TYPE_HTP = socks.PROXY_TYPE_HTTP if socks else None
PROXY_TYPE_SOCKS = socks.PROXY_TYPE_SOCKS5 if socks else None

# 决定实际使用的代理类型和端口 (可以根据需要调整优先级)
USE_PROXY_TYPE = PROXY_TYPE_HTP
USE_PROXY_PORT = PROXY_PORT_HTTP
# 如果你想优先用 SOCKS:
# if PROXY_TYPE_SOCKS:
#     USE_PROXY_TYPE = PROXY_TYPE_SOCKS
#     USE_PROXY_PORT = PROXY_PORT_SOCKS


# 用于 requests 的代理字典 (主要用于刷新令牌)
REQUESTS_PROXIES = None
if PROXY_ENABLED:
    # 根据选择的代理类型配置 requests
    if USE_PROXY_TYPE == PROXY_TYPE_HTP:
        REQUESTS_PROXIES = {
            "http": f"http://{PROXY_HOST}:{USE_PROXY_PORT}",
            "https": f"http://{PROXY_HOST}:{USE_PROXY_PORT}"
        }
    elif USE_PROXY_TYPE == PROXY_TYPE_SOCKS:
         # requests 需要 socks 协议头 (确保安装了 requests[socks])
         # pip install requests[socks]
         REQUESTS_PROXIES = {
            "http": f"socks5h://{PROXY_HOST}:{USE_PROXY_PORT}", # socks5h 支持 DNS 解析通过代理
            "https": f"socks5h://{PROXY_HOST}:{USE_PROXY_PORT}"
         }
    print(f"[Proxy] requests 将使用代理: {REQUESTS_PROXIES}")
else:
    print("[Proxy] requests 代理已禁用。")

# --- /代理设置 ---

# Gmail API 范围 (只读权限)
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'

# --- 工具函数 ---
def decode_mime_header(header_string):
    """安全地解码邮件头字符串。"""
    if not header_string: return ""
    if not isinstance(header_string, str): header_string = str(header_string)
    try: return str(make_header(decode_header(header_string)))
    except Exception: return header_string # 解码失败返回原始值

# --- /工具函数 ---

# --- 凭据加载/保存函数 ---
def load_credentials_from_token():
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            # print(f"[{time.strftime('%H:%M:%S')}] 成功从 {TOKEN_FILE} 加载凭据。")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] 加载 {TOKEN_FILE} 时出错: {e}")
            # 如果 token 文件损坏，尝试删除它
            try: os.remove(TOKEN_FILE); print(f"[{time.strftime('%H:%M:%S')}] 已删除损坏的 {TOKEN_FILE}。")
            except OSError: pass
    # else: print(f"[{time.strftime('%H:%M:%S')}] 未找到 {TOKEN_FILE} 文件。")
    return creds

def save_credentials_to_token(creds):
    try:
        with open(TOKEN_FILE, 'w') as token: token.write(creds.to_json())
        print(f"[{time.strftime('%H:%M:%S')}] 凭据已保存到 {TOKEN_FILE}")
    except Exception as e: print(f"[{time.strftime('%H:%M:%S')}] 保存 {TOKEN_FILE} 时出错: {e}")
# --- /凭据加载/保存函数 ---

# --- 获取 Gmail 服务核心函数 (使用 httplib2 + AuthorizedHttp + 代理) ---
# 使用缓存避免重复构建服务对象
_gmail_service_cache = None
def get_gmail_service(force_refresh=False):
    """获取授权的 Gmail API 服务对象（可选通过代理）。如果需要，会启动授权流程。"""
    global _gmail_service_cache
    if _gmail_service_cache and not force_refresh:
        # print("[Gmail Service] Returning cached service object.")
        return _gmail_service_cache

    print(f"[{time.strftime('%H:%M:%S')}] [Gmail Service] 开始获取或构建 Gmail 服务...")
    creds = load_credentials_from_token()

    # --- 创建 requests Session (带代理用于刷新令牌) ---
    refresh_session = requests.Session()
    if PROXY_ENABLED and REQUESTS_PROXIES:
        refresh_session.proxies = REQUESTS_PROXIES
    refresh_session.timeout = 60 # 设置超时
    # 忽略 SSL 警告（如果代理导致问题，但不推荐）
    # refresh_session.verify = False
    # import urllib3
    # urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # 处理凭据无效或过期
    if not creds or not creds.valid:
        print(f"[{time.strftime('%H:%M:%S')}] [Auth] 无有效凭据或凭据已过期。")
        if creds and creds.expired and creds.refresh_token:
            print(f"[{time.strftime('%H:%M:%S')}] [Auth] 凭据已过期，尝试刷新...")
            try:
                google_auth_request = GoogleAuthRequest(session=refresh_session)
                creds.refresh(google_auth_request)
                print(f"[{time.strftime('%H:%M:%S')}] [Auth] 凭据已刷新。")
                save_credentials_to_token(creds)
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] [Auth Error] 刷新凭据失败: {e}. 需要重新授权。")
                if os.path.exists(TOKEN_FILE):
                    try: os.remove(TOKEN_FILE); print(f"[{time.strftime('%H:%M:%S')}] [Auth] 已删除旧的 {TOKEN_FILE}。")
                    except OSError: pass
                creds = None # 清除无效凭据
        else: # 如果没有凭据或没有刷新令牌
             creds = None # 确保 creds 为 None

        # 如果仍然没有有效凭据，启动新的授权流程
        if not creds:
             if not os.path.exists(CREDENTIALS_FILE):
                 print(f"错误：找不到凭据文件 '{CREDENTIALS_FILE}'。请从 Google Cloud Console 下载。")
                 return None # 无法继续
             print(f"[{time.strftime('%H:%M:%S')}] [Auth] 需要用户授权...")
             try:
                 # 使用环境变量或固定端口可能更适合服务器环境，run_local_server 用于本地开发
                 flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                 # 可以指定 redirect_uri='urn:ietf:wg:oauth:2.0:oob' 使用复制粘贴代码的方式
                 creds = flow.run_local_server(port=0) # port=0 会自动选择可用端口
                 print(f"[{time.strftime('%H:%M:%S')}] [Auth] 授权成功。")
                 if creds: save_credentials_to_token(creds)
             except Exception as e:
                 print(f"[{time.strftime('%H:%M:%S')}] [Auth Error] 授权流程中出错: {e}")
                 return None

    # 如果最终获得了有效凭据
    if creds and creds.valid:
        try:
            # --- 配置 httplib2 代理 ---
            http_client = httplib2.Http(timeout=60) # 默认不带代理
            if PROXY_ENABLED and USE_PROXY_TYPE and socks: # 确保 socks 可用
                print(f"[{time.strftime('%H:%M:%S')}] [Proxy] 配置 httplib2 使用 {USE_PROXY_TYPE} 代理 ({PROXY_HOST}:{USE_PROXY_PORT})")
                proxy_info = httplib2.ProxyInfo(
                    proxy_type=USE_PROXY_TYPE,
                    proxy_host=PROXY_HOST,
                    proxy_port=USE_PROXY_PORT
                    # 可以添加 proxy_rdns=True 如果需要通过代理进行 DNS 解析 (SOCKS5)
                    # proxy_user=..., proxy_pass=... 如果代理需要认证
                )
                # disable_ssl_certificate_validation=True 仅在极端情况下使用
                http_client = httplib2.Http(proxy_info=proxy_info, timeout=60)#, disable_ssl_certificate_validation=True)
            elif PROXY_ENABLED:
                 print(f"[{time.strftime('%H:%M:%S')}] [Proxy Warn] 代理已启用但 socks 库不可用或未配置代理类型，httplib2 将不使用代理。")

            # 使用 google_auth_httplib2 包装凭证和 http 对象
            authed_http = AuthorizedHttp(credentials=creds, http=http_client)

            # 使用包装后的 http 对象构建 Google API 服务
            print(f"[{time.strftime('%H:%M:%S')}] [Gmail Service] 正在构建 Gmail API 服务对象...")
            service = build('gmail', 'v1', http=authed_http, cache_discovery=False)
            print(f"[{time.strftime('%H:%M:%S')}] [Gmail Service] Gmail 服务构建成功。")
            _gmail_service_cache = service # 缓存服务对象
            return service

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] [Gmail Service Error] 构建 Gmail 服务时出错: {e}")
            import traceback
            traceback.print_exc()
            return None
    else:
        print(f"[{time.strftime('%H:%M:%S')}] [Auth Error] 未能获取有效凭据，无法构建 Gmail 服务。")
        return None
# --- /获取 Gmail 服务核心函数 ---


# --- 根据查询获取邮件摘要列表 (修正版) ---
def get_messages_by_query(service, query, max_results=25, format='metadata', metadata_headers=None):
    """
    根据 Gmail 查询字符串获取邮件摘要列表。
    根据 format 参数决定返回内容的详细程度。
    """
    if metadata_headers is None:
        # 默认请求列表视图需要的头信息
        metadata_headers = ['From', 'Subject', 'Date', 'To'] # 添加 To

    if not service:
        print(f"[{time.strftime('%H:%M:%S')}] [API Error] get_messages_by_query 调用时 Gmail 服务未初始化。")
        return []

    emails_summary_list = []
    # print(f"[{time.strftime('%H:%M:%S')}] [API] 开始查询 '{query}' (max: {max_results}, format: {format})...") # 日志太频繁

    try:
        # 1. 列出匹配查询的邮件 ID
        list_request = service.users().messages().list(userId='me', q=query, maxResults=max_results, includeSpamTrash=True) # includeSpamTrash 获取所有位置
        results = list_request.execute()

        messages_info = results.get('messages', [])
        if not messages_info:
            # print(f"[{time.strftime('%H:%M:%S')}] [API] 查询 '{query}' 没有找到匹配的邮件。") # 日志太频繁
            return []

        # print(f"[{time.strftime('%H:%M:%S')}] [API] 找到 {len(messages_info)} 封邮件 ID，准备根据 format='{format}' 获取信息...") # 日志太频繁

        # 2. 遍历邮件 ID，获取指定格式的信息
        # --- (可选优化) 使用 Batch Request 获取元数据 ---
        # batch = service.new_batch_http_request(callback=handle_batch_response)
        # for msg_info in messages_info:
        #    msg_id = msg_info['id']
        #    if format == 'metadata':
        #        batch.add(service.users().messages().get(userId='me', id=msg_id, format='metadata', metadataHeaders=metadata_headers))
        #    elif format == 'minimal':
        #        # minimal 格式只需 ID 和 threadId，直接从 list() 结果获取
        #        emails_summary_list.append({'id': msg_info['id'], 'threadId': msg_info.get('threadId')})
        # batch.execute()
        # return emails_summary_list # 在 callback 中填充列表

        # --- 简单的逐一获取方式 ---
        for i, msg_info in enumerate(messages_info):
            msg_id = msg_info['id']
            thread_id = msg_info.get('threadId') # list() 调用会返回 threadId

            # 如果 format 是 minimal，只返回 ID 和 threadId
            if format == 'minimal':
                emails_summary_list.append({'id': msg_id, 'threadId': thread_id})
                continue # 处理下一封

            # 如果 format 不是 minimal，需要调用 get()
            try:
                get_params = {'userId': 'me', 'id': msg_id, 'format': format}
                if format == 'metadata':
                    get_params['metadataHeaders'] = metadata_headers

                # print(f"[{time.strftime('%H:%M:%S')}] [API] Getting message {msg_id} (format={format})...") # 日志太频繁
                get_start = time.time()
                message = service.users().messages().get(**get_params).execute()
                get_duration = time.time() - get_start
                # print(f"[{time.strftime('%H:%M:%S')}] [API] Get {msg_id} took {get_duration:.2f}s") # 日志太频繁

                # --- 根据 format 解析返回的数据 ---
                email_data = {'id': msg_id, 'threadId': message.get('threadId', thread_id)}

                if format == 'metadata' or format == 'full':
                    payload = message.get('payload', {})
                    headers = payload.get('headers', [])
                    email_data['snippet'] = message.get('snippet', '')
                    email_data['labelIds'] = message.get('labelIds', []) # 获取标签

                    # 解析请求的 headers (对 metadata 和 full 都可能有用)
                    parsed_headers = {}
                    if headers:
                        for header in headers:
                            name = header.get('name', '').lower()
                            value = header.get('value')
                            # 检查是否是我们关心的头
                            if name in [h.lower() for h in metadata_headers]:
                                parsed_headers[name] = decode_mime_header(value)
                    email_data.update(parsed_headers)

                    # 确保默认键存在
                    for key in ['subject', 'from', 'date', 'to']:
                        if key not in email_data:
                             email_data[key] = '' # 设为空字符串而非 'N/A'

                elif format == 'raw':
                     email_data['raw'] = message.get('raw')

                emails_summary_list.append(email_data)

            except HttpError as inner_error:
                 print(f"[{time.strftime('%H:%M:%S')}] [API Error] 获取邮件信息(ID: {msg_id}, format={format})时发生 HttpError: {inner_error}")
                 # 可以在这里添加错误标记到列表，或者直接跳过
                 # emails_summary_list.append({'id': msg_id, 'error': str(inner_error)})
                 if hasattr(inner_error, 'resp') and inner_error.resp.status in [401, 403]:
                     if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE); print(f"[{time.strftime('%H:%M:%S')}] [Auth Error] 认证错误，已删除 {TOKEN_FILE}，请重启应用。")
                     return [] # 认证错误，中断返回
            except Exception as inner_e:
                 print(f"[{time.strftime('%H:%M:%S')}] [Error] 处理邮件信息(ID: {msg_id}, format={format})时发生未知错误: {inner_e}")
                 # emails_summary_list.append({'id': msg_id, 'error': str(inner_e)})


        # print(f"[{time.strftime('%H:%M:%S')}] [API] 邮件信息获取循环完成。") # 日志太频繁
        return emails_summary_list

    except HttpError as error:
        print(f"[{time.strftime('%H:%M:%S')}] [API Error] 执行查询 '{query}' 时发生 HttpError: {error}")
        if hasattr(error, 'resp') and error.resp.status in [401, 403]:
             if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE); print(f"[{time.strftime('%H:%M:%S')}] [Auth Error] 认证错误，已删除 {TOKEN_FILE}，请重启应用。")
        return []
    except Exception as e:
         print(f"[{time.strftime('%H:%M:%S')}] [Error] 执行查询 '{query}' 时发生未知错误: {e}")
         import traceback; traceback.print_exc()
         return []
# --- /根据查询获取邮件摘要列表 ---


# --- 获取单封邮件的完整详情 ---
def get_message_detail(service, message_id, format='full'):
    """
    获取指定 message_id 的邮件完整详情。
    返回包含解析后 headers, payload, labels 的字典。
    """
    if not service:
        print(f"[{time.strftime('%H:%M:%S')}] [API Error] get_message_detail 调用时 Gmail 服务未初始化。")
        return {'id': message_id, 'error': 'Gmail service not initialized.'}

    # print(f"[{time.strftime('%H:%M:%S')}] [API] 开始获取邮件详情 (ID: {message_id}, Format: {format})...") # 日志太频繁
    try:
        # 调用 get API
        message = service.users().messages().get(userId='me', id=message_id, format=format).execute()

        # 解析邮件详情
        payload = message.get('payload', {})
        headers = payload.get('headers', [])
        email_details = {
            'id': message_id,
            'threadId': message.get('threadId'),
            'snippet': message.get('snippet', ''),
            'payload': payload, # 包含原始 payload，供上层解析正文
            'labelIds': message.get('labelIds', []), # 包含标签列表
            'subject': '', # 设置默认值
            'from': '',
            'to': '',
            'date': '',
            'error': None # 成功获取时 error 为 None
        }

        # 解析主要 headers
        if headers:
            for header in headers:
                name = header.get('name', '').lower() # 转小写方便比较
                value = header.get('value')
                if name == 'subject': email_details['subject'] = decode_mime_header(value)
                elif name == 'from': email_details['from'] = decode_mime_header(value)
                elif name == 'to': email_details['to'] = decode_mime_header(value)
                elif name == 'date': email_details['date'] = value # 日期通常不需要解码

        # print(f"[{time.strftime('%H:%M:%S')}] [API] 成功获取邮件 {message_id} 详情。") # 日志太频繁
        return email_details

    except HttpError as error:
        error_content = f"HttpError {error.resp.status}: {error.reason}"
        try: # 尝试解析错误详情
            error_details = json.loads(error.content).get('error', {})
            error_content = f"HttpError {error_details.get('code', error.resp.status)}: {error_details.get('message', error.reason)}"
        except: pass
        print(f"[{time.strftime('%H:%M:%S')}] [API Error] 获取邮件详情(ID: {message_id})时发生错误: {error_content}")
        if hasattr(error, 'resp') and error.resp.status in [401, 403]:
             if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE); print(f"[{time.strftime('%H:%M:%S')}] [Auth Error] 认证错误，已删除 {TOKEN_FILE}，请重启应用。")
        return {'id': message_id, 'error': error_content, 'payload': None} # 返回带错误信息的字典
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [Error] 获取邮件详情(ID: {message_id})时发生未知错误: {e}")
        import traceback; traceback.print_exc()
        return {'id': message_id, 'error': f'Unknown error: {e}', 'payload': None}
# --- /获取单封邮件的完整详情 ---


# --- 将邮件在 Gmail 上标记为已读 ---
def mark_message_as_read_on_gmail(service, message_id):
    """
    通过移除 'UNREAD' 标签，在 Gmail 服务器上将指定邮件标记为已读。
    """
    if not service:
        print(f"[{time.strftime('%H:%M:%S')}] [API Error] mark_message_as_read_on_gmail 调用时 Gmail 服务未初始化。")
        return False

    # print(f"[{time.strftime('%H:%M:%S')}] [API] 准备在 Gmail 上将邮件 {message_id} 标记为已读 (移除 UNREAD 标签)...") # 日志可能过于频繁
    try:
        # 要移除 UNREAD 标签，我们需要调用 modify API
        modify_request_body = {
            'removeLabelIds': ['UNREAD']
            # 如果需要同时添加其他标签，可以使用 'addLabelIds': ['SOME_LABEL']
        }
        # 执行修改请求
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body=modify_request_body
        ).execute()

        print(f"[{time.strftime('%H:%M:%S')}] [API Success] 成功在 Gmail 上将邮件 {message_id} 标记为已读。")
        return True

    except HttpError as error:
        error_content = f"HttpError {error.resp.status}: {error.reason}"
        try: # 尝试解析更详细的错误信息
            error_details = json.loads(error.content).get('error', {})
            error_content = f"HttpError {error_details.get('code', error.resp.status)}: {error_details.get('message', error.reason)}"
        except: pass
        print(f"[{time.strftime('%H:%M:%S')}] [API Error] 在 Gmail 上标记邮件 {message_id} 为已读时发生错误: {error_content}")
        # 特别处理 404 Not Found 错误，可能邮件在 Gmail 上已被删除
        if hasattr(error, 'resp') and error.resp.status == 404:
             print(f"[{time.strftime('%H:%M:%S')}] [API Warn] 邮件 {message_id} 在 Gmail 上未找到，可能已被删除。")
             # 即使 Gmail 上找不到，本地标记可能仍然需要，所以这里不一定返回 False，取决于业务逻辑
             # 但对于标记已读操作，找不到就意味着无法标记，返回 False 是合理的
             return False
        # 处理认证错误
        if hasattr(error, 'resp') and error.resp.status in [401, 403]:
             if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE); print(f"[{time.strftime('%H:%M:%S')}] [Auth Error] 认证错误，已删除 {TOKEN_FILE}，请重启应用。")
        # 对于其他 HttpError，也返回 False
        return False
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] [Error] 在 Gmail 上标记邮件 {message_id} 为已读时发生未知错误: {e}")
        import traceback; traceback.print_exc()
        return False