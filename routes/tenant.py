"""租户管理 API（平台管理员专用）。

多租户模型说明：
- 平台管理员 = User.tenant_id IS NULL（初始 admin 天然如此），JWT 载荷 tid=null，
  ORM 层不注入租户过滤，可跨租户操作。
- 租户内用户由平台管理员在此创建，username 全局唯一（登录无租户前缀，
  租户归属由账号自身携带）——共库共表架构下的标准取舍。
- 业务角色（admin/operator/viewer）在租户内有效：即便 *:* 权限，
  ORM 过滤仍将其约束在本租户数据内。
"""

from flask import Blueprint, current_app, g, request

from extensions import db
from models.system import Role, User
from models.tenant import Tenant
from services.auth import AuditLog
from services.password_policy import hash_password, validate_password
from services.rbac import get_actor_name, require_permission
from services.response import error_response, success_response

bp = Blueprint("tenant", __name__, url_prefix="/api/v1/tenants")


def _require_platform_admin():
    """租户管理仅平台管理员可用：租户内 admin 持 *:* 也会被此检查拦下。"""
    if getattr(g, "tenant_id", None) is not None:
        return error_response("ERR_SYS_003", "仅平台管理员可管理租户", http_status=403)
    return None


@bp.route("")
@require_permission("tenant:manage")
def list_tenants():
    denied = _require_platform_admin()
    if denied:
        return denied
    tenants = Tenant.query.order_by(Tenant.id).all()
    items = []
    for t in tenants:
        items.append(
            {
                "id": t.id,
                "code": t.code,
                "name": t.name,
                "is_active": t.is_active,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "user_count": User.query.filter(User.tenant_id == t.id).count(),
            }
        )
    return success_response(items)


@bp.route("", methods=["POST"])
@require_permission("tenant:manage")
def create_tenant():
    denied = _require_platform_admin()
    if denied:
        return denied
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()
    if not code or not name:
        return error_response("ERR_SYS_002", "code 与 name 必填"), 400
    if Tenant.query.filter_by(code=code).first():
        return error_response("ERR_SYS_002", f"租户编码 {code} 已存在", http_status=409)
    tenant = Tenant(code=code, name=name, is_active=bool(data.get("is_active", True)))
    db.session.add(tenant)
    db.session.flush()  # 取 tenant.id 供审计行使用
    log = AuditLog(
        actor=get_actor_name(),
        action="create",
        target_type="tenant",
        target_id=code,
        detail=f"创建租户 {code}/{name}",
        tenant_id=tenant.id,
    )
    db.session.add(log)
    db.session.commit()
    return success_response({"id": tenant.id, "code": tenant.code, "name": tenant.name}, http_status=201)


@bp.route("/<int:tenant_id>/users")
@require_permission("tenant:manage")
def list_tenant_users(tenant_id):
    denied = _require_platform_admin()
    if denied:
        return denied
    tenant = Tenant.query.get(tenant_id)
    if not tenant:
        return error_response("ERR_SYS_004", "租户不存在", http_status=404)
    # 租户用户量可控但防御性设上限，避免异常大户拖垮平台管理接口
    users = User.query.filter(User.tenant_id == tenant_id).order_by(User.id).limit(500).all()
    items = [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "is_active": u.is_active,
            "roles": [r.name for r in u.roles],
        }
        for u in users
    ]
    return success_response(items)


@bp.route("/<int:tenant_id>/users", methods=["POST"])
@require_permission("tenant:manage")
def create_tenant_user(tenant_id):
    denied = _require_platform_admin()
    if denied:
        return denied
    tenant = Tenant.query.get(tenant_id)
    if not tenant:
        return error_response("ERR_SYS_004", "租户不存在", http_status=404)
    if not tenant.is_active:
        return error_response("ERR_SYS_002", "租户已停用，不能开用户", http_status=409)

    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role_name = data.get("role") or "operator"
    if not username:
        return error_response("ERR_SYS_002", "username 必填", http_status=400)
    if User.query.filter_by(username=username).first():
        return error_response("ERR_SYS_002", f"用户名 {username} 已存在", http_status=409)
    ok, msg = validate_password(password)
    if not ok:
        return error_response("ERR_SYS_002", msg, http_status=400)
    role = Role.query.filter_by(name=role_name).first()
    if not role:
        return error_response("ERR_SYS_002", f"角色 {role_name} 不存在", http_status=400)

    user = User(
        username=username,
        password_hash=hash_password(password),
        display_name=(data.get("display_name") or username).strip(),
        must_change_password=bool(data.get("must_change_password", False)),
        tenant_id=tenant_id,
    )
    user.roles.append(role)
    db.session.add(user)
    db.session.add(
        AuditLog(
            actor=get_actor_name(),
            action="create",
            target_type="tenant_user",
            target_id=username,
            detail=f"在租户 {tenant.code} 创建用户 {username}（角色 {role_name}）",
            tenant_id=tenant_id,
        )
    )
    db.session.commit()
    current_app.logger.info("[TenantAPI] 租户 %s 新增用户 %s（角色 %s）", tenant.code, username, role_name)
    return success_response({"id": user.id, "username": user.username}, http_status=201)
