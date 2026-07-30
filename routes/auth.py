"""认证路由 Blueprint（P0-2 + P0 安全加固 + P1-12 HttpOnly cookie）"""

from flask import Blueprint, g, jsonify, make_response, request

from extensions import limiter
from services.auth import AuthService, jwt_required
from services.rbac import require_permission

bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")


def _set_token_cookies(resp, access_token, refresh_token=None):
    """P1-12: 将 token 写入 HttpOnly + Secure + SameSite=Strict cookie。

    - HttpOnly: JS 不可读，防御 XSS 窃取 token
    - Secure: 仅 HTTPS 传输（本地开发 http 会被浏览器拒绝，仅生产启用）
    - SameSite=Strict: 防 CSRF
    - access_token 短期 30min，refresh_token 长期 7d
    向后兼容：token 同时通过 JSON body 返回，前端旧逻辑仍可工作。
    """
    from flask import current_app

    is_prod = current_app.config.get("ENV") == "production"
    # Secure 仅在 HTTPS 下生效；开发环境 HTTP 不设 Secure，否则 cookie 不写入
    secure_flag = is_prod
    samesite = "Strict"
    resp.set_cookie(
        "access_token",
        access_token,
        httponly=True,
        secure=secure_flag,
        samesite=samesite,
        max_age=int(AuthService.ACCESS_TOKEN_EXPIRES.total_seconds()),
        path="/",
    )
    if refresh_token:
        resp.set_cookie(
            "refresh_token",
            refresh_token,
            httponly=True,
            secure=secure_flag,
            samesite=samesite,
            max_age=int(AuthService.REFRESH_TOKEN_EXPIRES.total_seconds()),
            path="/api/v1/auth",  # refresh 仅在 /auth 路径下发送，缩小暴露面
        )
    return resp


def _clear_token_cookies(resp):
    """P1-12: 登出时清除 cookie"""
    resp.delete_cookie("access_token", path="/")
    resp.delete_cookie("refresh_token", path="/api/v1/auth")
    return resp


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
    # P1-12: HttpOnly cookie（向后兼容：JSON body 仍返回 token）
    resp = make_response(jsonify(result), 200)
    _set_token_cookies(resp, result["access_token"], result.get("refresh_token"))
    return resp


@bp.route("/refresh", methods=["POST"])
@limiter.limit("30/minute")  # P0: refresh 也限流，防爆破
def refresh():
    data = request.get_json(force=True, silent=True) or {}
    refresh_tok = data.get("refresh_token", "")
    # P1-12: cookie 优先，Header/body fallback（向后兼容）
    if not refresh_tok:
        refresh_tok = request.cookies.get("refresh_token", "")
    if not refresh_tok:
        return jsonify({"error": "缺少 refresh_token"}), 400
    ip = request.remote_addr or "unknown"
    result, err = AuthService.refresh_token(refresh_tok, ip=ip)
    if err:
        return jsonify({"error": err}), 401
    # P1-12: 刷新 token 时也更新 cookie
    resp = make_response(jsonify(result), 200)
    _set_token_cookies(resp, result["access_token"], result.get("refresh_token"))
    return resp


@bp.route("/logout", methods=["POST"])
def logout():
    """登出：吊销 access + refresh token"""
    auth_header = request.headers.get("Authorization", "")
    access_tok = auth_header[7:] if auth_header.startswith("Bearer ") else None
    # P1-12: cookie fallback
    if not access_tok:
        access_tok = request.cookies.get("access_token")
    data = request.get_json(force=True, silent=True) or {}
    refresh_tok = data.get("refresh_token")
    if not refresh_tok:
        refresh_tok = request.cookies.get("refresh_token")
    if not access_tok and not refresh_tok:
        return jsonify({"error": "无可吊销的 token"}), 400
    ip = request.remote_addr or "unknown"
    revoked_count = AuthService.logout(access_tok, refresh_tok, ip=ip)
    # P1-12: 清除 cookie
    resp = make_response(jsonify({"status": "ok", "revoked_count": revoked_count}), 200)
    _clear_token_cookies(resp)
    return resp


@bp.route("/me")
@require_permission(None)  # P1-14: 仅需认证，不需要特定权限
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
@require_permission(None)  # P1-14: 仅需认证，不需要特定权限
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
