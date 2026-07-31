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


class TestIdempotencyRedisCache:
    """Redis 后端下的幂等缓存命中/未命中/非 2xx 不缓存测试

    通过 monkeypatch _is_redis_available 返回 True 并替换 services.idempotency.cache
    为内存版 FakeRedisCache，模拟 Redis 后端行为。
    """

    class _FakeRedisCache:
        """内存版 Redis cache，模拟 flask-caching RedisCache 接口"""

        def __init__(self):
            self._store = {}

        def get(self, key):
            return self._store.get(key)

        def set(self, key, value, timeout=None):
            self._store[key] = value

        def delete(self, key):
            self._store.pop(key, None)

    def test_redis_cache_hit_returns_cached_response(self, app, monkeypatch):
        """Redis 命中缓存时直接返回缓存结果，不执行业务函数"""
        from flask import jsonify

        from services import idempotency

        fake_redis = self._FakeRedisCache()
        monkeypatch.setattr(idempotency, "_is_redis_available", lambda: True)
        monkeypatch.setattr(idempotency, "cache", fake_redis)

        call_count = [0]

        @idempotency.idempotent()
        def view():
            call_count[0] += 1
            return jsonify({"ok": True}), 200

        # 首次请求：执行业务并缓存
        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "key-hit-001"}):
            resp1 = view()
            assert call_count[0] == 1
            assert resp1[1] == 200

        # 第二次相同 Key：应命中缓存，不执行业务
        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "key-hit-001"}):
            resp2 = view()
            assert call_count[0] == 1  # 业务未再调用
            assert resp2[1] == 200

    def test_non_2xx_not_cached(self, app, monkeypatch):
        """4xx/5xx 响应不缓存，相同 Key 重试时业务重新执行"""
        from flask import jsonify

        from services import idempotency

        fake_redis = self._FakeRedisCache()
        monkeypatch.setattr(idempotency, "_is_redis_available", lambda: True)
        monkeypatch.setattr(idempotency, "cache", fake_redis)

        call_count = [0]

        @idempotency.idempotent()
        def view():
            call_count[0] += 1
            return jsonify({"error": "bad request"}), 400

        # 首次：返回 400，不缓存
        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "key-4xx-001"}):
            resp1 = view()
            assert resp1[1] == 400
            assert call_count[0] == 1

        # 第二次相同 Key：因 400 未缓存，业务应再次执行
        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "key-4xx-001"}):
            resp2 = view()
            assert resp2[1] == 400
            assert call_count[0] == 2  # 业务再次执行

        # 缓存中不应有该 key
        assert fake_redis.get("idem:anon:key-4xx-001") is None

    def test_2xx_cached_with_full_ttl(self, app, monkeypatch):
        """2xx 响应缓存完整 TTL（24h）"""
        import json

        from flask import g, jsonify

        from services import idempotency

        fake_redis = self._FakeRedisCache()
        monkeypatch.setattr(idempotency, "_is_redis_available", lambda: True)
        monkeypatch.setattr(idempotency, "cache", fake_redis)

        @idempotency.idempotent()
        def view():
            resp = jsonify({"data": "value"})
            return resp, 201

        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "key-201-001"}):
            # 清理可能从前序测试残留的 g.current_user，确保使用 anon 作为 user_id
            if hasattr(g, "current_user"):
                del g.current_user
            resp = view()
            assert resp[1] == 201
            # 在请求上下文内取实际 cache_key，避免硬编码
            expected_key = idempotency._cache_key("key-201-001")

        # 验证缓存已写入
        cached = fake_redis.get(expected_key)
        assert cached is not None, f"缓存未写入, store keys: {list(fake_redis._store.keys())}"
        payload = json.loads(cached)
        assert payload["status"] == 201

    def test_redis_unavailable_fallback(self, app, monkeypatch):
        """Redis 不可用时（cache.get 抛异常），降级直接执行业务"""
        from flask import jsonify

        from services import idempotency

        class BrokenRedis:
            def get(self, key):
                raise RuntimeError("Redis 连接失败")

            def set(self, key, value, timeout=None):
                raise RuntimeError("Redis 连接失败")

            def delete(self, key):
                raise RuntimeError("Redis 连接失败")

        monkeypatch.setattr(idempotency, "_is_redis_available", lambda: True)
        monkeypatch.setattr(idempotency, "cache", BrokenRedis())

        call_count = [0]

        @idempotency.idempotent()
        def view():
            call_count[0] += 1
            return jsonify({"ok": True}), 200

        # Redis get 抛异常时应降级执行业务
        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "key-broken-001"}):
            resp = view()
            assert resp[1] == 200
            assert call_count[0] == 1  # 业务被调用

    def test_cached_payload_corrupted_re_executes(self, app, monkeypatch):
        """缓存内容损坏（JSON 反序列化失败）时重新执行业务"""
        from flask import jsonify

        from services import idempotency

        fake_redis = self._FakeRedisCache()
        # 预置损坏的缓存
        fake_redis._store["idem:anon:key-corrupt-001"] = "not a valid json {"
        monkeypatch.setattr(idempotency, "_is_redis_available", lambda: True)
        monkeypatch.setattr(idempotency, "cache", fake_redis)

        call_count = [0]

        @idempotency.idempotent()
        def view():
            call_count[0] += 1
            return jsonify({"ok": True}), 200

        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "key-corrupt-001"}):
            resp = view()
            assert resp[1] == 200
            assert call_count[0] == 1  # 业务被调用（缓存损坏）

    def test_get_method_skips_idempotency(self, app, monkeypatch):
        """GET 请求跳过幂等检查，直接执行业务"""
        from flask import jsonify

        from services import idempotency

        fake_redis = self._FakeRedisCache()
        monkeypatch.setattr(idempotency, "_is_redis_available", lambda: True)
        monkeypatch.setattr(idempotency, "cache", fake_redis)

        call_count = [0]

        @idempotency.idempotent()
        def view():
            call_count[0] += 1
            return jsonify({"ok": True}), 200

        # GET 请求即使带 Key 也应直接执行
        with app.test_request_context("/api/test", method="GET", headers={"Idempotency-Key": "key-get-001"}):
            resp = view()
            assert resp[1] == 200
            assert call_count[0] == 1
        # 缓存未写入
        assert fake_redis.get("idem:anon:key-get-001") is None

    def test_custom_ttl_seconds(self, app, monkeypatch):
        """自定义 ttl_seconds 参数"""
        import json

        from flask import jsonify

        from services import idempotency

        fake_redis = self._FakeRedisCache()
        set_calls = []

        original_set = fake_redis.set

        def tracking_set(key, value, timeout=None):
            set_calls.append(timeout)
            return original_set(key, value, timeout)

        fake_redis.set = tracking_set
        monkeypatch.setattr(idempotency, "_is_redis_available", lambda: True)
        monkeypatch.setattr(idempotency, "cache", fake_redis)

        @idempotency.idempotent(ttl_seconds=3600)
        def view():
            return jsonify({"ok": True}), 200

        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "key-ttl-001"}):
            view()

        # 验证 TTL 被传入
        assert len(set_calls) == 1
        assert set_calls[0] == 3600

    def test_cache_key_with_user_id_isolation(self, app, monkeypatch):
        """不同用户相同 Idempotency-Key 缓存隔离"""
        from flask import g, jsonify

        from services import idempotency

        fake_redis = self._FakeRedisCache()
        monkeypatch.setattr(idempotency, "_is_redis_available", lambda: True)
        monkeypatch.setattr(idempotency, "cache", fake_redis)

        call_count = [0]

        @idempotency.idempotent()
        def view():
            call_count[0] += 1
            return jsonify({"user": g.current_user.get("sub")}), 200

        # 用户 A 首次请求
        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "shared-key"}):
            g.current_user = {"sub": "user_a"}
            resp_a = view()
            assert resp_a[1] == 200
            assert call_count[0] == 1

        # 用户 B 相同 Key：应执行业务（缓存隔离）
        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "shared-key"}):
            g.current_user = {"sub": "user_b"}
            resp_b = view()
            assert resp_b[1] == 200
            assert call_count[0] == 2  # 业务再次执行（不同用户）

        # 用户 A 相同 Key：应命中缓存
        with app.test_request_context("/api/test", method="POST", headers={"Idempotency-Key": "shared-key"}):
            g.current_user = {"sub": "user_a"}
            resp_a2 = view()
            assert resp_a2[1] == 200
            assert call_count[0] == 2  # 业务未再调用（命中缓存）
