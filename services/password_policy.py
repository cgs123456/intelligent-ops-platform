"""密码安全策略（P2-7 + P0-8 强化）"""

import re

from werkzeug.security import check_password_hash, generate_password_hash

MIN_LENGTH = 8
REQUIRE_UPPER = True
REQUIRE_LOWER = True
REQUIRE_DIGIT = True
REQUIRE_SPECIAL = True  # P0-8: 强制要求特殊字符
PASSWORD_HISTORY_LIMIT = 5  # P0-8: 禁止重用最近 5 次密码


def validate_password(password):
    """校验密码强度，返回 (is_valid, error_message)"""
    if len(password) < MIN_LENGTH:
        return False, f"密码长度至少 {MIN_LENGTH} 位"
    if REQUIRE_UPPER and not re.search(r"[A-Z]", password):
        return False, "密码需包含大写字母"
    if REQUIRE_LOWER and not re.search(r"[a-z]", password):
        return False, "密码需包含小写字母"
    if REQUIRE_DIGIT and not re.search(r"\d", password):
        return False, "密码需包含数字"
    if REQUIRE_SPECIAL and not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return False, "密码需包含特殊字符"
    return True, None


def hash_password(password):
    """哈希密码（PBKDF2-SHA256）"""
    return generate_password_hash(password, method="pbkdf2:sha256", salt_length=16)


def verify_password(password, password_hash):
    """验证密码"""
    return check_password_hash(password_hash, password)


def check_password_history(new_password, history_hashes):
    """P0-8: 检查新密码是否与历史密码重复。

    :param new_password: 新密码明文
    :param history_hashes: 历史密码哈希列表（最近 N 条，按时间倒序）
    :return: True 表示重复（不可用），False 表示可用
    """
    for old_hash in history_hashes[:PASSWORD_HISTORY_LIMIT]:
        if old_hash and check_password_hash(old_hash, new_password):
            return True
    return False
