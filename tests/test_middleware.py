"""中间件测试 — request_id 透传 + 日志过滤器

覆盖：
- 默认 request_id 自动生成（UUID 短格式）
- 客户端 X-Request-Id 透传
- 非法 request_id 格式回退（防日志注入）
- 响应头 X-Request-Id 返回
- RequestIdFilter 注入 request_id 到 LogRecord
- configure_request_id_logging 给 handler 加 filter
"""

import logging

import pytest


@pytest.mark.usefixtures("db_session")
class TestRequestIdMiddleware:
    """request_id 中间件测试"""

    # ==================== 1. 默认自动生成 request_id ====================

    def test_request_id_auto_generated(self, client):
        """无 X-Request-Id 请求头时，自动生成 16 位 hex request_id"""
        resp = client.get("/health/live")
        assert resp.status_code == 200
        rid = resp.headers.get("X-Request-Id")
        assert rid is not None
        # 自动生成的 request_id 为 uuid4().hex[:16]，长度 16
        assert len(rid) == 16
        assert all(c.isalnum() for c in rid)

    # ==================== 2. 客户端透传 request_id ====================

    def test_request_id_passthrough(self, client):
        """客户端通过 X-Request-Id 透传 request_id，响应头原样返回"""
        custom_rid = "my-trace-id-12345"
        resp = client.get("/health/live", headers={"X-Request-Id": custom_rid})
        assert resp.status_code == 200
        assert resp.headers.get("X-Request-Id") == custom_rid

    # ==================== 3. 非法格式 request_id 回退 ====================

    def test_request_id_invalid_format_fallback(self, client):
        """X-Request-Id 含非法字符（空格、特殊符号）时回退为自动生成的 UUID"""
        # 含空格的非法 request_id
        resp = client.get("/health/live", headers={"X-Request-Id": "bad id with spaces"})
        assert resp.status_code == 200
        rid = resp.headers.get("X-Request-Id")
        assert rid is not None
        # 回退后应为 16 位 hex，不是原非法值
        assert rid != "bad id with spaces"
        assert len(rid) == 16

    def test_request_id_too_long_fallback(self, client):
        """X-Request-Id 超过 64 字符时回退为自动生成的 UUID"""
        long_rid = "a" * 100
        resp = client.get("/health/live", headers={"X-Request-Id": long_rid})
        assert resp.status_code == 200
        rid = resp.headers.get("X-Request-Id")
        assert rid is not None
        assert len(rid) == 16

    # ==================== 4. 允许的 request_id 格式（含 - 和 _） ====================

    def test_request_id_with_dash_underscore(self, client):
        """X-Request-Id 含 - 和 _ 时正常透传（合法字符）"""
        custom_rid = "trace-id_abc-123_def"
        resp = client.get("/health/live", headers={"X-Request-Id": custom_rid})
        assert resp.status_code == 200
        assert resp.headers.get("X-Request-Id") == custom_rid

    # ==================== 5. 每个请求分配独立 request_id ====================

    def test_request_id_unique_per_request(self, client):
        """无 X-Request-Id 时，每次请求生成不同的 request_id"""
        resp1 = client.get("/health/live")
        resp2 = client.get("/health/live")
        rid1 = resp1.headers.get("X-Request-Id")
        rid2 = resp2.headers.get("X-Request-Id")
        assert rid1 is not None and rid2 is not None
        assert rid1 != rid2  # 每次请求生成独立 request_id


class TestRequestIdFilter:
    """RequestIdFilter 日志过滤器测试"""

    def test_filter_with_request_context(self, app):
        """有请求上下文时，filter 从 g.request_id 读取并注入到 LogRecord"""
        from middleware import RequestIdFilter

        with app.test_request_context("/_test", headers={"X-Request-Id": "log-trace-001"}):
            from flask import g

            # 模拟 before_request 注入 request_id
            g.request_id = "log-trace-001"

            f = RequestIdFilter()
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0, msg="test msg", args=None, exc_info=None
            )
            result = f.filter(record)
            assert result is True
            assert record.request_id == "log-trace-001"

    def test_filter_without_request_id_attr(self, app):
        """有 app 上下文但 g.request_id 未设置时，filter 注入默认值 '-'"""
        from middleware import RequestIdFilter

        # 推入新的 app context，g 为干净状态（无 request_id 属性）
        with app.app_context():
            from flask import g

            # 确认 g 上没有 request_id
            assert not hasattr(g, "request_id")
            f = RequestIdFilter()
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0, msg="test msg", args=None, exc_info=None
            )
            result = f.filter(record)
            assert result is True
            # getattr(g, "request_id", "-") 返回默认值 "-"
            assert record.request_id == "-"

    def test_filter_always_returns_true(self):
        """filter 永远返回 True（不阻断日志输出）"""
        from middleware import RequestIdFilter

        f = RequestIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0, msg="error", args=None, exc_info=None
        )
        assert f.filter(record) is True


class TestConfigureRequestIdLogging:
    """configure_request_id_logging 配置测试"""

    def test_adds_filter_to_handlers(self):
        """configure_request_id_logging 给 root logger 的 handler 添加 RequestIdFilter"""
        from middleware import RequestIdFilter, configure_request_id_logging

        # 用临时 handler 避免污染全局
        test_handler = logging.StreamHandler()
        root = logging.getLogger()
        root.addHandler(test_handler)
        try:
            configure_request_id_logging()
            # 至少有一个 handler 带 RequestIdFilter
            has_filter = any(isinstance(f, RequestIdFilter) for h in root.handlers for f in h.filters)
            assert has_filter
        finally:
            root.removeHandler(test_handler)

    def test_named_loggers_get_filter(self):
        """configure_request_id_logging 给 app/services/routes/models logger 加 filter"""
        from middleware import RequestIdFilter, configure_request_id_logging

        configure_request_id_logging()
        for name in ("app", "services", "routes", "models"):
            lg = logging.getLogger(name)
            has_filter = any(isinstance(f, RequestIdFilter) for f in lg.filters)
            assert has_filter, f"logger '{name}' 应包含 RequestIdFilter"
