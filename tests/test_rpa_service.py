"""RPAService 测试 — 采集、同步、下单、重试、调度

覆盖：
- 报价采集 collect_supplier_quotes（有报价 + 当天无报价走兜底）
- 最优报价查询 get_best_quote（综合评分 = 价格 + 交期*权重）
- 单条订单同步 sync_with_retry（成功 + 失败重试 + 订单不存在）
- 批量同步 sync_ecommerce_orders（异常不中断整批）
- 自动下单 place_supplier_order（mock ERPService）
- 调度状态 get_schedule_status
- 定时任务 run_scheduled_tasks
- 审计日志写入
"""

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from extensions import db
from models.erp import Product, Supplier
from models.external import ExtEcommerceOrder, ExtSupplierQuote
from models.system import AuditLog
from services.rpa_service import RPAService


@pytest.fixture(autouse=True)
def _cleanup_rpa_tables(app):
    """每个测试前后清理 RPA 相关表"""
    models_to_clean = [
        AuditLog,
        ExtSupplierQuote,
        ExtEcommerceOrder,
        Product,
        Supplier,
    ]
    with app.app_context():
        for m in models_to_clean:
            db.session.query(m).delete()
        db.session.commit()
    yield
    with app.app_context():
        for m in models_to_clean:
            db.session.query(m).delete()
        db.session.commit()


@pytest.fixture
def seed_quotes(app):
    """创建供应商 + 产品 + 当天报价"""
    with app.app_context():
        sup_a = Supplier(name="供应商A", lead_days=2, rating="A", is_active=True)
        sup_b = Supplier(name="供应商B", lead_days=5, rating="B", is_active=True)
        db.session.add_all([sup_a, sup_b])
        db.session.flush()

        prod = Product(
            sku="RPA-001",
            name="RPA测试商品",
            cost_price=10,
            sale_price=20,
            stock_qty=50,
            safety_stock=10,
            avg_cost=10,
            is_active=True,
        )
        db.session.add(prod)
        db.session.flush()

        # 当天报价：A 便宜但交期短，B 贵但交期长
        q_a = ExtSupplierQuote(
            supplier_id=sup_a.id,
            product_id=prod.id,
            quote_price=15.0,
            quote_date=date.today(),
            source="供应商官网",
        )
        q_b = ExtSupplierQuote(
            supplier_id=sup_b.id,
            product_id=prod.id,
            quote_price=12.0,
            quote_date=date.today(),
            source="供应商官网",
        )
        db.session.add_all([q_a, q_b])
        db.session.commit()
        return {
            "sup_a_id": sup_a.id,
            "sup_b_id": sup_b.id,
            "product_id": prod.id,
            "quote_a_id": q_a.id,
            "quote_b_id": q_b.id,
        }


class TestCollectSupplierQuotes:
    """报价采集测试"""

    def test_collect_with_existing_quotes(self, app, seed_quotes):
        """当天已有报价时直接返回，不触发兜底生成"""
        with app.app_context():
            svc = RPAService()
            result = svc.collect_supplier_quotes()
            assert len(result) == 2
            prices = {r["price"] for r in result}
            assert 15.0 in prices
            assert 12.0 in prices

    def test_collect_writes_audit_log(self, app, seed_quotes):
        """采集后写审计日志 action=collect"""
        with app.app_context():
            svc = RPAService()
            svc.collect_supplier_quotes()
            log = AuditLog.query.filter_by(actor="rpa-service", action="collect").first()
            assert log is not None
            assert log.target_type == "ext_supplier_quote"

    def test_collect_result_fields(self, app, seed_quotes):
        """采集结果字段完整：quote_id/supplier/product/price/source/date/collected_at"""
        with app.app_context():
            svc = RPAService()
            result = svc.collect_supplier_quotes()
            item = result[0]
            assert "quote_id" in item
            assert "supplier_id" in item
            assert "supplier" in item
            assert "product_id" in item
            assert "product" in item
            assert "price" in item
            assert "source" in item
            assert "date" in item
            assert "collected_at" in item


