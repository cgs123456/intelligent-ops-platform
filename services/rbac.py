"""RBAC 权限装饰器
P0-2: JWT 认证 + RBAC 权限校验。
- TESTING 模式下跳过认证（兼容测试）
- RBAC_ENABLED=0 时仅做 JWT 认证，不校验权限
- RBAC_ENABLED=1 时做 JWT 认证 + 权限校验
"""

import json
import logging
from functools import wraps

from flask import current_app, g, jsonify, request

from extensions import db
from models.system import Role, User
from services.response import ErrorCode, error_response

logger = logging.getLogger(__name__)


def get_current_user():
    """从 JWT payload（g.current_user）获取当前用户信息。
    需配合 @jwt_required / @require_permission 使用，或在公开接口中返回 None。
    """
    payload = getattr(g, "current_user", None)
    if not payload:
        return None
    user = User.query.filter_by(username=payload.get("username"), is_active=True).first()
    g.current_user_obj = user
    return user


def get_actor_name(fallback=None):
    """P0-11: 获取真实操作人，用于审计留痕（actor/reviewer/created_by）。

    审计链的价值在于操作人不可抵赖：请求通过真实 JWT 认证（payload.sub 非空）时，
    操作人一律取认证身份，忽略请求体传入值，防止客户端伪造 actor 把操作嫁祸他人。
    仅在无真实认证（dev 放行模式 sub=0 / 测试）时回退到请求体传入值。
    """
    payload = getattr(g, "current_user", None)
    if payload and payload.get("sub"):
        username = payload.get("username")
        if username:
            return str(username)
    return fallback or "system"


def get_user_permissions(user):
    """P3-F3: 返回用户权限集合的公共入口（供路由层传递给服务层做业务级审批校验）。"""
    return _collect_permissions_safely(user)


def require_permission(permission):
    """权限校验装饰器：JWT 认证 + RBAC 权限检查。

    行为：
    - TESTING=True：完全跳过（兼容测试）
    - RBAC_ENABLED=False 且无 Authorization 头：放行（dev demo 模式）
    - RBAC_ENABLED=False 但带了 Authorization 头：验证 JWT，但不校验权限
    - RBAC_ENABLED=True：JWT 认证 + 权限校验

    :param permission: 权限字符串（如 "erp:read"）。传 None 或空字符串表示
                       "仅需认证无需特定权限"（P1-14：/me、/change-password 使用）。
    """

    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # 测试模式跳过认证
            if current_app.config.get("TESTING"):
                return f(*args, **kwargs)

            from services.auth import AuthService

            auth_header = request.headers.get("Authorization", "")
            has_token = auth_header.startswith("Bearer ")
            # P1-12: 无 Authorization Header 时回退 HttpOnly cookie
            token = auth_header[7:] if has_token else request.cookies.get("access_token", "")
            has_token = bool(token)

            # P0-5: RBAC_ENABLED=False 时仅在开发环境放行（防 prod 误关 RBAC 绕过认证）
            if not current_app.config.get("RBAC_ENABLED", True):
                if current_app.config.get("ENV") != "development":
                    return error_response(ErrorCode.FORBIDDEN, "RBAC 未启用且非开发环境，拒绝访问")
                g.current_user = {"sub": 0, "username": "dev", "roles": ["admin"]}
                return f(*args, **kwargs)

            # 有 token 必须验证
            if has_token:
                payload = AuthService.verify_token(token)
                if not payload:
                    return jsonify({"error": "Token无效或已过期"}), 401
                g.current_user = payload
                g.tenant_id = payload.get("tid", 1)  # 多租户：回填当前租户

            # 第二步：RBAC 权限校验（仅 RBAC_ENABLED=True 时执行）
            if not current_app.config.get("RBAC_ENABLED"):
                return f(*args, **kwargs)
            # P1-14: permission 为 None/空表示仅需认证，跳过权限校验
            if not permission:
                return f(*args, **kwargs)
            user = get_current_user()
            if not user:
                return jsonify({"error": "用户不存在或已禁用"}), 401
            # P0-9 fix: must_change_password=True blocks all except change-password/me/logout/refresh
            if getattr(user, "must_change_password", False):
                _allowed = {"auth.change_password", "auth.me", "auth.logout", "auth.refresh"}
                if request.endpoint not in _allowed:
                    return jsonify({"error": "Password must be changed before accessing this resource"}), 403
            user_perms = _collect_permissions_safely(user)
            if permission not in user_perms and "*:*" not in user_perms:
                return jsonify({"error": f"无权限：{permission}"}), 403
            return f(*args, **kwargs)

        return wrapped

    return decorator


