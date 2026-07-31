"""MFA TOTP 服务（P0-8 + P0-2 安全加固）

基于 RFC 6238 的 TOTP（Time-based One-Time Password）双因素认证。
使用 pyotp 库生成和验证 6 位动态验证码。

用法：
    from services.mfa import mfa_service

    # 用户开启 MFA：生成 secret + otpauth URI
    secret = mfa_service.generate_secret()
    uri = mfa_service.get_provisioning_uri(secret, "user@example.com", "OpsPlatform")

    # 用户输入 6 位验证码
    if mfa_service.verify_code(secret, "123456"):
        # 验证通过
        pass

    # 生成备用恢复码（返回明文给用户一次，哈希入库）
    plaintext_codes, hashed_codes = mfa_service.generate_backup_codes()
    # 存储 hashed_codes 到数据库，明文 plaintext_codes 只返回给用户一次

安全策略（P0-2 + P1-8）：
- 备用恢复码使用 SHA-256 + 随机 salt 哈希存储，数据库泄露无法直接使用
- 备用码熵值从 16 位（token_hex(4)）提升到 128 位（token_urlsafe(16)），防暴力破解
- 验证后从存储中删除该哈希（一次性使用）
"""

import hashlib
import hmac
import logging
import os
import secrets as pysecrets

import pyotp

logger = logging.getLogger(__name__)

ISSUER = os.getenv("MFA_ISSUER", "OpsPlatform")
BACKUP_CODE_COUNT = 10


class MFAService:
    """TOTP 双因素认证服务"""

    @staticmethod
    def generate_secret():
        """生成 TOTP secret（base32 编码，32 字符）"""
        return pyotp.random_base32()

    @staticmethod
    def get_provisioning_uri(secret, user_identifier, issuer=None):
        """生成 otpauth:// URI（用于二维码扫描）

        :param secret: TOTP secret
        :param user_identifier: 用户标识（邮箱或用户名）
        :param issuer: 发行方名称
        :return: otpauth://totp/Issuer:user_identifier?secret=XXX&issuer=Issuer
        """
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=user_identifier, issuer_name=issuer or ISSUER)

    @staticmethod
    def verify_code(secret, code):
        """验证 6 位 TOTP 验证码

        :param secret: TOTP secret
        :param code: 用户输入的 6 位验证码
        :return: True 验证通过，False 验证失败
        """
        if not secret or not code:
            return False
        try:
            totp = pyotp.TOTP(secret)
            # valid_window=1 允许前后 30 秒的时间偏差
            return totp.verify(code, valid_window=1)
        except Exception as e:
            logger.warning("TOTP 验证异常：%s", e)
            return False

    @staticmethod
    def generate_backup_codes(count=BACKUP_CODE_COUNT):
        """生成一次性备用恢复码（用于丢失设备时恢复访问）

        P0-2: 返回明文 + 哈希两份。明文只给用户一次，哈希入库。
        P1-8: 熵值从 token_hex(4)（16位）提升到 token_urlsafe(16)（128位）。

        :param count: 生成数量
        :return: (plaintext_codes, hashed_codes) 元组
                 - plaintext_codes: 明文备用码列表（格式：XXXXXX-XXXXXX，返回给用户一次）
                 - hashed_codes: 哈希后的备用码列表（存数据库，格式：salt$hash）
        """
        plaintext_codes = []
        hashed_codes = []
        for _ in range(count):
            # P1-8: token_urlsafe(16) 产生 22 字符，约 128 位熵，防暴力破解
            raw = pysecrets.token_urlsafe(16).upper()
            # 分两段显示，便于用户抄录
            code = f"{raw[:11]}-{raw[11:]}"
            plaintext_codes.append(code)
            hashed_codes.append(MFAService._hash_backup_code(code))
        return plaintext_codes, hashed_codes

    @staticmethod
    def _hash_backup_code(code):
        """P0-2: 使用 SHA-256 + 随机 salt 哈希备用码

        :param code: 明文备用码
        :return: "salt$hash" 格式字符串（salt 为 16 字节 hex）
        """
        salt = pysecrets.token_hex(16)
        normalized = code.strip().upper()
        hashed = hashlib.sha256((salt + normalized).encode("utf-8")).hexdigest()
        return f"{salt}${hashed}"

    @staticmethod
    def verify_backup_code(input_code, stored_codes):
        """验证备用恢复码（一次性，验证后应从存储中删除）

        P0-2: stored_codes 为 salt$hash 格式的哈希列表，使用 SHA-256 + salt 比对。

        :param input_code: 用户输入的备用码
        :param stored_codes: 存储的备用码哈希列表（salt$hash 格式）
        :return: 匹配的索引（未匹配返回 -1），调用方应在匹配后从存储中删除该项
        """
        if not input_code or not stored_codes:
            return -1
        input_upper = input_code.strip().upper()
        for idx, stored in enumerate(stored_codes):
            if not stored or "$" not in stored:
                # 兼容：若存储的是明文（旧数据），也做一次大小写比对（建议尽快迁移到哈希）
                if stored and stored.upper() == input_upper:
                    return idx
                continue
            # P0-2: 哈希比对 salt$hash
            parts = stored.split("$", 1)
            if len(parts) != 2:
                continue
            salt, expected_hash = parts
            actual_hash = hashlib.sha256((salt + input_upper).encode("utf-8")).hexdigest()
            # 使用 hmac.compare_digest 防止时序攻击
            if hmac.compare_digest(actual_hash, expected_hash):
                return idx
        return -1


# 单例
mfa_service = MFAService()