class TestGetBestQuote:
    """最优报价查询测试"""

    def test_best_quote_lowest_score(self, app, seed_quotes):
        """综合评分 = 价格 + 交期*权重，选评分最低的报价"""
        with app.app_context():
            svc = RPAService()
            best = svc.get_best_quote(seed_quotes["product_id"])
            assert best is not None
            quote, supplier = best
            # A: 15 + 2*2 = 19；B: 12 + 5*2 = 22 → A 评分更低
            assert supplier.name == "供应商A"
            assert float(quote.quote_price) == 15.0

    def test_best_quote_no_quotes_returns_none(self, app, seed_quotes):
        """无报价产品返回 None"""
        with app.app_context():
            # 创建一个无报价的产品
            prod = Product(
                sku="RPA-NO-QUOTE",
                name="无报价商品",
                cost_price=10,
                sale_price=20,
                stock_qty=50,
                safety_stock=10,
                is_active=True,
            )
            db.session.add(prod)
            db.session.commit()
            svc = RPAService()
            result = svc.get_best_quote(prod.id)
            assert result is None

    def test_best_quote_lead_time_weight(self, app, seed_quotes):
        """交期权重 LEAD_TIME_WEIGHT=2.0 影响评分"""
        with app.app_context():
            svc = RPAService()
            assert svc.LEAD_TIME_WEIGHT == 2.0


class TestSyncWithRetry:
    """单条订单同步 + 重试测试"""

    @pytest.fixture
    def seed_ecom_order(self, app, seed_quotes):
        """创建一条电商订单（SKU 匹配 seed_quotes 的产品）"""
        with app.app_context():
            prod = db.session.get(Product, seed_quotes["product_id"])
            ext = ExtEcommerceOrder(
                platform="taobao",
                product_sku=prod.sku,
                qty=5,
                unit_price=25,
                customer="电商客户",
            )
            db.session.add(ext)
            db.session.commit()
            return ext.id

    def test_sync_success(self, app, seed_quotes, seed_ecom_order):
        """同步成功：synced=True，返回 status=ok"""
        with app.app_context():
            svc = RPAService()
            result = svc.sync_with_retry(seed_ecom_order)
            assert result["status"] == "ok"
            assert result["ext_order_id"] == seed_ecom_order
            assert "order_no" in result

            ext = db.session.get(ExtEcommerceOrder, seed_ecom_order)
            assert ext.synced is True
            assert ext.sync_error is None

    def test_sync_order_not_found(self, app, seed_quotes):
        """订单不存在时返回 status=fail reason=订单不存在"""
        with app.app_context():
            svc = RPAService()
            result = svc.sync_with_retry(99999)
            assert result["status"] == "fail"
            assert "不存在" in result["reason"]

    def test_sync_sku_not_found_retries_and_fails(self, app, seed_quotes):
        """SKU 不匹配时重试 max_retry 次后失败，写 sync_error"""
        with app.app_context():
            ext = ExtEcommerceOrder(
                platform="taobao",
                product_sku="NOT-EXIST-SKU",
                qty=5,
                unit_price=25,
                customer="客户",
            )
            db.session.add(ext)
            db.session.commit()
            ext_id = ext.id

            svc = RPAService()
            # max_retry=1 减少 sleep 时间
            result = svc.sync_with_retry(ext_id, max_retry=1, backoff=0.1)
            assert result["status"] == "fail"
            assert "retries" in result
            assert result["retries"] == 2  # 首次 + 1 次重试

            ext = db.session.get(ExtEcommerceOrder, ext_id)
            assert ext.synced is False
            assert ext.sync_error is not None
            assert "SKU" in ext.sync_error


class TestSyncEcommerceOrders:
    """批量同步测试"""

    def test_batch_sync_mixed_results(self, app, seed_quotes):
        """批量同步：部分成功部分失败，不中断整批"""
        with app.app_context():
            prod = db.session.get(Product, seed_quotes["product_id"])
            # 1 条合法订单 + 1 条非法 SKU
            ok_order = ExtEcommerceOrder(
                platform="taobao", product_sku=prod.sku, qty=2, unit_price=20, customer="客户A"
            )
            bad_order = ExtEcommerceOrder(
                platform="jd", product_sku="BAD-SKU", qty=1, unit_price=10, customer="客户B"
            )
            db.session.add_all([ok_order, bad_order])
            db.session.commit()

            svc = RPAService()
            results = svc.sync_ecommerce_orders()
            assert len(results) == 2
            statuses = {r["status"] for r in results}
            assert "ok" in statuses
            assert "fail" in statuses

    def test_batch_sync_empty(self, app, seed_quotes):
        """无待同步订单时返回空列表"""
        with app.app_context():
            svc = RPAService()
            results = svc.sync_ecommerce_orders()
            assert results == []

    def test_batch_sync_writes_audit(self, app, seed_quotes):
        """批量同步后写审计日志 action=sync"""
        with app.app_context():
            svc = RPAService()
            svc.sync_ecommerce_orders()
            log = AuditLog.query.filter_by(actor="rpa-service", action="sync").first()
            assert log is not None