def verify_sse_auth(permission):
    """SSE 端点认证：EventSource 不支持自定义 Header，改为从 query 参数 ?token=xxx 读取 token。

    复用 AuthService.verify_token 做 JWT 校验，RBAC 开启时同步校验权限。
    同时兼容 Authorization Header（fetch 场景），优先 Header，缺失时回退 query 参数。
    返回 None 表示放行，返回 (Response, status) 表示需立即返回错误。
    注意：日志中不记录 token 值，仅记录是否通过。
    """
    # 测试模式跳过认证
    if current_app.config.get("TESTING"):
        return None

    from services.auth import AuthService

    # 优先 Authorization Header，其次 query 参数 token（兼容 EventSource），最后 HttpOnly cookie
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    else:
        token = request.args.get("token", "").strip()
        # P1-12: cookie fallback（SSE 场景 EventSource 自动携带 cookie）
        if not token:
            token = request.cookies.get("access_token", "")

    has_token = bool(token)

    # dev 模式（RBAC_ENABLED=False）且无 token：放行
    if not current_app.config.get("RBAC_ENABLED") and not has_token:
        return None

    # 有 token 必须验证
    if has_token:
        payload = AuthService.verify_token(token)
        if not payload:
            return jsonify({"error": "Token无效或已过期"}), 401
        g.current_user = payload
        # 多租户：与 require_permission 一致，从 JWT 载荷回填租户上下文
        g.tenant_id = payload.get("tid", 1)

    # 第二步：RBAC 权限校验（仅 RBAC_ENABLED=True 时执行）
    if not current_app.config.get("RBAC_ENABLED"):
        return None
    user = get_current_user()
    if not user:
        return jsonify({"error": "用户不存在或已禁用"}), 401
    # P0-9 fix: must_change_password=True blocks SSE access
    if getattr(user, "must_change_password", False):
        return jsonify({"error": "Password must be changed before accessing this resource"}), 403
    user_perms = _collect_permissions_safely(user)
    if permission not in user_perms and "*:*" not in user_perms:
        return jsonify({"error": f"无权限：{permission}"}), 403
    return None


def _collect_permissions_safely(user):
    """收集用户权限。权限 JSON 解析异常显式记日志，不静默吞。"""
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
    return perms


def init_rbac_data(app):
    """初始化默认角色和 admin 用户（仅首次）

    P0 安全：admin 密码随机生成并打印到日志，标记 must_change_password=True，
    杜绝默认 admin123 弱口令。
    """
    with app.app_context():
        if Role.query.first():
            return
        roles = [
            Role(name="admin", permissions=json.dumps(["*:*"]), description="管理员，全部权限"),
            Role(
                name="operator",
                permissions=json.dumps(
                    [
                        "erp:read",
                        "erp:write",
                        # P2-23: 细粒度权限（保留 erp:write / finance:write 向后兼容）
                        "purchase:write",
                        "sale:write",
                        "transfer:write",
                        "return:write",
                        "recon:create",
                        "recon:import",
                        "recon:match",
                        "recon:close",
                        "rpa:read",
                        "rpa:write",
                        "fde:read",
                        "fde:run",
                        "aigc:read",
                        "aigc:review",
                        "loop:read",
                        "loop:run",
                        "audit:read",
                        # 注意：不含 loop:rollback 和 loop:reset（P0-9）
                    ]
                ),
                description="运营，可操作业务但不可重置/回滚",
            ),
            Role(
                name="viewer",
                permissions=json.dumps(["erp:read", "rpa:read", "fde:read", "aigc:read", "loop:read", "audit:read"]),
                description="查看者，只读",
            ),
        ]
        db.session.add_all(roles)
        db.session.flush()

        # P0: admin 密码随机生成（不再硬编码 admin123）
        from services.auth import generate_random_password
        from services.password_policy import hash_password

        initial_password = generate_random_password(16)
        admin = User(
            username="admin",
            password_hash=hash_password(initial_password),
            display_name="管理员",
            must_change_password=True,
        )
        admin.roles.append(roles[0])
        db.session.add(admin)
        db.session.commit()

        # P0-2: 密码写入受保护文件（权限 0o600），不进日志避免被 ELK 长期采集
        import os

        try:
            instance_dir = app.instance_path
            os.makedirs(instance_dir, exist_ok=True)
            pwd_file = os.path.join(instance_dir, ".initial_password")
            # O_CREAT|O_WRONLY|O_EXCL|O_NOFOLLOW：不存在则创建，已存在则失败，不跟随符号链接
            fd = os.open(pwd_file, os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as fp:
                fp.write(initial_password)
            logger.warning(
                "首次启动：admin 账号已创建，初始密码已写入 %s（权限 0o600）。"
                "请立即读取并删除该文件，然后登录修改密码。",
                pwd_file,
            )
        except FileExistsError:
            logger.warning("admin 账号已创建，.initial_password 文件已存在，请先删除后再重试")
        except Exception as e:
            # 文件写入失败时回退到 stderr 一次性输出（不进日志采集系统）
            import sys

            print(f"[启动提示] admin 初始密码（写入文件失败，仅本次 stderr 输出）：{initial_password}", file=sys.stderr)
            logger.warning("初始密码文件写入失败：%s，已回退到 stderr 输出", e)
