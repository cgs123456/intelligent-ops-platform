"""字段级加密服务测试（P0-1 生产强制校验 + P1-14 加密失败抛异常）"""

import os
from unittest.mock import patch

import pytest

from services.crypto import FieldCrypto, _is_production


class TestFieldCrypto:
    """字段级加密服务测试"""

    def test_encrypt_decrypt_roundtrip(self):
        """加密后解密恢复原文"""
        crypto = FieldCrypto()
        plaintext = "13800138000"
        encrypted = crypto.encrypt(plaintext)
        assert encrypted != plaintext
        assert encrypted.startswith("enc:")
        decrypted = crypto.decrypt(encrypted)
        assert decrypted == plaintext

    def test_encrypt_empty_returns_empty(self):
        """空值加密返回空值"""
        crypto = FieldCrypto()
        assert crypto.encrypt("") == ""
        assert crypto.encrypt(None) is None

    def test_decrypt_non_encrypted_returns_original(self):
        """未加密的旧数据（无 enc: 前缀）原样返回"""
        crypto = FieldCrypto()
        assert crypto.decrypt("13800138000") == "13800138000"
        assert crypto.decrypt("plain text") == "plain text"

    def test_decrypt_invalid_ciphertext_returns_none(self):
        """无效密文解密返回 None"""
        crypto = FieldCrypto()
        result = crypto.decrypt("enc:invalidciphertext")
        assert result is None

    def test_generate_key_returns_valid_fernet_key(self):
        """generate_key 返回有效的 Fernet 密钥"""
        key = FieldCrypto.generate_key()
        assert isinstance(key, str)
        # 用生成的 key 能构造 Fernet
        from cryptography.fernet import Fernet

        Fernet(key.encode())  # 不抛异常即有效

    # ==================== P0-1: 生产环境密钥校验 ====================

    def test_is_production_detection(self):
        """_is_production 正确识别生产环境"""
        with patch.dict(os.environ, {"FLASK_ENV": "production"}):
            assert _is_production() is True
        with patch.dict(os.environ, {"FLASK_ENV": "development"}):
            assert _is_production() is False

    # ==================== P1-14: 生产环境加密失败抛异常 ====================

    def test_encrypt_failure_in_production_raises(self):
        """P1-14: 生产环境加密失败抛 RuntimeError，不返回明文"""
        crypto = FieldCrypto()
        # 模拟生产环境
        with (
            patch("services.crypto._is_production", return_value=True),
            patch.object(crypto, "_fernet", None),
            pytest.raises(RuntimeError, match="字段加密器未初始化"),
        ):
            crypto.encrypt("sensitive_data")

    def test_encrypt_failure_in_dev_returns_plaintext(self):
        """开发环境加密失败返回明文（降级可接受）"""
        crypto = FieldCrypto()
        with patch("services.crypto._is_production", return_value=False), patch.object(crypto, "_fernet", None):
            result = crypto.encrypt("sensitive_data")
            assert result == "sensitive_data"

    def test_encrypt_with_broken_fernet_in_production_raises(self):
        """P1-14: 生产环境 fernet.encrypt 抛异常时，encrypt 也抛 RuntimeError"""
        crypto = FieldCrypto()

        class BrokenFernet:
            def encrypt(self, data):
                raise Exception("simulated encryption failure")

        with (
            patch("services.crypto._is_production", return_value=True),
            patch.object(crypto, "_fernet", BrokenFernet()),
            pytest.raises(RuntimeError, match="字段加密失败"),
        ):
            crypto.encrypt("sensitive_data")