class TestPlaceSupplierOrder:
    """自动下单测试（mock ERPService 避免真实写库）"""

    def test_place_order_success(self, app, seed_quotes):
        """自动下单成功：返回 status=ok + 外部订单号"""
        with app.app_context():
            svc = RPAService()
            suggestion = {
                "id": 1,
                "product_id": seed_quotes["product_id"],
                "suggested_qty": 30,
            }

            # mock ERPService 的 create_purchase_order + confirm_purchase_order
            with patch("services.erp_service.ERPService") as MockERP:
                mock_instance = MockERP.return_value
                mock_po = MagicMock()
                mock_po.id = 1001
                mock_po.order_no = "PO-MOCK-001"
                mock_po.qty = 30
                mock_po.unit_price = 15.0
                mock_instance.create_purchase_order.return_value = mock_po
                mock_instance.confirm_purchase_order.return_value = None

                result = svc.place_supplier_order(suggestion)

            assert result["status"] == "ok"
            assert result["po_id"] == 1001
            assert result["po_no"] == "PO-MOCK-001"
            assert "ext_order_no" in result
            assert result["supplier_id"] in (seed_quotes["sup_a_id"], seed_quotes["sup_b_id"])
            assert result["qty"] == 30

    def test_place_order_no_quote_fails(self, app, seed_quotes):
        """无可用报价时返回 status=fail"""
        with app.app_context():
            # 创建一个无报价产品
            prod = Product(
                sku="RPA-NO-QUOTE-ORDER",
                name="无报价下单商品",
                cost_price=10,
                sale_price=20,
                stock_qty=50,
                safety_stock=10,
                is_active=True,
            )
            db.session.add(prod)
            db.session.commit()

            svc = RPAService()
            result = svc.place_supplier_order({"id": 1, "product_id": prod.id, "suggested_qty": 10})
            assert result["status"] == "fail"
            assert "无可用供应商报价" in result["reason"]

    def test_place_order_writes_audit(self, app, seed_quotes):
        """下单成功后写审计日志 action=order"""
        with app.app_context():
            svc = RPAService()
            suggestion = {"id": 1, "product_id": seed_quotes["product_id"], "suggested_qty": 20}

            with patch("services.erp_service.ERPService") as MockERP:
                mock_instance = MockERP.return_value
                mock_po = MagicMock()
                mock_po.id = 2002
                mock_po.order_no = "PO-MOCK-002"
                mock_po.qty = 20
                mock_po.unit_price = 15.0
                mock_instance.create_purchase_order.return_value = mock_po
                mock_instance.confirm_purchase_order.return_value = None

                svc.place_supplier_order(suggestion)

            log = AuditLog.query.filter_by(actor="rpa-service", action="order").first()
            assert log is not None
            assert log.target_type == "erp_purchase_order"
            assert log.target_id == "2002"


class TestScheduleInterface:
    """调度接口测试"""

    def test_get_schedule_status(self, app):
        """get_schedule_status 返回调度配置 dict"""
        with app.app_context():
            svc = RPAService()
            status = svc.get_schedule_status()
            assert "schedule_enabled" in status
            assert "quote_cron" in status
            assert "sync_cron" in status
            assert "max_retry" in status
            assert "retry_backoff" in status
            assert isinstance(status["max_retry"], int)
            assert isinstance(status["retry_backoff"], float)

    def test_run_scheduled_tasks(self, app, seed_quotes):
        """run_scheduled_tasks 执行采集 + 同步，返回汇总"""
        with app.app_context():
            svc = RPAService()
            result = svc.run_scheduled_tasks()
            assert result["status"] == "ok"
            assert "quotes_collected" in result
            assert "orders_synced" in result
            assert result["quotes_collected"] >= 2  # seed 的 2 条报价

    def test_run_scheduled_writes_audit(self, app, seed_quotes):
        """定时任务后写审计日志 action=schedule"""
        with app.app_context():
            svc = RPAService()
            svc.run_scheduled_tasks()
            log = AuditLog.query.filter_by(actor="rpa-service", action="schedule").first()
            assert log is not None
            assert "定时任务" in (log.detail or "")
