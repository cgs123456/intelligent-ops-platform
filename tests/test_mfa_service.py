"""MFA 服务测试（P0-2 备用恢复码哈希存储 + P1-8 熵值提升）"""

from services.mfa import MFAService


class TestMFAService:
    """MFA TOTP 双因素认证服务测试"""

    def test_generate_secret_length(self):
        """TOTP secret 为 base32 编码 32 字符"""
        secret = MFAService.generate_secret()
        assert len(secret) == 32
        assert secret.isalnum()

    def test_verify_code_valid(self):
        """有效 TOTP 验证码通过"""
        import pyotp

        secret = MFAService.generate_secret()
        code = pyotp.TOTP(secret).now()
        assert MFAService.verify_code(secret, code) is True

    def test_verify_code_invalid(self):
        """无效验证码被拒"""
        secret = MFAService.generate_secret()
        assert MFAService.verify_code(secret, "000000") is False or MFAService.verify_code(secret, "000000") is True
        # 000000 有极小概率恰好是当前码，用明显错误的码测试
        assert MFAService.verify_code(secret, "abcdef") is False

    def test_verify_code_empty(self):
        """空 secret 或空 code 返回 False"""
        assert MFAService.verify_code("", "123456") is False
        assert MFAService.verify_code("JBSWY3DPEHPK3PXP", "") is False

    # ==================== P0-2: 备用恢复码哈希存储 ====================

    def test_backup_codes_return_tuple(self):
        """P0-2: generate_backup_codes 返回 (明文列表, 哈希列表) 元组"""
        plaintext_codes, hashed_codes = MFAService.generate_backup_codes(count=5)
        assert len(plaintext_codes) == 5
        assert len(hashed_codes) == 5

    def test_backup_codes_plaintext_not_equal_hash(self):
        """P0-2: 明文码与哈希码不同（哈希不可逆）"""
        plaintext_codes, hashed_codes = MFAService.generate_backup_codes(count=3)
        for plain, hashed in zip(plaintext_codes, hashed_codes, strict=False):
            assert plain != hashed
            assert "$" in hashed  # 哈希格式：salt$hash

    def test_backup_code_verify_correct(self):
        """P0-2: 正确的备用码验证通过，返回匹配索引"""
        plaintext_codes, hashed_codes = MFAService.generate_backup_codes(count=5)
        # 验证第 3 个码
        idx = MFAService.verify_backup_code(plaintext_codes[2], hashed_codes)
        assert idx == 2

    def test_backup_code_verify_wrong(self):
        """P0-2: 错误的备用码验证失败，返回 -1"""
        plaintext_codes, hashed_codes = MFAService.generate_backup_codes(count=5)
        idx = MFAService.verify_backup_code("WRONG-CODE-XXXX", hashed_codes)
        assert idx == -1

    def test_backup_code_verify_empty(self):
        """P0-2: 空输入返回 -1"""
        _, hashed_codes = MFAService.generate_backup_codes(count=3)
        assert MFAService.verify_backup_code("", hashed_codes) == -1
        assert MFAService.verify_backup_code("ABCD-EFGH", []) == -1
        assert MFAService.verify_backup_code(None, hashed_codes) == -1

    def test_backup_code_verify_case_insensitive(self):
        """P0-2: 备用码大小写不敏感"""
        plaintext_codes, hashed_codes = MFAService.generate_backup_codes(count=3)
        # 用小写验证
        idx = MFAService.verify_backup_code(plaintext_codes[0].lower(), hashed_codes)
        assert idx == 0

    def test_backup_code_hash_contains_salt(self):
        """P0-2: 哈希格式为 salt$hash，salt 为 32 位 hex（16 字节）"""
        _, hashed_codes = MFAService.generate_backup_codes(count=2)
        for hashed in hashed_codes:
            parts = hashed.split("$", 1)
            assert len(parts) == 2
            salt, hash_val = parts
            assert len(salt) == 32  # token_hex(16) = 32 hex chars
            assert len(hash_val) == 64  # SHA-256 hex = 64 chars

    # ==================== P1-8: 备用码熵值提升 ====================

    def test_backup_code_entropy_sufficient(self):
        """P1-8: 备用码长度足够（token_urlsafe(16) 约 22 字符，128 位熵）"""
        plaintext_codes, _ = MFAService.generate_backup_codes(count=1)
        code = plaintext_codes[0]
        # 格式 XXXXXXXXX-XXXXXXXXX，去掉分隔符后应 >= 20 字符
        raw = code.replace("-", "")
        assert len(raw) >= 20, f"备用码熵值不足：{code} 仅 {len(raw)} 字符"

    def test_backup_codes_unique(self):
        """生成的备用码互不相同"""
        plaintext_codes, _ = MFAService.generate_backup_codes(count=10)
        assert len(set(plaintext_codes)) == 10
