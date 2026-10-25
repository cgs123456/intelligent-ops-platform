"""多租户隔离专项测试。

覆盖：
1. ORM 自动过滤（with_loader_criteria）：租户 A 查不到租户 B 数据，平台管理员全见
2. before_flush 自动填充：显式缺省 tenant_id 按 g.tenant_id 落位
3. 复合唯一约束：跨租户同 sku 合法、同租户同 sku 拒绝
4. AuditLog.tenant_id 默认取当前租户；审计路由按租户过滤
5. DataAgent 原生 SQL 强制改写（tenant_id 注入 + 执行只回本租户行）
6. 租户管理 API：平台管理员创建租户/用户；租户内 *:* 用户被拦
"""

import json

import pytest

from extensions import current_tenant_id
from extensions import db as _db
from models.erp import Product, Supplier
from models.system import AuditLog, Role, User
from models.tenant import DEFAULT_TENANT_ID, Tenant, TenantScoped
from models.warehouse import DwsSalesSkuDaily
from services.auth import AuthService
from services.password_policy import hash_password

PASSWORD = "{{SECRET_Vav9qqpji}}"


@pytest.fixture
def app(app):
    # conftest 的 app fixture 只建 schema，测试自管数据；默认租户行由本 fixture 保证
    with app.app_context():
        if Tenant.query.get(1) is None:
            _db.session.add(Tenant(id=1, code="default", name="默认租户", is_active=True))
        if Tenant.query.get(2) is None:
            _db.session.add(Tenant(id=2, code="acme", name="ACME", is_active=True))
        _db.session.commit()
    yield app


@pytest.fixture(autouse=True)
def cleanup(app):
    yield
    with app.app_context():
        for table in (
            "audit_log",
            "user_roles",
            "sys_user",
            "sys_role",
            "erp_product",
            "erp_supplier",
            "dws_sales_sku_daily",
        ):
            _db.session.execute(_db.text(f"DELETE FROM {table}"))
        _db.session.commit()
        # bulk DELETE 绕过 identity map：不清会把已删对象的陈旧版本串给后续测试
        _db.session.expunge_all()


class TestOrmIsolation:
    def test_tenant_a_cannot_see_tenant_b(self, app):
        with app.test_request_context():
            from flask import g

            g.tenant_id = 1
            _db.session.add(Product(sku="MT-A1", name="A 租户商品", cost_price=10, sale_price=20))
            _db.session.commit()

            g.tenant_id = 2
            assert Product.query.filter_by(sku="MT-A1").count() == 0

            g.tenant_id = None  # 平台管理员
            assert Product.query.filter_by(sku="MT-A1").count() == 1

    def test_before_flush_fills_current_tenant(self, app):
        with app.test_request_context():
            from flask import g

            g.tenant_id = 2
            p = Product(sku="MT-B1", name="B 租户商品", cost_price=1, sale_price=2)
            _db.session.add(p)
            _db.session.commit()
            assert p.tenant_id == 2

    def test_no_request_context_falls_to_default(self, app):
        with app.app_context():  # 无 g.tenant_id → _UNSET → 默认租户
            p = Product(sku="MT-D1", name="默认租户商品", cost_price=1, sale_price=2)
            _db.session.add(p)
            _db.session.commit()
            assert p.tenant_id == DEFAULT_TENANT_ID
            assert current_tenant_id() == DEFAULT_TENANT_ID

    def test_cross_tenant_same_sku_allowed(self, app):
        with app.test_request_context():
            from flask import g

            g.tenant_id = 1
            _db.session.add(Product(sku="MT-SAME", name="A", cost_price=1, sale_price=2))
            _db.session.commit()
            g.tenant_id = 2
            _db.session.add(Product(sku="MT-SAME", name="B", cost_price=1, sale_price=2))
            _db.session.commit()  # 跨租户同 sku 合法

            g.tenant_id = 1
            from sqlalchemy.exc import IntegrityError

            with pytest.raises(IntegrityError):
                _db.session.add(Product(sku="MT-SAME", name="A2", cost_price=1, sale_price=2))
                _db.session.commit()
            _db.session.rollback()


class TestAuditTenant:
    def test_auditlog_default_uses_current_tenant(self, app):
        with app.test_request_context():
            from flask import g

            g.tenant_id = 2
            log = AuditLog(actor="tester", action="create", target_type="x")
            _db.session.add(log)
            _db.session.commit()
            assert log.tenant_id == 2

    def test_platform_admin_audit_gets_default(self, app):
        with app.test_request_context():
            from flask import g

            g.tenant_id = None  # 平台管理员
            log = AuditLog(actor="platform", action="create", target_type="x")
            _db.session.add(log)
            _db.session.commit()
            assert log.tenant_id == DEFAULT_TENANT_ID


