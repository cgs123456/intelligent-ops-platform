"""JWT 认证服务（P0 安全加固版）

特性：
- access_token 2h、refresh_token 7d，均带 jti（唯一 ID），支持吊销
- refresh token 旋转：每次刷新签发新 refresh_token，旧 refresh 进黑名单
- logout 黑名单：登出后 access+refresh 都进黑名单，无法继续使用
- 登录失败 5 次 / 5 分钟锁定
- 登录成功也写审计日志
- 首次登录随机生成的 admin 密码，标记 must_change_password=True
- 权限 JSON 解析异常显式记日志，不静默吞
"""

import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request
from werkzeug.security import check_password_hash

from extensions import db
from models.system import AuditLog, TokenBlacklist, User

logger = logging.getLogger(__name__)


def _now_utc():
    return datetime.now(timezone.utc)


def _to_epoch(dt):
    """datetime → JWT exp（秒，整数）"""
    return int(dt.timestamp())


class AuthService:
    """JWT 认证服务"""

    ACCESS_TOKEN_EXPIRES = timedelta(minutes=30)  # P2-6: 从 2h 缩短到 30 分钟，配合前端自动刷新
    REFRESH_TOKEN_EXPIRES = timedelta(days=7)
    MAX_LOGIN_ATTEMPTS = 5
    LOCKOUT_DURATION = 300  # 秒
    ALGORITHM = "HS256"

    # P0-5: 缓存改为 Redis 优先（多 worker 共享），进程内 dict 作为 fallback
    # 原 P1 进程内缓存在多 worker 下改密后 30s 窗口内旧 token 仍有效，迁 Redis 后窗口降到 0
    _token_cache = {}  # fallback: {jti: (expires_at, is_blacklisted)}
    _user_cache = {}  # fallback: {user_id: (expires_at, token_version)}
    _CACHE_TTL = 30.0
    _CACHE_MAXSIZE = 10000

    # ============ Token 签发 ============

    @staticmethod
    def _encode(expires_delta, token_type, user):
        """统一签发 token：注入 jti 用于黑名单追踪"""
        now = _now_utc()
        exp = now + expires_delta
        full_payload = {
            "sub": str(user.id),
            "username": user.username,
            "iat": _to_epoch(now),
            "exp": _to_epoch(exp),
            "type": token_type,
            "jti": uuid.uuid4().hex,
            "token_ver": user.token_version or 0,  # P0-1: 写入 token_version，改密后递增使旧 token 失效
        }
        if token_type == "access":
            full_payload["roles"] = [r.name for r in user.roles]
            full_payload["permissions"] = AuthService._collect_permissions(user)
        token = jwt.encode(full_payload, current_app.config["SECRET_KEY"], algorithm=AuthService.ALGORITHM)
        return token, full_payload["jti"], exp

    @staticmethod
    def login(username, password, ip=None):
        """用户登录，返回 access_token + refresh_token"""
        # 1. 锁定检查（P1-2: 优先用 Redis 计数器，fallback 查 AuditLog）
        recent_fails = AuthService._get_login_fail_count(username)
        if recent_fails >= AuthService.MAX_LOGIN_ATTEMPTS:
            return None, f"账号已被锁定，请 {AuthService.LOCKOUT_DURATION // 60} 分钟后重试"

        # 2. 用户校验
        user = User.query.filter_by(username=username, is_active=True).first()
        if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
            db.session.add(
                AuditLog(
                    actor=username,
                    action="login_failed",
                    target_type="auth",
                    detail="密码错误",
                    ip=ip,
                )
            )
            db.session.commit()
            AuthService._incr_login_fail_count(username)  # P1-2: Redis 计数器递增
            return None, "用户名或密码错误"

        # 3. 签发 access + refresh
        access_token, access_jti, _ = AuthService._encode(AuthService.ACCESS_TOKEN_EXPIRES, "access", user)
        refresh_token, refresh_jti, _ = AuthService._encode(AuthService.REFRESH_TOKEN_EXPIRES, "refresh", user)

        # 4. 登录成功审计
        db.session.add(
            AuditLog(
                actor=user.username,
                action="login_success",
                target_type="auth",
                target_id=str(user.id),
                detail=f"jti={access_jti[:8]}",
                ip=ip,
            )
        )
        db.session.commit()
        AuthService._clear_login_fail_count(username)  # P1-2: 登录成功清除计数器

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": int(AuthService.ACCESS_TOKEN_EXPIRES.total_seconds()),
            "must_change_password": bool(user.must_change_password),
            "user": {
                "id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "roles": [r.name for r in user.roles],
            },
        }, None

    @staticmethod
    def refresh_token(refresh_tok, ip=None):
        """用 refresh_token 换取新的 access + refresh（旋转 refresh）"""
        try:
            payload = jwt.decode(refresh_tok, current_app.config["SECRET_KEY"], algorithms=[AuthService.ALGORITHM])
            if payload.get("type") != "refresh":
                return None, "无效的 refresh_token"

            # 检查黑名单
            jti = payload.get("jti")
            if jti and TokenBlacklist.query.filter_by(jti=jti).first():
                return None, "refresh_token 已被吊销，请重新登录"

            user = db.session.get(User, int(payload["sub"]))
            if not user or not user.is_active:
                return None, "用户不存在或已禁用"

            # 旋转：签发新 access + 新 refresh，旧 refresh 进黑名单
            access_token, _, _ = AuthService._encode(AuthService.ACCESS_TOKEN_EXPIRES, "access", user)
            new_refresh_token, new_refresh_jti, _ = AuthService._encode(
                AuthService.REFRESH_TOKEN_EXPIRES, "refresh", user
            )

            # 旧 refresh 进黑名单
            db.session.add(
                TokenBlacklist(
                    jti=jti,
                    token_type="refresh",
                    user_id=user.id,
                    expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
                )
            )
            db.session.add(
                AuditLog(
                    actor=user.username,
                    action="token_refresh",
                    target_type="auth",
                    ip=ip,
                    detail=f"new_jti={new_refresh_jti[:8]}",
                )
            )
            db.session.commit()

            return {
                "access_token": access_token,
                "refresh_token": new_refresh_token,  # 返回新 refresh
                "token_type": "Bearer",
                "expires_in": int(AuthService.ACCESS_TOKEN_EXPIRES.total_seconds()),
            }, None
        except jwt.ExpiredSignatureError:
            return None, "refresh_token 已过期，请重新登录"
        except jwt.InvalidTokenError:
            return None, "无效的 refresh_token"

    @staticmethod
    def _prune_cache(cache):
        """缓存超限时清理：先删过期项，仍超限则删 expires_at 最小（最旧）的项。"""
        if len(cache) < AuthService._CACHE_MAXSIZE:
            return
        now = time.time()
        for k in [k for k, v in cache.items() if v[0] <= now]:
            del cache[k]
        while len(cache) >= AuthService._CACHE_MAXSIZE and cache:
            del cache[min(cache, key=lambda k: cache[k][0])]

    @staticmethod
    def _redis_get(key):
        """P0-5: 从 Redis 读取缓存，返回值或 None。非 Redis 模式返回 None。"""
        try:
            from extensions import cache

            if current_app.config.get("CACHE_TYPE") == "redis":
                return cache.get(key)
        except Exception:
            pass
        return None

    @staticmethod
    def _redis_set(key, value, ttl=None):
        """P0-5: 写入 Redis 缓存。非 Redis 模式跳过。"""
        try:
            from extensions import cache

            if current_app.config.get("CACHE_TYPE") == "redis":
                cache.set(key, value, timeout=ttl or int(AuthService._CACHE_TTL))
        except Exception:
            pass

    @staticmethod
    def _redis_delete(key):
        """P0-5: 删除 Redis 缓存键（改密/登出时调用）。"""
        try:
            from extensions import cache

            if current_app.config.get("CACHE_TYPE") == "redis":
                cache.delete(key)
        except Exception:
            pass

    @staticmethod
    def verify_token(token):
        """校验 access_token：返回 payload 或 None。检查黑名单 + token_version（Redis 缓存优先）。"""
        try:
            payload = jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=[AuthService.ALGORITHM])
            if payload.get("type") != "access":
                return None
            now = time.time()
            # 1. 黑名单检查（Redis 缓存优先，fallback 进程内 dict + DB）
            jti = payload.get("jti")
            if jti:
                redis_key = f"tok_bl:{jti}"
                redis_val = AuthService._redis_get(redis_key)
                if redis_val is not None:
                    # Redis 命中：1=黑名单，0=有效
                    if redis_val in ("1", 1, b"1"):
                        return None
                else:
                    # fallback 进程内 dict
                    cached = AuthService._token_cache.get(jti)
                    if cached and now < cached[0]:
                        if cached[1]:  # is_blacklisted
                            return None
                    else:
                        is_blacklisted = bool(TokenBlacklist.query.filter_by(jti=jti).first())
                        # 写 Redis + 进程内
                        AuthService._redis_set(redis_key, "1" if is_blacklisted else "0")
                        AuthService._prune_cache(AuthService._token_cache)
                        AuthService._token_cache[jti] = (now + AuthService._CACHE_TTL, is_blacklisted)
                        if is_blacklisted:
                            return None
            # 2. token_version 校验（Redis 缓存优先，改密后立即失效）
            user_id = payload.get("sub")
            if user_id:
                uid = int(user_id)
                redis_ukey = f"tok_ver:{uid}"
                redis_uval = AuthService._redis_get(redis_ukey)
                if redis_uval is not None:
                    try:
                        user_ver = int(redis_uval)
                    except (ValueError, TypeError):
                        user_ver = None
                else:
                    # fallback 进程内 dict
                    cached_user = AuthService._user_cache.get(uid)
                    if cached_user and now < cached_user[0]:
                        user_ver = cached_user[1]
                    else:
                        user = db.session.get(User, uid)
                        if not user or not user.is_active:
                            return None
                        user_ver = user.token_version
                        # 写 Redis + 进程内
                        AuthService._redis_set(redis_ukey, str(user_ver))
                        AuthService._prune_cache(AuthService._user_cache)
                        AuthService._user_cache[uid] = (now + AuthService._CACHE_TTL, user_ver)
                if payload.get("token_ver", 0) != user_ver:
                    return None  # 旧 token 已因改密失效
            return payload
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None

    @staticmethod
    def logout(access_tok, refresh_tok=None, ip=None):
        """登出：把 access + refresh 加入黑名单"""
        revoked = []
        logout_username = "unknown"  # P0-2: 预定义变量，避免 dir() 检查失效
        for tok in [access_tok, refresh_tok]:
            if not tok:
                continue
            try:
                payload = jwt.decode(
                    tok,
                    current_app.config["SECRET_KEY"],
                    algorithms=[AuthService.ALGORITHM],
                    options={"verify_exp": False},
                )
                logout_username = payload.get("username", "unknown")  # P0-2: 从成功解析的 payload 取 username
                jti = payload.get("jti")
                if jti and not TokenBlacklist.query.filter_by(jti=jti).first():
                    db.session.add(
                        TokenBlacklist(
                            jti=jti,
                            token_type=payload.get("type", "access"),
                            user_id=int(payload.get("sub", 0)) if payload.get("sub") else None,
                            expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
                        )
                    )
                    revoked.append(jti)
            except jwt.InvalidTokenError:
                continue
        if revoked:
            db.session.add(
                AuditLog(
                    actor=logout_username,  # P0-2: 直接使用变量，不再用 dir() 检查
                    action="logout",
                    target_type="auth",
                    ip=ip,
                    detail=f"revoked_jti_count={len(revoked)}",
                )
            )
            db.session.commit()
        return len(revoked)

    @staticmethod
    def change_password(user_id, old_password, new_password, ip=None):
        """修改密码：校验旧密码 → 校验新密码强度 → 更新 + 吊销所有旧 token"""
        from services.password_policy import hash_password, validate_password

        user = db.session.get(User, user_id)
        if not user:
            raise ValueError("用户不存在")
        if not check_password_hash(user.password_hash, old_password):
            raise ValueError("旧密码错误")
        ok, msg = validate_password(new_password)
        if not ok:
            raise ValueError(msg)
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        # P0-1: 吊销该用户所有现存 token（强制重新登录）
        # 修复：原代码错误地删除了黑名单记录（含义完全相反），改为将该用户所有有效 token 加入黑名单
        AuthService._revoke_all_user_tokens(user_id)
        db.session.add(
            AuditLog(
                actor=user.username,
                action="password_changed",
                target_type="auth",
                target_id=str(user.id),
                ip=ip,
            )
        )
        db.session.commit()

    @staticmethod
    def _collect_permissions(user):
        """收集用户所有角色的权限。权限 JSON 解析异常显式记日志，不静默吞。"""
        perms = set()
        for role in user.roles:
            if role.permissions:
                try:
                    perms.update(json.loads(role.permissions))
                except (json.JSONDecodeError, TypeError) as e:
                    logger.error(
                        "角色权限 JSON 解析失败 role=%s user=%s err=%s，" "该角色权限视为空集（拒绝授权更安全）",
                        role.name,
                        user.username,
                        e,
                    )
        return list(perms)

    # ============ P0-1: 改密后吊销所有旧 token ============

    @staticmethod
    def _revoke_all_user_tokens(user_id):
        """改密后递增 token_version，使该用户所有旧 token 失效（P0-1 真正修复）。

        原理：JWT 签发时写入 token_ver=user.token_version，
        verify_token 校验时比对 payload.token_ver 与 user.token_version，
        改密时递增 token_version，旧 token 的 token_ver 不匹配即失效。
        失败时抛异常让外层事务回滚，避免密码已改但 token_version 未递增。
        """
        user = db.session.get(User, user_id)
        if user:
            user.token_version = (user.token_version or 0) + 1
            db.session.flush()
            # P0-5: 改密后清除 Redis 缓存，确保所有 worker 立即生效
            AuthService._redis_delete(f"tok_ver:{user_id}")
            logger.info("用户 %s 改密后 token_version 递增，所有旧 token 已失效", user_id)
        else:
            raise ValueError(f"用户不存在 user_id={user_id}，无法递增 token_version")

    # ============ P1-2: 登录失败计数器（Redis 优先，AuditLog fallback） ============

    @staticmethod
    def _get_login_fail_count(username):
        """获取最近锁定窗口内的登录失败次数。优先用 Redis，fallback 查 AuditLog。"""
        # 优先用 Redis 计数器
        try:
            from extensions import cache

            if current_app.config.get("CACHE_TYPE") == "redis":
                key = f"login_fail:{username}"
                count = cache.get(key)
                return int(count) if count else 0
        except Exception:
            pass

        # fallback: 查 AuditLog（原逻辑）
        recent_fails = AuditLog.query.filter(
            AuditLog.actor == username,
            AuditLog.action == "login_failed",
            AuditLog.created_at >= datetime.now() - timedelta(seconds=AuthService.LOCKOUT_DURATION),
        ).count()
        return recent_fails

    @staticmethod
    def _incr_login_fail_count(username):
        """递增登录失败计数器。Redis 可用时写入 Redis，否则仅靠 AuditLog 记录。"""
        try:
            from extensions import cache

            if current_app.config.get("CACHE_TYPE") == "redis":
                key = f"login_fail:{username}"
                count = cache.get(key)
                count = int(count) + 1 if count else 1
                cache.set(key, count, timeout=AuthService.LOCKOUT_DURATION)
        except Exception:
            pass  # AuditLog 已记录，fallback 路径可用

    @staticmethod
    def _clear_login_fail_count(username):
        """登录成功后清除失败计数器。"""
        try:
            from extensions import cache

            if current_app.config.get("CACHE_TYPE") == "redis":
                cache.delete(f"login_fail:{username}")
        except Exception:
            pass


def jwt_required(f):
    """JWT 认证装饰器：校验 Authorization: Bearer <token>，无 Header 时回退 HttpOnly cookie（P1-12）"""

    @wraps(f)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            # P1-12: 回退 HttpOnly cookie
            token = request.cookies.get("access_token", "")
        if not token:
            return jsonify({"error": "缺少认证Token，请先登录"}), 401
        payload = AuthService.verify_token(token)
        if not payload:
            return jsonify({"error": "Token无效或已过期"}), 401
        g.current_user = payload
        return f(*args, **kwargs)

    return wrapped


def generate_random_password(length=16):
    """生成随机强密码（首字母大写以满足策略）"""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        # 确保含大写+小写+数字
        if any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw):
            return pw
