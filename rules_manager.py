# rules_manager.py
import json
import os

RULES_FILE = 'rules.json'

def load_rules():
    """从 rules.json 加载规则"""
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, 'r', encoding='utf-8') as f:
                rules = json.load(f)
                # 确保所有基本规则列表存在
                for key in ['allowedSenders', 'blockedSenders', 'allowedKeywords', 'blockedKeywords']:
                    if key not in rules:
                        rules[key] = []
                    # 确保是列表且内容是字符串 (基本验证)
                    if not isinstance(rules[key], list):
                        print(f"[Rules Warn] Rule type '{key}' in {RULES_FILE} is not a list. Resetting.")
                        rules[key] = []
                    else:
                        # 清理非字符串项，并转小写
                        rules[key] = [str(item).lower().strip() for item in rules[key] if isinstance(item, str) and item.strip()]
                return rules
        except (json.JSONDecodeError, IOError) as e:
            print(f"读取规则文件 '{RULES_FILE}' 时出错: {e}")
            # 返回默认空规则
            return {'allowedSenders': [], 'blockedSenders': [], 'allowedKeywords': [], 'blockedKeywords': []}
    else:
        # 文件不存在，返回默认空规则
        return {'allowedSenders': [], 'blockedSenders': [], 'allowedKeywords': [], 'blockedKeywords': []}

def save_rules(rules_dict):
    """将规则字典保存到 rules.json"""
    try:
        cleaned_rules = {}
        default_keys = ['allowedSenders', 'blockedSenders', 'allowedKeywords', 'blockedKeywords']
        for key in default_keys:
             rule_list = rules_dict.get(key, [])
             if isinstance(rule_list, list):
                 # 去重、转小写、去空、排序
                 cleaned_list = sorted(list(set(str(item).lower().strip() for item in rule_list if isinstance(item, str) and item.strip())))
                 cleaned_rules[key] = cleaned_list
             else:
                 cleaned_rules[key] = [] # 格式不对存空列表

        with open(RULES_FILE, 'w', encoding='utf-8') as f:
            json.dump(cleaned_rules, f, ensure_ascii=False, indent=4)
        print(f"规则已保存到 {RULES_FILE}")
        return True
    except IOError as e:
        print(f"写入规则文件 '{RULES_FILE}' 时出错: {e}")
        return False
    except Exception as e:
        print(f"保存规则时发生未知错误: {e}")
        return False


def apply_rules(message_data, rules):
    """
    根据邮件信息和规则判断是否命中规则。
    返回 ('分类', '原因') 元组，如果未命中则返回 None。
    分类: 'Normal' 或 'Filtered'
    """
    sender = message_data.get('from', '').lower()
    subject = message_data.get('subject', '').lower()
    body = message_data.get('body', '').lower() # 假设 body 字段存在且为纯文本

    # 规则优先级: 允许 > 阻止
    # 使用 load_rules 清理后的规则

    # 1. 检查允许列表 (白名单)
    for allowed in rules.get('allowedSenders', []):
        # 使用 'in' 进行部分匹配 (e.g., "example.com" matches "user@example.com")
        if allowed and allowed in sender:
            return ('Normal', f'规则匹配: 发件人白名单 ({allowed})')
    for keyword in rules.get('allowedKeywords', []):
        if keyword and (keyword in subject or keyword in body):
            return ('Normal', f'规则匹配: 关键词白名单 ({keyword})')

    # 2. 检查阻止列表 (黑名单)
    for blocked in rules.get('blockedSenders', []):
        if blocked and blocked in sender:
            return ('Filtered', f'规则匹配: 发件人黑名单 ({blocked})')
    for keyword in rules.get('blockedKeywords', []):
        if keyword and (keyword in subject or keyword in body):
            return ('Filtered', f'规则匹配: 关键词黑名单 ({keyword})')

    # 没有规则命中
    return None