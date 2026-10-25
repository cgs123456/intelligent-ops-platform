"""契约对齐消融实验（adversarial review 产出）

验证前后端契约修复的核心机制：每个断言都对应一条"拿掉修复就失败"的因果链，
防止字段名/结构回归：

1. dashboard low_stock_count 依赖 get_inventory_summary 输出的 is_below_safety 键
   —— 消融：换成旧键名 is_low 后计数必须归零（证明键名是唯一通路）。
2. inventory 端点必须输出 is_below_safety（前端唯一消费键）。
3. 近期采购单必须输出 product（产品名）字段（前端渲染直接消费）。
4. 旧无版本路径（308 兼容层）移除后必须 404，v1 路径正常。
"""

import pytest

from extensions import db
from models.erp import Product, PurchaseOrder, Supplier
from models.warehouse import DataLineage, DataQualityLog
from services.erp_service import ERPService


@pytest.fixture(autouse=True)
def _cleanup(app):
    with app.app_context():
        for model in [DataQualityLog, DataLineage, PurchaseOrder, Product, Supplier]:
            db.session.query(model).delete()
        db.session.commit()
    yield
    with app.app_context():
        for model in [DataQualityLog, DataLineage, PurchaseOrder, Product, Supplier]:
            db.session.query(model).delete()
        db.session.commit()


def _seed_products(app):
    """1 个低库存（stock < safety）+ 1 个正常，返回 (low_id, ok_id)。"""
    with app.app_context():
        low = Product(sku="ABL-LOW", name="低库存品", stock_qty=5, safety_stock=50)
        ok = Product(sku="ABL-OK", name="正常品", stock_qty=100, safety_stock=10)
        db.session.add_all([low, ok])
        db.session.commit()
        return low.id, ok.id


class TestDashboardLowStockAblation:
    """低库存计数的字段名通路消融"""

    def test_low_stock_count_counts_below_safety_items(self, client, app):
        _seed_products(app)
        resp = client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["low_stock_count"] == 1

    def test_ablation_old_key_name_yields_zero(self, client, app, monkeypatch):
        """消融：service 输出旧键名 is_low 时，dashboard 必须计 0。

        证明 dashboard 只认 is_below_safety —— 键名即因果通路，
        同时反向锚定"若有人改回 is_low 会立刻破坏仪表盘"。
        """
        _seed_products(app)

        original = ERPService.get_inventory_summary

        def old_shape(self):
            out = []
            for i in original(self):
                j = dict(i)
                j["is_low"] = j.pop("is_below_safety")
                out.append(j)
            return out

        monkeypatch.setattr(ERPService, "get_inventory_summary", old_shape)
        resp = client.get("/api/dashboard")
        assert resp.status_code == 200
        assert resp.get_json()["low_stock_count"] == 0


class TestInventoryContract:
    def test_inventory_exposes_is_below_safety_key(self, client, app):
        _seed_products(app)
        resp = client.get("/api/v1/erp/inventory")
        assert resp.status_code == 200
        rows = {r["sku"]: r for r in resp.get_json()}
        assert rows["ABL-LOW"]["is_below_safety"] is True
        assert rows["ABL-OK"]["is_below_safety"] is False


class TestRecentOrdersContract:
    def test_recent_purchase_orders_expose_product_name(self, client, app):
        with app.app_context():
            sup = Supplier(name="消融供应商", contact="张三", phone="13800000000")
            db.session.add(sup)
            db.session.commit()
            ERPService().create_purchase_order(
                supplier_id=sup.id, product_id=_seed_products(app)[1], qty=3, unit_price=9.9
            )
        resp = client.get("/api/v1/erp/orders")
        assert resp.status_code == 200
        orders = [o for o in resp.get_json() if o.get("product") == "正常品"]
        assert len(orders) == 1
        assert orders[0]["qty"] == 3


class TestLegacyPathsRemoved:
    def test_unversioned_paths_are_404(self, client):
        for path in ("/api/erp/inventory", "/api/aigc/report", "/api/loop/status"):
            assert client.get(path).status_code == 404, path

    def test_v1_paths_alive(self, client):
        assert client.get("/api/v1/erp/inventory").status_code == 200


class TestFdeContractKeys:
    def test_lineage_and_dq_output_shape(self, client, app):
        with app.app_context():
            db.session.add(
                DataLineage(
                    upstream_table="ods_sale_order",
                    downstream_table="dwd_sales_order_di",
                    layer="DWD",
                    transformation="清洗",
                )
            )
            db.session.add(
                DataQualityLog(
                    table_name="ods_sale_order",
                    test_name="not_null",
                    status="pass",
                    failures=0,
                    column_name="order_no",
                )
            )
            db.session.commit()
        lin = client.get("/api/v1/fde/lineage").get_json()
        assert lin and lin[0]["upstream"] == "ods_sale_order"
        assert lin[0]["downstream"] == "dwd_sales_order_di"
        dq = client.get("/api/v1/fde/data-quality").get_json()
        assert dq["ods_sale_order"]["not_null"]["status"] == "pass"
