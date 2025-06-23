# database.py (SQLite 数据库交互 - 最终版)
import sqlite3
import time
import os

DATABASE_FILE = 'local_emails.db'

def get_db_connection():
    """建立到 SQLite 数据库的连接"""
    try:
        # check_same_thread=False 对于后台线程访问是必要的
        # timeout 参数可以增加等待锁的时间（如果并发写入可能发生）
        conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row # 让查询结果可以通过列名访问
        # 启用 WAL (Write-Ahead Logging) 模式可以提高并发读写性能
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn
    except sqlite3.Error as e:
        print(f"[DB Error] 无法连接到数据库 '{DATABASE_FILE}': {e}")
        return None


def init_db():
    """初始化数据库，创建 emails 表 (如果不存在) 并添加索引"""
    print("[DB] 检查并初始化数据库...")
    conn = get_db_connection()
    if not conn: return # 连接失败则退出

    try:
        cursor = conn.cursor()
        # 创建 emails 表，使用更明确的数据类型和约束
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS emails (
                message_id TEXT PRIMARY KEY NOT NULL, -- Gmail 邮件 ID, 非空主键
                thread_id TEXT,                     -- Gmail 线索 ID
                subject TEXT,
                sender TEXT,                        -- 发件人 (From)
                recipient TEXT,                     -- 收件人 (To)
                date_received INTEGER NOT NULL,     -- 接收日期 (Unix 时间戳), 非空
                snippet TEXT,
                body TEXT,                          -- 邮件正文 (HTML 或纯文本)
            
                  body_cleaned TEXT,                  -- 清洗后的纯文本正文 (用于模型)
                labels TEXT,                        -- Gmail 标签 (逗号分隔)
                is_unread INTEGER DEFAULT 1 CHECK(is_unread IN (0, 1)), -- 0 或 1
                custom_rule_result TEXT,            -- 自定义规则结果 ('Filtered', 'Allowed', NULL)
                custom_rule_reason TEXT,            -- 自定义规则原因
                model_prediction TEXT,              -- 模型预测结果 ('垃圾邮件', '正常邮件', NULL)
                model_prob_spam REAL,               -- 模型预测垃圾概率
                model_prob_normal REAL,             -- 模型预测正常概率
                local_category TEXT,                -- 应用内部最终分类 (便于查询)
                last_synced INTEGER NOT NULL        -- 本地最后更新时间戳, 非空
            )
        ''')

        # 2. 检查并添加 body_cleaned 列（如果表已存在但缺少该列）
        cursor.execute("PRAGMA table_info(emails)")
        columns = [column[1] for column in cursor.fetchall()]
        if 'body_cleaned' not in columns:
            print("[DB] 检测到 'emails' 表缺少 'body_cleaned' 列，正在添加...")
            cursor.execute("ALTER TABLE emails ADD COLUMN body_cleaned TEXT")
            print("[DB] 'body_cleaned' 列已添加。")
        # 为常用查询字段创建索引 (如果不存在)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_date_received ON emails (date_received DESC);") # 按日期降序查得多
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_local_category ON emails (local_category);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_is_unread ON emails (is_unread);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_labels ON emails (labels);") # 如果经常按标签查

        conn.commit()
        print(f"[DB] 数据库表 'emails' 已确保存在于 '{DATABASE_FILE}'。")
    except sqlite3.Error as e:
        print(f"[DB Error] 初始化数据库表时出错: {e}")
        conn.rollback() # 出错时回滚
    finally:
        conn.close()

def add_or_update_email(email_data):
    """
    向数据库添加新邮件或更新现有邮件。
    email_data 是一个字典，键应与 emails 表的列名对应。
    """
    required_keys = [
        'message_id', 'thread_id', 'subject', 'sender', 'recipient', 'date_received',
        'snippet', 'body', 'body_cleaned', 'labels', 'is_unread', 'custom_rule_result',
        'custom_rule_reason', 'model_prediction', 'model_prob_spam', 'model_prob_normal',
        'local_category', 'last_synced'
    ]
    # 准备插入的数据，确保所有列都有值（即使是 None）
    data_to_insert = {key: email_data.get(key) for key in required_keys}

    # 数据类型转换和默认值处理
    if data_to_insert['message_id'] is None:
        print("[DB Error] message_id 不能为空，跳过插入。")
        return False
    data_to_insert['is_unread'] = 1 if data_to_insert.get('is_unread') else 0 # 转为 0/1
    data_to_insert['date_received'] = int(data_to_insert.get('date_received', 0)) # 确保是整数
    data_to_insert['last_synced'] = int(data_to_insert.get('last_synced', time.time())) # 确保是整数

    # 使用 INSERT OR REPLACE 语句
    sql = f'''
        INSERT OR REPLACE INTO emails ({", ".join(required_keys)})
        VALUES ({", ".join([':' + key for key in required_keys])})
    '''

    conn = get_db_connection()
    if not conn: return False

    try:
        cursor = conn.cursor()
        cursor.execute(sql, data_to_insert)
        conn.commit()
        # print(f"[DB] 邮件 {data_to_insert['message_id']} 已添加或更新。") # 日志太频繁，注释掉
        return True
    except sqlite3.Error as e:
        print(f"[DB Error] 添加/更新邮件 {data_to_insert.get('message_id')} 时出错: {e}")
        conn.rollback() # 出错时回滚
        return False
    finally:
        conn.close()

def get_emails_by_category(category, limit=25):
    """根据本地分类从数据库获取邮件列表"""
    conn = get_db_connection()
    if not conn: return []

    emails = []
    # 构建 WHERE 子句 (这部分保持上次修改后的状态)
    where_clause = ""
    params = []
    excluded_categories = ('GmailSpam', 'CustomBlocked', 'ModelSpam')
    excluded_placeholders = ', '.join('?' * len(excluded_categories))

    if category == 'inbox':
        where_clause = f"WHERE local_category NOT IN ({excluded_placeholders})"
        params.extend(excluded_categories)
    elif category == 'read':
        where_clause = "WHERE is_unread = ? AND local_category = ?"
        params.append(0)
        params.append('Inbox')
    elif category == 'unread':
        where_clause = "WHERE local_category = ?"
        params.append('Unread')
    elif category == 'spam':
        where_clause = "WHERE local_category = ?"
        params.append('GmailSpam')
    elif category == 'custom':
        where_clause = "WHERE local_category = ?"
        params.append('CustomBlocked')
    elif category == 'model_spam':
        where_clause = "WHERE local_category = ?"
        params.append('ModelSpam')
    else:
        where_clause = f"WHERE local_category NOT IN ({excluded_placeholders})"
        params.extend(excluded_categories)

    params.append(limit)

    # SQL 查询语句确保包含所有需要的列 (保持不变)
    sql = f"""
        SELECT message_id, thread_id, subject, sender, date_received, snippet,
               custom_rule_reason, model_prediction, model_prob_spam, model_prob_normal, local_category, "from"
        FROM emails
        {where_clause}
        ORDER BY date_received DESC
        LIMIT ?
    """

    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        for row in rows:
            # **关键：先将 row 转换为字典**
            email_dict = dict(row)

            # 转换日期 (这里可以直接用 email_dict 或者 row，因为 date_received 肯定存在)
            try:
                # 使用 email_dict 访问更统一
                email_dict['date'] = time.strftime('%Y-%m-%d %H:%M', time.localtime(email_dict['date_received']))
            except (ValueError, TypeError):
                email_dict['date'] = '无效日期'

            # --- 重点：现在在 email_dict (字典) 上使用 .get() ---
            email_dict['reason'] = None
            # 使用 .get() 从字典获取 local_category
            local_cat = email_dict.get('local_category')

            # 1. 处理自定义规则屏蔽的原因
            if local_cat == 'CustomBlocked':
                # 使用 .get() 从字典获取 custom_rule_reason
                rule_reason = email_dict.get('custom_rule_reason')
                if rule_reason:
                    email_dict['reason'] = rule_reason
                else:
                    email_dict['reason'] = "自定义规则屏蔽 (原因未记录)"

            # 2. 处理模型识别垃圾邮件的原因
            elif local_cat == 'ModelSpam':
                # 使用 .get() 从字典获取 model_prediction
                model_pred = email_dict.get('model_prediction')
                if model_pred:
                    reason_str = f"模型: {model_pred}"
                    # 使用 .get() 从字典获取概率
                    prob_n_val = email_dict.get('model_prob_normal')
                    prob_s_val = email_dict.get('model_prob_spam')
                    prob_n_formatted = None
                    prob_s_formatted = None

                    # 尝试格式化概率值（如果存在且有效）
                    if prob_n_val is not None:
                        try:
                            prob_n_formatted = f"{float(prob_n_val):.2f}"
                        except (ValueError, TypeError):
                            pass
                    if prob_s_val is not None:
                        try:
                            prob_s_formatted = f"{float(prob_s_val):.2f}"
                        except (ValueError, TypeError):
                            pass

                    if prob_n_formatted is not None and prob_s_formatted is not None:
                        reason_str += f" (P(N)={prob_n_formatted}, P(S)={prob_s_formatted})"

                    email_dict['reason'] = reason_str
                else:
                    email_dict['reason'] = "模型识别垃圾邮件 (预测结果未记录)"

            # 3. （可选）为其他分类添加原因
            # elif local_cat == 'GmailSpam':
            #    email_dict['reason'] = "Gmail 标记为垃圾邮件"

            # --- 结束 'reason' 字段填充逻辑 ---

            emails.append(email_dict)  # 将处理好的字典添加到列表
    except sqlite3.Error as e:
        print(f"[DB Error] 查询分类 '{category}' 时出错: {e}")
        print(f"[DB Debug] SQL: {sql}")
        print(f"[DB Debug] Params: {params}")
        # 注意：这里我们捕获了数据库错误，但 emails 列表可能为空或不完整
        # app.py 中应该有处理这个 error_message 的逻辑
        raise e  # 重新抛出异常，让上层知道查询失败了
    except Exception as e:  # 捕获其他可能的错误，比如日期转换错误
        print(f"[Error] 处理邮件数据时发生错误: {e}")
        import traceback
        traceback.print_exc()
        # 根据需要决定是否继续处理下一行或直接抛出错误
        # 这里选择继续，但会记录错误
    finally:
        if conn:
            conn.close()
    return emails

def get_category_counts():
    """查询并返回所有邮件分类的数量统计"""
    conn = get_db_connection()
    if not conn:
        # 连接失败，返回一个包含 0 的默认字典
        return {
            'inbox': 0, 'read': 0, 'unread': 0,
            'spam': 0, 'custom': 0, 'model_spam': 0
        }

    counts = {}
    try:
        cursor = conn.cursor()

        # 定义排除的分类和占位符 (与 get_emails_by_category 保持一致)
        excluded_categories = ('GmailSpam', 'CustomBlocked', 'ModelSpam')
        excluded_placeholders = ', '.join('?' * len(excluded_categories))

        # 定义需要计数的分类及其对应的 WHERE 子句和参数
        category_queries = {
            'inbox': (f"WHERE local_category NOT IN ({excluded_placeholders})", list(excluded_categories)),
            'read': ("WHERE is_unread = ? AND local_category = ?", [0, 'Inbox']),
            'unread': ("WHERE local_category = ?", ['Unread']),
            'spam': ("WHERE local_category = ?", ['GmailSpam']),
            'custom': ("WHERE local_category = ?", ['CustomBlocked']),
            'model_spam': ("WHERE local_category = ?", ['ModelSpam'])
        }

        # 遍历每个分类，执行 COUNT 查询
        for category, (where_clause, params) in category_queries.items():
            sql = f"SELECT COUNT(*) FROM emails {where_clause}"
            try:
                cursor.execute(sql, params)
                result = cursor.fetchone()
                # 如果查询成功且结果不为 None，则取第一个元素作为计数，否则为 0
                counts[category] = result[0] if result and result[0] is not None else 0
            except sqlite3.Error as e:
                print(f"[DB Error] 查询分类 '{category}' 计数时出错: {e}")
                print(f"[DB Debug] SQL: {sql}")
                print(f"[DB Debug] Params: {params}")
                counts[category] = 0 # 查询出错也设为 0

    except sqlite3.Error as e:
        print(f"[DB Error] 获取分类计数时发生数据库错误: {e}")
        # 如果在准备阶段出错，返回默认 0 计数
        return {cat: 0 for cat in category_queries.keys()}
    finally:
        if conn:
            conn.close()

    return counts

def get_custom_blocked_emails_data():
    """获取所有当前被自定义规则屏蔽的邮件的核心信息，包括清洗后的正文"""
    conn = get_db_connection()
    if not conn: return []

    emails_data = []
    # 添加 body_cleaned 到 SELECT 列表
    sql = """
        SELECT message_id, sender, subject, body, body_cleaned, labels
        FROM emails
        WHERE local_category = 'CustomBlocked'
    """
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        for row in rows:
            emails_data.append(dict(row)) # 转换为字典
    except sqlite3.Error as e:
        print(f"[DB Error] 查询 CustomBlocked 邮件数据时出错: {e}")
    finally:
        if conn:
            conn.close()
    return emails_data


def update_email_classification(message_id, update_data):
    """
    更新指定邮件的分类相关字段。
    update_data 是一个包含要更新字段和值的字典。
    """
    conn = get_db_connection()
    if not conn: return False

    # 确保包含 last_synced 字段
    if 'last_synced' not in update_data:
        update_data['last_synced'] = int(time.time())

    # 构建 SET 子句
    set_clause = ", ".join([f"{key} = :{key}" for key in update_data.keys()])
    sql = f"UPDATE emails SET {set_clause} WHERE message_id = :message_id"

    # 将 message_id 添加到参数字典中
    params = update_data.copy()
    params['message_id'] = message_id

    success = False
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        if cursor.rowcount > 0:
            # print(f"[DB] 邮件 {message_id} 分类信息已更新。") # 日志可能过于频繁
            success = True
        # else:
            # print(f"[DB] 未找到邮件 {message_id} 进行更新或无需更新。")
    except sqlite3.Error as e:
        print(f"[DB Error] 更新邮件 {message_id} 分类时出错: {e}")
        print(f"[DB Debug] SQL: {sql}")
        print(f"[DB Debug] Params: {params}")
        conn.rollback()
    finally:
        if conn:
            conn.close()
    return success


def get_email_by_id(message_id):
    """根据 message_id 从数据库获取单封邮件的详情"""
    conn = get_db_connection()
    if not conn: return {'id': message_id, 'error': '无法连接到数据库。'}

    email_data = None
    try:
        cursor = conn.cursor()
        # 选择所有需要的列
        cursor.execute("""
            SELECT message_id, thread_id, subject, sender, recipient, date_received,
                   snippet, body, labels, is_unread, custom_rule_result, custom_rule_reason,
                   model_prediction, model_prob_spam, model_prob_normal, local_category, last_synced
            FROM emails WHERE message_id = ?
            """, (message_id,))
        row = cursor.fetchone()
        print(f"[DB Debug] Raw row data for {message_id}: {dict(row)}")
        if row:
            email_data = dict(row)
            # 转换日期格式
            try:
                email_data['date'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['date_received']))
            except ValueError:
                email_data['date'] = '无效日期'
            email_data['error'] = None # 表示成功获取
        else:
            # 明确表示未找到
            email_data = {'id': message_id, 'error': '邮件在本地数据库中未找到。', 'subject': '未找到', 'body': ''}
    except sqlite3.Error as e:
        print(f"[DB Error] 查询邮件 {message_id} 时出错: {e}")
        email_data = {'id': message_id, 'error': f'数据库查询错误: {e}', 'subject': '查询错误', 'body': ''}
    finally:
        if conn:
            conn.close()
    return email_data

def is_message_in_db(message_id):
    """检查邮件 ID 是否已存在于数据库中"""
    conn = get_db_connection()
    if not conn: return False # 连接失败则认为不存在或无法检查

    exists = False
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM emails WHERE message_id = ? LIMIT 1", (message_id,))
        result = cursor.fetchone()
        if result:
            exists = True
    except sqlite3.Error as e:
        print(f"[DB Error] 检查邮件 {message_id} 是否存在时出错: {e}")
    finally:
        if conn:
            conn.close()
    return exists

def get_last_sync_timestamp():
    """获取 emails 表中最新的 last_synced 时间戳"""
    conn = get_db_connection()
    if not conn: return 0

    last_sync = 0 # 默认为 0 (纪元初)
    try:
        cursor = conn.cursor()
        # 使用 MAX() 聚合函数
        cursor.execute("SELECT MAX(last_synced) FROM emails")
        result = cursor.fetchone()
        # 检查结果是否有效且不为 None
        if result and result[0] is not None:
            last_sync = result[0]
    except sqlite3.Error as e:
        print(f"[DB Error] 获取最后同步时间戳时出错: {e}")
    finally:
        if conn:
            conn.close()
    # 确保返回的是整数
    return int(last_sync)

# --- 可选：添加获取邮件最后同步时间的函数，用于更精细的同步控制 ---
def get_email_last_sync(message_id):
    """获取特定邮件的 last_synced 时间戳"""
    conn = get_db_connection()
    if not conn: return 0
    last_sync = 0
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT last_synced FROM emails WHERE message_id = ?", (message_id,))
        result = cursor.fetchone()
        if result and result[0] is not None:
            last_sync = result[0]
    except sqlite3.Error as e:
        print(f"[DB Error] 获取邮件 {message_id} 的最后同步时间时出错: {e}")
    finally:
        if conn:
            conn.close()
    return int(last_sync)

def get_latest_date_received():
    """获取 emails 表中最大的 date_received 时间戳"""
    conn = get_db_connection()
    if not conn: return 0

    latest_date = 0 # 默认为 0
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(date_received) FROM emails")
        result = cursor.fetchone()
        if result and result[0] is not None:
            latest_date = result[0]
    except sqlite3.Error as e:
        print(f"[DB Error] 获取最大接收日期时出错: {e}")
    finally:
        if conn:
            conn.close()
    return int(latest_date) # 确保返回整数

def mark_email_as_read_locally(message_id):
    """
    在本地数据库中将指定邮件标记为已读 (is_unread=0)。
    如果该邮件的本地分类是 'Unread'，则将其更改为 'Inbox'。
    如果更新成功（或无需更新），返回 True，否则返回 False。
    """
    conn = get_db_connection()
    if not conn: return False # 连接数据库失败则返回 False

    success = False # 初始化成功标志
    try:
        cursor = conn.cursor()
        # 执行更新操作：
        # 1. 将 is_unread 设置为 0 (已读)。
        # 2. 使用 CASE 语句判断：如果 local_category 当前是 'Unread'，则将其更新为 'Inbox'，否则保持不变。
        # 3. 使用 WHERE 条件：仅当邮件 ID 匹配且当前状态为未读 (is_unread = 1) 时才执行更新。
        cursor.execute("""
            UPDATE emails
            SET is_unread = 0,
                local_category = CASE
                                    WHEN local_category = 'Unread' THEN 'Inbox'
                                    ELSE local_category
                                END
                -- 如果需要，可以选择性地更新 last_synced 时间戳，但仅标记已读通常不需要
                -- last_synced = ?
            WHERE message_id = ? AND is_unread = 1
            """, (message_id,)) # 如果要更新 last_synced，在这里添加 time.time()
            # SET 子句中未使用的参数已移除

        conn.commit() # 提交事务

        # 检查是否有行被实际修改了
        if cursor.rowcount > 0:
            print(f"[DB] 已在本地将邮件 {message_id} 标记为已读。分类可能已更新。")
        # else:
            # print(f"[DB] 邮件 {message_id} 已经是已读状态或未找到。") # 这条日志可能会过于频繁，注释掉

        success = True # 认为操作成功，即使没有行被修改（因为它已经是已读状态）
    except sqlite3.Error as e:
        print(f"[DB Error] 标记邮件 {message_id} 为已读时失败: {e}")
        conn.rollback() # 发生错误时回滚事务
        success = False # 标记操作失败
    finally:
        if conn:
            conn.close() # 关闭数据库连接
    return success # 返回操作结果

# ... (保留剩余的函数: get_emails_by_category, get_email_by_id, is_message_in_db, get_last_sync_timestamp, get_email_last_sync 等)
def get_custom_blocked_emails_data():
    """获取所有当前被自定义规则屏蔽的邮件的核心信息，包括清洗后的正文"""
    # 这个函数保持原样即可
    conn = get_db_connection()
    if not conn: return []

    emails_data = []
    sql = """
        SELECT message_id, sender, subject, body, body_cleaned, labels
        FROM emails
        WHERE local_category = 'CustomBlocked'
    """
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        for row in rows:
            emails_data.append(dict(row))
    except sqlite3.Error as e:
        print(f"[DB Error] 查询 CustomBlocked 邮件数据时出错: {e}")
    finally:
        if conn:
            conn.close()
    return emails_data

def get_emails_for_reclassification_by_categories(categories_list):
    """
    获取指定本地分类的邮件核心信息，用于重新评估。
    Args:
        categories_list: 一个本地分类字符串的列表, 例如: ['ModelSpam', 'GmailSpam']
    """
    if not categories_list:
        return []

    conn = get_db_connection()
    if not conn: return []

    emails_data = []
    placeholders = ', '.join('?' * len(categories_list)) # 生成占位符 ?, ?, ...
    sql = f"""
        SELECT message_id, sender, subject, body, body_cleaned, labels
        FROM emails
        WHERE local_category IN ({placeholders})
    """
    try:
        cursor = conn.cursor()
        cursor.execute(sql, categories_list) # 将列表作为参数传递
        rows = cursor.fetchall()
        for row in rows:
            emails_data.append(dict(row))
    except sqlite3.Error as e:
        print(f"[DB Error] 查询待重新分类邮件数据 (分类: {categories_list}) 时出错: {e}")
    finally:
        if conn:
            conn.close()
    return emails_data


def get_emails_potentially_affected_by_allow_rule_removal():
    """
    获取那些之前可能因为“允许”规则而被分类为 'Inbox' 或 'Unread' 的邮件。
    这些邮件的 custom_rule_result 应该是 'Normal'。
    """
    conn = get_db_connection()
    if not conn: return []

    emails_data = []
    sql = """
        SELECT message_id, sender, subject, body, body_cleaned, labels
        FROM emails
        WHERE custom_rule_result = 'Normal' AND local_category IN ('Inbox', 'Unread')
    """
    # 注意: 这里的条件是 custom_rule_result = 'Normal'
    # 这意味着它们是被某个*允许*规则标记为正常的。
    # 当我们移除一个允许规则时，这些邮件需要重新评估，
    # 因为它们可能不再被任何允许规则覆盖，此时模型或其他规则可能将其标记为垃圾邮件。
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        for row in rows:
            emails_data.append(dict(row))
    except sqlite3.Error as e:
        print(f"[DB Error] 查询可能受允许规则移除影响的邮件数据时出错: {e}")
    finally:
        if conn:
            conn.close()
    return emails_data

def get_emails_not_yet_custom_blocked():
    """
    获取所有当前本地分类不是 'CustomBlocked' 的邮件的核心信息。
    这些邮件在添加新的阻止规则时可能需要重新评估。
    """
    conn = get_db_connection()
    if not conn: return []

    emails_data = []
    # 我们选择所有非 CustomBlocked 的邮件，因为任何一个都可能被新的阻止规则命中
    # 如果邮件已经是 CustomBlocked，那么新的阻止规则（除非是完全相同的）不会改变它的 CustomBlocked 状态
    # 但为了逻辑简单和覆盖所有情况，获取所有非 CustomBlocked 邮件进行检查是安全的。
    sql = """
        SELECT message_id, sender, subject, body, body_cleaned, labels
        FROM emails
        WHERE local_category != 'CustomBlocked' OR local_category IS NULL
    """
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        for row in rows:
            emails_data.append(dict(row))
    except sqlite3.Error as e:
        print(f"[DB Error] 查询非 CustomBlocked 邮件数据时出错: {e}")
    finally:
        if conn:
            conn.close()
    return emails_data

if __name__ == '__main__':
    # 如果直接运行此文件，则执行初始化
    print("直接运行 database.py，执行数据库初始化...")
    init_db()
    print("初始化完成。你可以使用 DB Browser for SQLite 等工具查看 local_emails.db 文件。")