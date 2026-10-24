"""认证模块扩展测试 — JWT 黑名单、改密失效、锁定、SSE 认证等"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest


@pytest.mark.usefixtures("db_session")
class TestAuthExtended:
    """AuthService 扩展测试：token 校验、黑名单、改密失效、锁定、SSE 认证"""

    # ==================== 辅助方法 ====================

    def _create_user(self, username, password="Test@123456", must_change=False):
        """在当前 app context 内创建测试用户（admin 角色），返回 user_id"""
        from extensions import db
        from models.system import Role, User
        from services.password_policy import hash_password

        # 必须在 app context 内调用
        role = Role.query.filter_by(name="admin").first()
        if not role:
            role = Role(name="admin", permissions=json.dumps(["*:*"]), description="管理员")
            db.session.add(role)
            db.session.flush()

        user = User(
            username=username,
            password_hash=hash_password(password),
            must_change_password=must_change,
        )
        user.roles.append(role)
        db.session.add(user)
        db.session.commit()
        return user.id

    # ==================== 1. 过期 token ====================

    def test_token_expired_returns_none(self, app):
        """构造过期 token，verify_token 返回 None"""
        from extensions import db
        from models.system import User
        from services.auth import AuthService

        with app.app_context():
            user_id = self._create_user("expired_user")
            user = db.session.get(User, user_id)

            # 构造一个已过期的 payload
            now = datetime.now(timezone.utc)
            payload = {
                "sub": str(user.id),
                "username": user.username,
                "iat": int((now - timedelta(hours=2)).timestamp()),
                "exp": int((now - timedelta(hours=1)).timestamp()),  # 1 小时前过期
                "type": "access",
                "jti": uuid.uuid4().hex,
                "token_ver": user.token_version or 0,
            }
            expired_token = jwt.encode(payload, app.config["SECRET_KEY"], algorithm=AuthService.ALGORITHM)
            assert AuthService.verify_token(expired_token) is None

    # ==================== 2. 无效签名 token ====================

    def test_invalid_token_returns_none(self, app):
        """伪造签名 token，verify_token 返回 None"""
        from extensions import db
        from models.system import User
        from services.auth import AuthService

        with app.app_context():
            user_id = self._create_user("invalid_user")
            user = db.session.get(User, user_id)

            now = datetime.now(timezone.utc)
            payload = {
                "sub": str(user.id),
                "username": user.username,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(hours=1)).timestamp()),
                "type": "access",
                "jti": uuid.uuid4().hex,
                "token_ver": user.token_version or 0,
            }
            # 用错误的密钥签名
            bad_token = jwt.encode(payload, "wrong-secret-key", algorithm=AuthService.ALGORITHM)
            assert AuthService.verify_token(bad_token) is None

    @staticmethod
    def _clear_auth_cache():
        """清空 AuthService 进程内缓存（模拟 30s TTL 过期），让 verify_token 回源 DB 查询。

        AuthService.verify_token 用类级 dict 缓存黑名单状态（TTL=30s），
        测试中 logout/change_password 后需立即验证 DB 层失效，故手动清缓存。
        """
        from services.auth import AuthService

        AuthService._token_cache.clear()
        AuthService._user_cache.clear()

    # ==================== 3. logout 黑名单 ====================

    def test_logout_blacklists_token(self, app):
        """login 后 logout，verify_token 返回 None"""
        from services.auth import AuthService

        with app.app_context():
            self._create_user("logout_user")
            result, err = AuthService.login("logout_user", "Test@123456")
            assert err is None
            access_token = result["access_token"]
            refresh_token = result["refresh_token"]

            # logout 前可正常校验
            assert AuthService.verify_token(access_token) is not None

            # logout 后 token 进黑名单
            revoked_count = AuthService.logout(access_token, refresh_token)
            assert revoked_count >= 1  # 至少吊销了 access + refresh
            # 清空进程内缓存，模拟 30s TTL 过期，让 verify_token 回源 DB 校验黑名单
            self._clear_auth_cache()
            assert AuthService.verify_token(access_token) is None

    # ==================== 4. 改密后旧 token 失效 ====================

    def test_change_password_invalidates_old_token(self, app):
        """login 获取 token，change_password 后旧 token verify_token 返回 None"""
        from services.auth import AuthService

        with app.app_context():
            user_id = self._create_user("chpwd_user")
            result, err = AuthService.login("chpwd_user", "Test@123456")
            assert err is None
            access_token = result["access_token"]

            # 改密前 token 有效
            assert AuthService.verify_token(access_token) is not None

            # 修改密码（新密码需满足强度要求）
            AuthService.change_password(user_id, "Test@123456", "NewPass@789")

            # 改密后旧 token 失效（token_version 不匹配）
            # 清空进程内缓存，模拟 30s TTL 过期，让 verify_token 回源 DB 校验 token_version
            self._clear_auth_cache()
            assert AuthService.verify_token(access_token) is None

    # ==================== 5. 连续失败 5 次后锁定 ====================

    def test_login_lockout_after_5_fails(self, app):
        """连续 5 次错误密码，第 6 次返回锁定消息"""
        from services.auth import AuthService

        with app.app_context():
            self._create_user("lockout_user")

            # 前 5 次错误密码，每次返回"用户名或密码错误"
            for _ in range(5):
                result, err = AuthService.login("lockout_user", "WrongPass@1")
                assert result is None
                assert "用户名或密码错误" in err

            # 第 6 次应返回限流消息（P2-5: 与密码错误文案统一防枚举）
            result, err = AuthService.login("lockout_user", "WrongPass@1")
            assert result is None
            assert "登录失败次数过多" in err

    # ==================== 6. must_change_password 字段 ====================

    def test_must_change_password_in_response(self, app):
        """创建 must_change_password=True 的用户，login 响应中包含该字段"""
        from services.auth import AuthService

        with app.app_context():
            self._create_user("must_chpwd_user", must_change=True)
            result, err = AuthService.login("must_chpwd_user", "Test@123456")
            assert err is None
            assert "must_change_password" in result
            assert result["must_change_password"] is True

    # ==================== 7. refresh_token 旋转 ====================

    def test_refresh_token_rotation(self, app):
        """login 获取 refresh_token，调用 refresh_token 后旧 refresh 进黑名单"""
        from services.auth import AuthService

        with app.app_context():
            self._create_user("refresh_user")

            # 1. 登录获取 refresh_token
            result, err = AuthService.login("refresh_user", "Test@123456")
            assert err is None
            old_refresh = result["refresh_token"]

            # 2. 用旧 refresh 换取新 token
            new_result, err = AuthService.refresh_token(old_refresh)
            assert err is None
            assert "access_token" in new_result
            assert "refresh_token" in new_result
            new_refresh = new_result["refresh_token"]
            assert new_refresh != old_refresh  # 旋转：新旧 refresh 不同

            # 3. 旧 refresh 应进黑名单，再次使用返回错误
            result2, err2 = AuthService.refresh_token(old_refresh)
            assert result2 is None
            assert err2 is not None  # "refresh_token 已被吊销，请重新登录"

    # ==================== 8. verify_sse_auth 从 query 参数读 token ====================

    def test_verify_sse_auth_with_query_token(self, app):
        """测试 verify_sse_auth 从 query 参数 ?token=xxx 读取 token"""
        from flask import g, request

        from services.auth import AuthService
        from services.rbac import verify_sse_auth

        with app.app_context():
            self._create_user("sse_user")
            result, err = AuthService.login("sse_user", "Test@123456")
            assert err is None
            access_token = result["access_token"]

        # 在请求上下文中模拟 SSE 请求（?token=xxx）
        # TESTING=True 时 verify_sse_auth 直接放行，需临时关闭 TESTING
        original_testing = app.config.get("TESTING")
        app.config["TESTING"] = False
        try:
            with app.test_request_context("/api/v1/aigc/query-stream", query_string={"token": access_token}):
                # 确认无 Authorization 头，从 query 参数读取 token
                assert not request.headers.get("Authorization", "").startswith("Bearer ")
                assert request.args.get("token") == access_token

                result = verify_sse_auth("aigc:read")
                # 返回 None 表示认证通过
                assert result is None
                # g.current_user 应被设置
                assert g.current_user is not None
                assert g.current_user.get("username") == "sse_user"
        finally:
            app.config["TESTING"] = original_testing