class TestDataAgentRewrite:
    def test_rewrite_injects_tenant_predicate(self, app):
        from services.data_agent import DataAgent

        with app.test_request_context():
            from flask import g

            g.tenant_id = 2
            agent = DataAgent()
            sql = agent._validate_sql_ast("SELECT product_name FROM ads_replenishment_suggest")
            assert sql is not None
            assert "tenant_id" in sql and ":tid" in sql

    def test_rewrite_executes_scoped(self, app):
        from services.data_agent import DataAgent

        with app.app_context():
            from datetime import date

            _db.session.add_all(
                [
                    DwsSalesSkuDaily(product_id=101, dt=date(2026, 10, 24), tenant_id=1, sale_qty=5, sale_amount=50),
                    DwsSalesSkuDaily(product_id=202, dt=date(2026, 10, 24), tenant_id=2, sale_qty=7, sale_amount=70),
                ]
            )
            _db.session.commit()

        with app.test_request_context():
            from flask import g

            g.tenant_id = 2
            agent = DataAgent()
            sql = agent._validate_sql_ast("SELECT product_id FROM dws_sales_sku_daily")
            assert sql is not None
            rows, cnt = agent._execute_sql(sql)
            assert cnt > 0
            product_ids = {int(r["product_id"]) for r in rows}
            assert product_ids == {202}  # 只见本租户


class TestTenantApi:
    @pytest.fixture(autouse=True)
    def _restore_app_config(self, app):
        """app 是 session 级 fixture：本类测试临时关 TESTING 走真实 JWT，退出后必须恢复，否则污染后续测试。"""
        saved = {"TESTING": app.config.get("TESTING"), "RBAC_ENABLED": app.config.get("RBAC_ENABLED")}
        yield
        app.config.update(saved)

    def _make_platform_admin(self, app):
        with app.app_context():
            role = Role(
                name=f"plat-{abs(hash('plat')) % 10**8}",
                permissions=json.dumps(["*:*"]),
            )
            _db.session.add(role)
            _db.session.flush()
            user = User(
                username="plat-admin",
                password_hash=hash_password(PASSWORD),
                is_active=True,
                tenant_id=None,  # 平台管理员
            )
            user.roles.append(role)
            _db.session.add(user)
            _db.session.commit()
            result = AuthService.login("plat-admin", PASSWORD)
        return result[0]["access_token"] if isinstance(result, tuple) else result["access_token"]

    def _get(self, app, token):
        app.config.update(TESTING=False, RBAC_ENABLED=True)
        return app.test_client()

    def test_platform_admin_creates_tenant_and_user(self, app):
        # 测试库未经 init_rbac，预建 operator 角色供租户用户接口引用
        with app.app_context():
            if not Role.query.filter_by(name="operator").first():
                _db.session.add(Role(name="operator", permissions=json.dumps(["erp:read"])))
                _db.session.commit()
            _db.session.expunge_all()
        token = self._make_platform_admin(app)
        client = app.test_client()
        app.config.update(TESTING=False, RBAC_ENABLED=True)
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.post(
            "/api/v1/tenants",
            json={"code": "globex", "name": "Globex"},
            headers=headers,
        )
        assert resp.status_code == 201
        tid = resp.get_json()["data"]["id"]

        resp = client.post(
            f"/api/v1/tenants/{tid}/users",
            json={"username": "globex-op", "password": PASSWORD, "role": "operator"},
            headers=headers,
        )
        assert resp.status_code == 201
        with app.app_context():
            u = User.query.filter_by(username="globex-op").first()
            assert u.tenant_id == tid

    def test_tenant_admin_cannot_manage_tenants(self, app):
        with app.app_context():
            role = Role(name="t2-admin", permissions=json.dumps(["*:*"]))
            _db.session.add(role)
            _db.session.flush()
            user = User(
                username="t2-admin",
                password_hash=hash_password(PASSWORD),
                is_active=True,
                tenant_id=2,
            )
            user.roles.append(role)
            _db.session.add(user)
            _db.session.commit()
        _db.session.expunge_all()  # 防跨测试 identity map 陈旧对象串号
        login_res = AuthService.login("t2-admin", PASSWORD)
        token = login_res[0]["access_token"]

        app.config.update(TESTING=False, RBAC_ENABLED=True)
        client = app.test_client()
        resp = client.get("/api/v1/tenants", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403, resp.get_json()  # *:* 过权限关但被平台管理员检查拦下
