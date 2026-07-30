"""字段级加密服务（P0-8）

使用 Fernet 对称加密（AES-128-CBC + HMAC-SHA256）对敏感字段加密，
密钥从环境变量 FIELD_ENCRYPTION_KEY 读取（Fernet 兼容的 base64 编码 32 字节）。

用法：
    from services.crypto import field_crypto
    encrypted = field_crypto.encrypt("13800138000")
    plaintext = field_crypto.decrypt(encrypted)  # "13800138000"

若无 FIELD_ENCRYPTION_KEY，则降级为不加密（仅记日志），适用于开发环境。
"""

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class FieldCrypto:
    """字段级加密（Fernet 对称加密）"""

    def __init__(self):
        self._fernet = None
        self._init_key()

    def _init_key(self):
        """从环境变量读取密钥，无则用 SECRET_KEY 派生（开发降级）"""
        key = os.getenv("FIELD_ENCRYPTION_KEY", "")
        if not key:
            # 开发降级：用 SECRET_KEY 派生密钥（不推荐生产使用）
            secret = os.getenv("SECRET_KEY", "dev-only-secret-key")
            key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()).decode()
            logger.warning("FIELD_ENCRYPTION_KEY 未设置，使用 SECRET_KEY 派生密钥（仅限开发）")
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except Exception as e:
            logger.error("字段加密密钥初始化失败：%s", e)
            self._fernet = None

    def encrypt(self, plaintext):
        """加密明文，返回加密字符串（带前缀 enc: 便于识别）"""
        if not plaintext or not self._fernet:
            return plaintext
        try:
            encrypted = self._fernet.encrypt(str(plaintext).encode("utf-8")).decode("utf-8")
            return f"enc:{encrypted}"
        except Exception as e:
            logger.error("字段加密失败：%s", e)
            return plaintext

    def decrypt(self, ciphertext):
        """解密，返回明文。非加密字符串（无 enc: 前缀）原样返回。"""
        if not ciphertext or not isinstance(ciphertext, str):
            return ciphertext
        if not ciphertext.startswith("enc:"):
            return ciphertext  # 未加密的旧数据，原样返回
        if not self._fernet:
            return ciphertext
        try:
            return self._fernet.decrypt(ciphertext[4:].encode("utf-8")).decode("utf-8")
        except InvalidToken:
            logger.error("字段解密失败：密文无效或密钥不匹配")
            return None
        except Exception as e:
            logger.error("字段解密异常：%s", e)
            return None

    @staticmethod
    def generate_key():
        """生成新的 Fernet 密钥（用于初始化生产环境）"""
        return Fernet.generate_key().decode()


# 单例
field_crypto = FieldCrypto()
