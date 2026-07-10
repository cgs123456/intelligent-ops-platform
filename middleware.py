"""请求中间件：request_id 注入 + 日志格式增强

设计：
- 每个请求分配唯一 request_id（UUID 短格式）
- 客户端可通过 X-Request-Id Header 透传（兼容分布式链路追踪）
- request_id 写入 g 对象 + 响应头 X-Request-Id
- 日志格式自动带上 request_id，便于全链路排查
"""

import logging
import uuid

from flask import current_app, g, request


# 全局 request_id 过滤器（所有日志自动带上）
class RequestIdFilter(logging.Filter):
    """日志过滤器：从 Flask g 对象读取 request_id 注入到 LogRecord"""

    def filter(self, record):
        try:
            from flask import g

            record.request_id = getattr(g, "request_id", "-")
        except Exception:
            record.request_id = "-"
        return True


def init_request_id(app):
    """注册 request_id 中间件"""
    from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: F401

    @app.before_request
    def _attach_request_id():
        # 1. 优先使用客户端透传的 X-Request-Id
        rid = request.headers.get("X-Request-Id", "").strip()
        # 2. 校验格式（防止注入异常字符到日志）
        if not rid or len(rid) > 64 or not all(c.isalnum() or c in "-_" for c in rid):
            rid = uuid.uuid4().hex[:16]
        g.request_id = rid

    @app.after_request
    def _expose_request_id(response):
        # 响应头带 X-Request-Id，便于客户端 + 日志关联
        rid = getattr(g, "request_id", None)
        if rid:
            response.headers["X-Request-Id"] = rid
        return response


def configure_request_id_logging():
    """配置日志过滤器（让所有 handler 自动带上 request_id）
    在 setup_logging 之后调用，避免被覆盖。
    """
    filter_obj = RequestIdFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(filter_obj)
    # 也加到 app logger
    for name in ("app", "services", "routes", "models"):
        lg = logging.getLogger(name)
        for handler in lg.handlers:
            handler.addFilter(filter_obj)
        lg.addFilter(filter_obj)
