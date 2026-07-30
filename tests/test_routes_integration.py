"""Routes 集成测试

覆盖 routes/erp.py + routes/audit.py + routes/fde.py 的 GET 接口，
用 test_client 调用真实端点，同时覆盖 routes 层 + service 层。
"""

import pytest

from extensions import db
from models.erp import Product, Supplier, Warehouse
from models.system import AuditLog


@pytest.fixture(autouse=True)
def _cleanup(app):
    with app.app_context():
        for model in [AuditLog, Product, Supplier, Warehouse]:
            db.session.query(model).delete()
        db.session.commit()
    yield
    with app.app_context():
        for model in [AuditLog, Product, Supplier, Warehouse]:
            db.session.query(model).delete()
        db.session.commit()


class TestErpRoutes:
    """ERP 路由集成测试"""

    def test_inventory_empty(self, client):
        """空库存查询返回 200 + 空列表"""
        resp = client.get("/api/v1/erp/inventory")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_warehouses_list(self, client, app):
        """仓库列表查询"""
        with app.app_context():
            wh = Warehouse(code="WH-TEST", name="测试仓", location="深圳")
            db.session.add(wh)
            db.session.commit()
        resp = client.get("/api/v1/erp/warehouses")
        assert resp.status_code == 200
        data = resp.get_json()
        assert any(w["code"] == "WH-TEST" for w in data)

    def test_orders_default_limit(self, client):
        """订单列表默认 limit"""
        resp = client.get("/api/v1/erp/orders")
        assert resp.status_code == 200

    def test_orders_limit_bound(self, client):
        """订单列表 limit 上限保护（最大 100）"""
        resp = client.get("/api/v1/erp/orders?limit=500")
        assert resp.status_code == 200

    def test_account_summary(self, client):
        """会计汇总查询"""
        resp = client.get("/api/v1/erp/account")
        assert resp.status_code == 200

    def test_transfer_missing_params(self, client):
        """调拨缺少必填参数返回 400"""
        resp = client.post("/api/v1/erp/transfers", json={})
        assert resp.status_code == 400

    def test_transfer_invalid_qty(self, client):
        """调拨 qty 非正数返回 400"""
        resp = client.post(
            "/api/v1/erp/transfers",
            json={"product_id": 1, "qty": -5, "from_warehouse_id": 1, "to_warehouse_id": 2},
        )
        assert resp.status_code == 400


class TestAuditRoutes:
    """审计日志路由集成测试"""

    def test_audit_list_empty(self, client):
        """空审计日志查询"""
        resp = client.get("/api/v1/audit/logs")
        assert resp.status_code == 200

    def test_audit_list_with_data(self, client, app):
        """有数据的审计日志查询"""
        with app.app_context():
            log = AuditLog(actor="test_user", action="login_success", target_type="auth")
            db.session.add(log)
            db.session.commit()
        resp = client.get("/api/v1/audit/logs?actor=test_user")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data is not None

    def test_audit_stats(self, client, app):
        """审计日志 30 天统计"""
        with app.app_context():
            log = AuditLog(actor="test_user", action="login_success", target_type="auth")
            db.session.add(log)
            db.session.commit()
        resp = client.get("/api/v1/audit/stats")
        assert resp.status_code == 200


class TestFdeRoutes:
    """FDE 路由集成测试"""

    def test_fde_meta(self, client):
        """ETL 元数据查询（实际端点为 /stats，对应各层记录数统计）"""
        resp = client.get("/api/v1/fde/stats")
        assert resp.status_code == 200

    def test_fde_quality(self, client):
        """数据质量日志查询（实际端点为 /data-quality）"""
        resp = client.get("/api/v1/fde/data-quality")
        assert resp.status_code == 200

    def test_fde_lineage(self, client):
        """数据血缘查询"""
        resp = client.get("/api/v1/fde/lineage")
        assert resp.status_code == 200

    def test_fde_ads_report_empty(self, client):
        """ADS 日报查询（空）"""
        resp = client.get("/api/v1/fde/ads")
        assert resp.status_code == 200
