"""MFA TOTP 服务（P0-8）

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

    # 生成备用恢复码（每个用户 10 个，一次性使用）
    backup_codes = mfa_service.generate_backup_codes()
"""

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

        :param count: 生成数量
        :return: 备用码列表（格式：XXXX-XXXX，8 位字母数字）
        """
        codes = []
        for _ in range(count):
            # 8 位字母数字，分两段显示
            raw = pysecrets.token_hex(4).upper()  # 8 位 hex
            code = f"{raw[:4]}-{raw[4:]}"
            codes.append(code)
        return codes

    @staticmethod
    def verify_backup_code(input_code, stored_codes):
        """验证备用恢复码（一次性，验证后应从存储中删除）

        :param input_code: 用户输入的备用码
        :param stored_codes: 存储的备用码列表（哈希后的）
        :return: 匹配的索引（未匹配返回 -1）
        """
        if not input_code or not stored_codes:
            return -1
        input_upper = input_code.strip().upper()
        for idx, stored in enumerate(stored_codes):
            if stored and stored.upper() == input_upper:
                return idx
        return -1


# 单例
mfa_service = MFAService()
