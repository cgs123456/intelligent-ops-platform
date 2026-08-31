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
import os
from functools import wraps

from flask import current_app, jsonify, request

from extensions import cache

logger = logging.getLogger(__name__)

# Redis 键前缀与 TTL
_KEY_PREFIX = "idem:"
_TTL_SECONDS = 24 * 3600  # 24 小时
# P1-2: SETNX 占位标记，区分"正在处理"与"已完成缓存"
_PROCESSING_MARKER = "__idem_processing__"
# P0 修复：占位锁独立短 TTL —— 业务执行中进程被杀时锁可自行过期恢复，
# 否则 24h 内所有重试都会 409。成功响应会以完整 TTL 覆盖写。
_PROCESSING_TTL = int(os.getenv("IDEMPOTENCY_PROCESSING_TTL", "300"))


def _anon_namespace():
    """P1-3: 为匿名用户生成命名空间，优先用客户端 IP 隔离，无法识别时返回唯一值使缓存永不命中。

    结果缓存在 g 中，确保同一请求内多次调用 _cache_key 返回相同 key
    （否则 SETNX 占位锁 key 与最终写入 key 不一致，幂等失效）。
    """
    from flask import g

    # 同一请求内复用已生成的命名空间，保证 cache_key 一致
    if hasattr(g, "_idem_anon_ns"):
        return g._idem_anon_ns
    try:
        xff = request.headers.get("X-Forwarded-For", "").strip()
        if xff:
            ns = f"anon:{xff.split(',')[0].strip()}"
        elif request.remote_addr:
            ns = f"anon:{request.remote_addr}"
        else:
            # 无法识别客户端 IP：用固定值（不随机），确保同一请求内 key 一致；
            # 跨请求共享 anon:unknown 命名空间在无 IP 场景下可接受（罕见且无身份混淆）
            ns = "anon:unknown"
    except Exception:
        ns = "anon:unknown"
    g._idem_anon_ns = ns
    return ns


def _cache_key(idem_key):
    """构造 Redis 键名（P0-8: 加入 user_id 防止跨用户数据泄漏）"""
    # P0-8: 从 g.current_user 获取用户身份，避免不同用户用相同 key 时返回他人数据
    user_id = "anon"
    try:
        from flask import g

        if hasattr(g, "current_user") and g.current_user:
            user_id = g.current_user.get("sub", "anon")
    except Exception:
        pass
    # P1-3: 未认证用户共享 "anon" 命名空间有冲突风险，
    # 改用客户端 IP（X-Forwarded-For / remote_addr）作为命名空间隔离
    if user_id == "anon":
        return f"{_KEY_PREFIX}{_anon_namespace()}:{idem_key}"
    return f"{_KEY_PREFIX}{user_id}:{idem_key}"


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

            # P1-2: SETNX 占位锁防 TOCTOU —— 并发请求只有一个能执行业务。
            # P0 修复：原实现 cache.set(..., nx=True)，但 flask-caching 的 set() 签名
            # 只有 (key, value, timeout)，必抛 TypeError 静默降级，锁从未生效。
            # cache.add() 在 Redis 后端就是原子 SET NX EX，SimpleCache 等也支持。
            # 占位锁用短 TTL（默认 300s ≈ 业务最大执行时长）：业务执行中 worker 崩溃时
            # 占位锁可自行过期，客户端重试不会永远 409；成功响应会以完整 TTL 覆盖。
            has_placeholder = False
            try:
                acquired = cache.add(cache_key, _PROCESSING_MARKER, timeout=_PROCESSING_TTL)
                has_placeholder = bool(acquired)
            except Exception as e:
                logger.warning(
                    "Redis 不可用，跳过幂等检查 key=%s err=%s",
                    idem_key[:8],
                    e,
                )
                return f(*args, **kwargs)

            if not has_placeholder:
                # ===== key 已存在：可能是并发 processing 或已完成结果 =====
                try:
                    cached = cache.get(cache_key)
                except Exception as e:
                    logger.warning(
                        "Redis 不可用，跳过幂等检查 key=%s err=%s",
                        idem_key[:8],
                        e,
                    )
                    return f(*args, **kwargs)

                if cached == _PROCESSING_MARKER:
                    # 并发请求正在处理，返回 409 避免重复执行
                    return jsonify({"error": "请求正在处理中，请稍后重试"}), 409

                if cached is not None:
                    try:
                        payload = json.loads(cached)
                        logger.info("幂等命中 key=%s，返回缓存结果", idem_key[:8])
                        return jsonify(payload.get("body")), payload.get("status", 200)
                    except (json.JSONDecodeError, KeyError):
                        logger.warning("幂等缓存反序列化失败 key=%s，重新执行", idem_key[:8])
                # cached 为 None（占位锁已过期）或损坏：重新执行业务（覆盖旧值）

            # 2. 首次请求：执行业务
            response = f(*args, **kwargs)

            # P1-5: 2xx 缓存长 TTL（24h），4xx/5xx 不缓存（P1-11: 避免 4xx 缓存导致合法重试被拒）
            # P1-11 修复：原 4xx 缓存 60s 会导致用户修正参数后重试（相同 Idempotency-Key）命中旧错误，
            #            改为不缓存，让每次参数错误都能重新执行业务校验。
            # P1-2: SETNX 路径下非 2xx 需删除占位锁，否则后续重试会一直命中 processing 返回 409
            # P2-2: 写缓存失败时降级（仅记 warning，不影响业务响应）
            try:
                status_code = (
                    response[1]
                    if isinstance(response, tuple)
                    else (response.status_code if hasattr(response, "status_code") else 200)
                )
                status_int = int(status_code)
                if 200 <= status_int < 300:
                    # 成功响应：缓存完整 TTL（覆盖占位锁）
                    body = response[0].get_json() if hasattr(response[0], "get_json") else response[0]
                    cached_payload = json.dumps(
                        {
                            "status": status_int,
                            "body": body if isinstance(body, (dict, list)) else {"raw": str(body)},
                        },
                        ensure_ascii=False,
                    )
                    cache.set(cache_key, cached_payload, timeout=ttl_seconds)
                elif has_placeholder:
                    # P1-2: 非成功响应删除占位锁，允许客户端修正参数后重试
                    cache.delete(cache_key)
                # P1-11: 4xx 和 5xx 均不缓存，允许用户修正参数后重试
            except Exception as e:
                logger.warning(
                    "写入幂等缓存失败（Redis 可能不可用） key=%s err=%s",
                    idem_key[:8],
                    e,
                )

            return response

        return wrapped

    return decorator
