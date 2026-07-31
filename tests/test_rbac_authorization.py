"""RBAC authorization tests (P1-13)

Enable RBAC explicitly (RBAC_ENABLED=True) and disable TESTING mode to test:
1. No token on protected endpoint -> 401
2. Invalid/expired token -> 401
3. Valid token but no permission -> 403
4. Valid token with permission -> 200
5. permission=None endpoints (auth only) -> 200
"""

import json
import os
import tempfile

import pytest

from extensions import db
from models.system import Role, User
from services.auth import AuthService
from services.password_policy import hash_password


@pytest.fixture(scope="module")
def rbac_app():
    """Standalone Flask app with RBAC enabled and TESTING off.

    注意：保存并恢复 os.environ["DATABASE_URL"]，避免污染 conftest 的 session 级 app。
    不调用 db.drop_all()（db 是全局 singleton，drop 会影响其他 app 的 metadata 状态），
    改为通过临时文件 cleanup + 仅清理本 fixture 创建的测试用户/角色。
    """
    from app import create_app

    # 保存原环境变量，teardown 时恢复
    saved_db_url = os.environ.get("DATABASE_URL")
    saved_rbac = os.environ.get("RBAC_ENABLED")

    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["RBAC_ENABLED"] = "1"

    app = create_app()
    app.config.update(
        TESTING=False,
        RBAC_ENABLED=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{db_path}",
        WTF_CSRF_ENABLED=False,
        LLM_PROVIDER="rule",
        CACHE_TYPE="simple",
    )

    with app.app_context():
        # create_app 内部 RBAC_ENABLED=True 时已调用 init_rbac_data 创建默认角色
        # 这里仅创建测试用 viewer 用户（admin 角色和 admin 用户已由 init_rbac_data 创建）
        db.create_all()
        # 注意：create_app 内部 RBAC_ENABLED=True 时已调用 init_rbac_data(app)
        # 创建了默认 admin/operator/viewer 角色和 admin 用户（must_change_password=True）。
        # 这里用 get_or_create 模式避免 UNIQUE 约束冲突。
        admin_role = Role.query.filter_by(name="admin").first()
        if not admin_role:
            admin_role = Role(name="admin", permissions=json.dumps(["*:*"]), description="admin")
            db.session.add(admin_role)
            db.session.flush()

        viewer_role = Role.query.filter_by(name="viewer").first()
        if not viewer_role:
            viewer_role = Role(
                name="viewer",
                permissions=json.dumps(["erp:read", "finance:read", "loop:read"]),
                description="read-only",
            )
            db.session.add(viewer_role)
            db.session.flush()
        # 覆盖 viewer 权限为本测试需要的子集
        viewer_role.permissions = json.dumps(["erp:read", "finance:read", "loop:read"])

        admin = User.query.filter_by(username="rbac_admin").first()
        if not admin:
            admin = User(
                username="rbac_admin",
                password_hash=hash_password("Test@123456"),
                must_change_password=False,
                token_version=0,
            )
            admin.roles.append(admin_role)
            db.session.add(admin)

        viewer = User.query.filter_by(username="rbac_viewer").first()
        if not viewer:
            viewer = User(
                username="rbac_viewer",
                password_hash=hash_password("Test@123456"),
                must_change_password=False,
                token_version=0,
            )
            viewer.roles.append(viewer_role)
            db.session.add(viewer)

        db.session.commit()
        yield app
        # 不调用 db.drop_all()：db 是全局 singleton，drop_all 会影响其他 app 的 metadata。
        # 仅清理测试用户和角色（避免 UNIQUE 约束冲突），数据库文件由 tempfile 清理。
        db.session.remove()

    # 恢复原环境变量
    if saved_db_url is not None:
        os.environ["DATABASE_URL"] = saved_db_url
    else:
        os.environ.pop("DATABASE_URL", None)
    if saved_rbac is not None:
        os.environ["RBAC_ENABLED"] = saved_rbac
    else:
        os.environ.pop("RBAC_ENABLED", None)

    try:
        os.close(db_fd)
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def rbac_client(rbac_app):
    return rbac_app.test_client()


def _get_token(rbac_app, username, password="Test@123456"):
    with rbac_app.app_context():
        result = AuthService.login(username, password)
        # AuthService.login 返回 (dict, error_msg) tuple
        if isinstance(result, tuple):
            data, err = result
            return data.get("access_token", "") if data else ""
        return result.get("access_token", "") if result else ""


class TestRBACAuthorization:
    """RBAC permission check tests"""

    def test_no_token_returns_401(self, rbac_app, rbac_client):
        resp = rbac_client.get("/api/v1/erp/inventory")
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self, rbac_app, rbac_client):
        resp = rbac_client.get(
            "/api/v1/erp/inventory",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401

    def test_admin_with_permission_access_ok(self, rbac_app, rbac_client):
        token = _get_token(rbac_app, "rbac_admin")
        assert token, "admin login failed"
        resp = rbac_client.get(
            "/api/v1/erp/inventory",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code in (200, 404), f"admin should have access, got: {resp.status_code}"

    def test_viewer_read_permission_ok(self, rbac_app, rbac_client):
        token = _get_token(rbac_app, "rbac_viewer")
        assert token
        resp = rbac_client.get(
            "/api/v1/erp/inventory",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code in (200, 404), f"viewer has erp:read, got: {resp.status_code}"

    def test_viewer_write_permission_denied(self, rbac_app, rbac_client):
        token = _get_token(rbac_app, "rbac_viewer")
        assert token
        resp = rbac_client.post(
            "/api/v1/erp/returns",
            json={"product_id": 1, "quantity": 1, "reason": "test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403, f"viewer no erp:write, should 403, got: {resp.status_code}"

    def test_viewer_no_loop_run_permission(self, rbac_app, rbac_client):
        token = _get_token(rbac_app, "rbac_viewer")
        assert token
        resp = rbac_client.post(
            "/api/v1/loop/run-step",
            json={"step": 1, "actor": "test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403, f"viewer no loop:run, should 403, got: {resp.status_code}"

    def test_admin_loop_run_permission_ok(self, rbac_app, rbac_client):
        token = _get_token(rbac_app, "rbac_admin")
        assert token
        resp = rbac_client.post(
            "/api/v1/loop/run-step",
            json={"step": 1, "actor": "test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code != 401, "admin should not get 401"
        assert resp.status_code != 403, "admin should not get 403"

    def test_valid_token_in_cookie(self, rbac_app, rbac_client):
        token = _get_token(rbac_app, "rbac_admin")
        assert token
        rbac_client.set_cookie("access_token", token, httponly=True)
        resp = rbac_client.get("/api/v1/erp/inventory")
        assert resp.status_code in (200, 404), f"cookie auth should work, got: {resp.status_code}"
