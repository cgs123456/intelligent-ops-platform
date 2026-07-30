"""幂等性装饰器测试 — Idempotency-Key 读取、放行逻辑、Key 长度校验、缓存键构造"""

import pytest


@pytest.mark.usefixtures("db_session")
class TestIdempotency:
    """@idempotent 装饰器测试：Key 读取、无 Key 放行、Key 长度校验、非 Redis 放行、缓存键隔离"""

    @staticmethod
    def _make_counted_view():
        """构造带 @idempotent 装饰器的测试视图，返回 (view, call_count_list)"""
        from flask import jsonify

        from services.idempotency import idempotent

        call_count = [0]

        @idempotent()
        def view():
            call_count[0] += 1
            return jsonify({"ok": True}), 200

        return view, call_count

    # ==================== 1. 从 Header 读取 Key ====================

    def test_get_idempotency_key_from_header(self, app):
        """get_idempotency_key 从 Idempotency-Key Header 读取并 strip"""
        from services.idempotency import get_idempotency_key

        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "  abc-123  "}):
            assert get_idempotency_key() == "abc-123"

        # 无 Header 时返回空串
        with app.test_request_context("/api/test", method="POST"):
            assert get_idempotency_key() == ""

    # ==================== 2. 无 Key 时放行（兼容旧客户端） ====================

    def test_no_key_passes_through(self, app):
        """无 Idempotency-Key 时直接执行业务函数，不缓存"""
        view, call_count = self._make_counted_view()

        with app.test_request_context("/api/test", method="POST"):
            response = view()
            # 业务函数被调用一次
            assert call_count[0] == 1
            # 返回正常响应
            assert response[1] == 200

    # ==================== 3. Key 过长返回 400 ====================

    def test_key_too_long_returns_400(self, app):
        """Idempotency-Key 超过 128 字符时返回 400"""
        view, call_count = self._make_counted_view()

        long_key = "x" * 129
        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": long_key}):
            response = view()
            assert response[1] == 400
            # 业务函数未被调用
            assert call_count[0] == 0

    # ==================== 4. 非 Redis 后端直接执行（不缓存） ====================

    def test_non_redis_passes_through(self, app):
        """CACHE_TYPE=simple（非 Redis）时，带 Key 也直接执行业务函数"""
        view, call_count = self._make_counted_view()

        # conftest 中 CACHE_TYPE=simple，非 Redis
        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "test-key-001"}):
            response = view()
            assert response[1] == 200
            # 非 Redis 后端不缓存，业务函数被调用
            assert call_count[0] == 1

    # ==================== 5. 缓存键包含 user_id（防跨用户数据泄漏） ====================

    def test_cache_key_includes_user_id(self, app):
        """_cache_key 加入 user_id 防止不同用户用相同 Key 时返回他人数据"""
        from flask import g

        from services.idempotency import _cache_key

        with app.test_request_context("/api/test"):
            # 模拟用户A
            g.current_user = {"sub": "user_a_id"}
            key_a = _cache_key("shared-key")
            assert "user_a_id" in key_a
            assert "shared-key" in key_a

            # 模拟用户B
            g.current_user = {"sub": "user_b_id"}
            key_b = _cache_key("shared-key")
            assert "user_b_id" in key_b
            assert "shared-key" in key_b

            # 不同用户的缓存键不同
            assert key_a != key_b

        # 无 g.current_user 时用 "anon" 兜底
        with app.test_request_context("/api/test"):
            key_anon = _cache_key("anon-key")
            assert "anon" in key_anon
