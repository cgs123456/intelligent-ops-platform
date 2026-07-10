"""认证路由 Blueprint（P0-2 + P0 安全加固）"""

from flask import Blueprint, g, jsonify, request

from extensions import limiter
from services.auth import AuthService, jwt_required
from services.rbac import require_permission

bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")


@bp.route("/login", methods=["POST"])
@limiter.limit("10/minute")  # P1-1: 每IP每分钟最多10次登录
def login():
    data = request.get_json(force=True, silent=True) or {}
    # P1-4: Schema 校验输入
    from schemas import LoginSchema

    errors = LoginSchema().validate(data)
    if errors:
        return jsonify({"error": "请求参数校验失败", "detail": errors}), 400
    username = data.get("username", "").strip()
    password = data.get("password", "")
    ip = request.remote_addr or "unknown"
    result, err = AuthService.login(username, password, ip=ip)
    if err:
        return jsonify({"error": err}), 401
    return jsonify(result)


@bp.route("/refresh", methods=["POST"])
@limiter.limit("30/minute")  # P0: refresh 也限流，防爆破
def refresh():
    data = request.get_json(force=True, silent=True) or {}
    refresh_tok = data.get("refresh_token", "")
    if not refresh_tok:
        return jsonify({"error": "缺少 refresh_token"}), 400
    ip = request.remote_addr or "unknown"
    result, err = AuthService.refresh_token(refresh_tok, ip=ip)
    if err:
        return jsonify({"error": err}), 401
    return jsonify(result)


@bp.route("/logout", methods=["POST"])
def logout():
    """登出：吊销 access + refresh token"""
    auth_header = request.headers.get("Authorization", "")
    access_tok = auth_header[7:] if auth_header.startswith("Bearer ") else None
    data = request.get_json(force=True, silent=True) or {}
    refresh_tok = data.get("refresh_token")
    if not access_tok and not refresh_tok:
        return jsonify({"error": "无可吊销的 token"}), 400
    ip = request.remote_addr or "unknown"
    revoked_count = AuthService.logout(access_tok, refresh_tok, ip=ip)
    return jsonify({"status": "ok", "revoked_count": revoked_count})


@bp.route("/me")
@require_permission("erp:read")  # P0: 任意已认证用户都应有 erp:read 才能访问
def me():
    """获取当前用户信息（需 JWT）"""
    payload = getattr(g, "current_user", {})
    return jsonify(
        {
            "user_id": payload.get("sub"),
            "username": payload.get("username"),
            "roles": payload.get("roles", []),
            "permissions": payload.get("permissions", []),
        }
    )


@bp.route("/change-password", methods=["POST"])
@require_permission("erp:read")  # 任意已认证用户
def change_password():
    """修改密码：校验旧密码 + 新密码强度"""
    data = request.get_json(force=True, silent=True) or {}
    # P1-4: Schema 校验（含新密码最小长度 8）
    from schemas import ChangePasswordSchema

    errors = ChangePasswordSchema().validate(data)
    if errors:
        return jsonify({"error": "请求参数校验失败", "detail": errors}), 400
    old_pwd = data.get("old_password", "")
    new_pwd = data.get("new_password", "")
    payload = getattr(g, "current_user", {})
    user_id = int(payload.get("sub", 0))
    if not user_id:
        return jsonify({"error": "无法识别用户"}), 400
    try:
        ip = request.remote_addr or "unknown"
        AuthService.change_password(user_id, old_pwd, new_pwd, ip=ip)
        return jsonify({"status": "ok", "message": "密码已修改，请重新登录"})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
