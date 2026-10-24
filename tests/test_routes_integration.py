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


# ====================================================================
# P2-8: 补齐五域路由集成测试（loop / aigc / finance / mrp）
# 原文件仅覆盖 erp / audit / fde 三域
# ====================================================================


class TestLoopRoutes:
    """闭环编排路由"""

    def test_status_shape(self, client):
        """GET /status 返回闭环状态结构"""
        resp = client.get("/api/v1/loop/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "current_step" in data and "steps" in data

    def test_run_step_missing_step(self, client):
        """run-step 缺 step 参数返回 400"""
        resp = client.post("/api/v1/loop/run-step", json={})
        assert resp.status_code == 400

    def test_run_step_out_of_range(self, client):
        """run-step step 超出 1-5 返回 400"""
        resp = client.post("/api/v1/loop/run-step", json={"step": 9})
        assert resp.status_code == 400

    def test_rollback_out_of_range(self, client):
        """rollback step 非法返回 400"""
        resp = client.post("/api/v1/loop/rollback", json={"step": 0})
        assert resp.status_code == 400


class TestAigcRoutes:
    """AIGC 智能建议路由（只测校验分支，不触发 LLM/Text2SQL 真实调用）"""

    def test_suggestions_list(self, client):
        """GET /suggestions 返回 200 列表"""
        resp = client.get("/api/v1/aigc/suggestions")
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_query_empty_question(self, client):
        """data-query 空问题返回 400"""
        resp = client.post("/api/v1/aigc/query", json={"question": "  "})
        assert resp.status_code == 400

    def test_query_question_too_long(self, client):
        """data-query 超 500 字返回 400"""
        resp = client.post("/api/v1/aigc/query", json={"question": "查" * 501})
        assert resp.status_code == 400


class TestFinanceRoutes:
    """财务对账路由（envelope 响应：code 为字符串错误码）"""

    @pytest.fixture(autouse=True)
    def _cleanup_batches(self, app):
        from models.finance import ReconciliationBatch

        yield
        with app.app_context():
            db.session.query(ReconciliationBatch).delete()
            db.session.commit()

    def test_batches_empty_list(self, client):
        """GET 对账批次列表（空）"""
        resp = client.get("/api/v1/finance/reconciliation/batches")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["code"] == "OK" and isinstance(body["data"], list)

    def test_create_batch_missing_period(self, client):
        """创建批次缺 period 返回业务错误码"""
        resp = client.post("/api/v1/finance/reconciliation/batches", json={})
        body = resp.get_json()
        assert body["code"] == "ERR_SYS_002"

    def test_create_batch_then_filter(self, client, app):
        """创建批次后可按 period 过滤查回"""
        resp = client.post(
            "/api/v1/finance/reconciliation/batches", json={"period": "2026-10"}
        )
        assert resp.get_json()["code"] == "OK"
        resp = client.get("/api/v1/finance/reconciliation/batches?period=2026-10")
        data = resp.get_json()["data"]
        assert len(data) >= 1 and data[0]["period"] == "2026-10"


class TestMrpRoutes:
    """MRP/BOM 路由"""

    def test_boms_empty_list(self, client):
        """GET BOM 列表（空，分页结构）"""
        resp = client.get("/api/v1/mrp/boms")
        assert resp.status_code == 200
        body = resp.get_json()
        assert isinstance(body["items"], list) and body["total"] == 0

    def test_create_bom_missing_product(self, client):
        """创建 BOM 缺 product_id 返回 400"""
        resp = client.post("/api/v1/mrp/boms", json={})
        assert resp.status_code == 400

    def test_create_bom_empty_items(self, client):
        """创建 BOM 空 items 返回 400"""
        resp = client.post("/api/v1/mrp/boms", json={"product_id": 1, "items": []})
        assert resp.status_code == 400
