"""幂等性服务（基于 Idempotency-Key Header + Redis 缓存）

设计：
- 客户端在 POST/PUT/DELETE 请求时携带 `Idempotency-Key` Header（UUID 或任意唯一字符串）
- 服务端首次请求时执行业务，把响应体与状态码缓存到 Redis 24h
- 第二次相同 Key 的请求直接返回缓存结果，不再执行业务
- 缺少 Header 时按原逻辑放行（兼容旧客户端），但建议前端统一带上

为什么不强制：
- 强制会破坏 GET / 简单调用，且旧客户端会立即失败
- 默认放行 + 推荐前端使用，渐进迁移
"""

import json
import logging
from functools import wraps

from flask import current_app, jsonify, request

from extensions import cache

logger = logging.getLogger(__name__)

# Redis 键前缀与 TTL
_KEY_PREFIX = "idem:"
_TTL_SECONDS = 24 * 3600  # 24 小时


def _cache_key(idem_key):
    """构造 Redis 键名"""
    return f"{_KEY_PREFIX}{idem_key}"


def _is_redis_available():
    """判断 cache 后端是否为 Redis（simple 不支持分布式幂等）"""
    return current_app.config.get("CACHE_TYPE") == "redis"


def get_idempotency_key():
    """从请求头读取 Idempotency-Key"""
    return request.headers.get("Idempotency-Key", "").strip()


def idempotent(ttl_seconds=_TTL_SECONDS):
    """幂等装饰器：仅作用于 POST/PUT/PATCH/DELETE

    用法：
        @bp.route('/returns', methods=['POST'])
        @require_permission('erp:write')
        @idempotent()
        def create_return():
            ...
    """

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # 仅对写操作生效
            if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
                return f(*args, **kwargs)

            idem_key = get_idempotency_key()
            # 无 Key：放行（兼容旧客户端）
            if not idem_key:
                return f(*args, **kwargs)

            # 校验 Key 长度（防滥用）
            if len(idem_key) > 128:
                return jsonify({"error": "Idempotency-Key 过长（≤128）"}), 400

            # 非 Redis 后端：仅记日志，不缓存（dev 模式不要求幂等）
            if not _is_redis_available():
                logger.debug("cache 非 Redis，Idempotency-Key=%s 未缓存", idem_key[:8])
                return f(*args, **kwargs)

            cache_key = _cache_key(idem_key)

            # 1. 命中缓存：直接返回
            cached = cache.get(cache_key)
            if cached is not None:
                try:
                    payload = json.loads(cached)
                    logger.info("幂等命中 key=%s，返回缓存结果", idem_key[:8])
                    return jsonify(payload.get("body")), payload.get("status", 200)
                except (json.JSONDecodeError, KeyError):
                    logger.warning("幂等缓存反序列化失败 key=%s，重新执行", idem_key[:8])

            # 2. 首次请求：执行业务
            response = f(*args, **kwargs)

            # 仅缓存成功的响应（2xx），失败响应不缓存
            try:
                status_code = (
                    response[1]
                    if isinstance(response, tuple)
                    else (response.status_code if hasattr(response, "status_code") else 200)
                )
                if 200 <= int(status_code) < 300:
                    body = response[0].get_json() if hasattr(response[0], "get_json") else response[0]
                    cached_payload = json.dumps(
                        {
                            "status": int(status_code),
                            "body": body if isinstance(body, (dict, list)) else {"raw": str(body)},
                        },
                        ensure_ascii=False,
                    )
                    cache.set(cache_key, cached_payload, timeout=ttl_seconds)
            except Exception as e:
                logger.warning("写入幂等缓存失败 key=%s err=%s", idem_key[:8], e)

            return response

        return wrapped

    return decorator
