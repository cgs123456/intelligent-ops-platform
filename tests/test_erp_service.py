"""ERPService 进销存核心链路测试

覆盖：
- 采购管理（创建/确认/到货入库/取消）
- 销售管理（创建销售单扣库存）
- 退货管理（创建退货单库存回增）
- 库存调拨（warehouse 间转移）
- 查询接口（库存汇总/近期订单/会计汇总）
- 移动加权平均成本核算
- 审计日志留痕
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from extensions import db
from models.erp import (
    AccountMove,
    Budget,
    CostCenter,
    Customer,
    ExchangeRate,
    Product,
    PurchaseOrder,
    ReorderRule,
    ReturnOrder,
    SaleOrder,
    StockLot,
    StockMove,
    StockTake,
    Supplier,
    Warehouse,
)
from models.system import AuditLog
from services.erp_service import ERPService


@pytest.fixture(autouse=True)
def _cleanup_erp_tables(app):
    """每个测试前后清理 ERP 相关表，避免数据污染（ERPService 方法会 commit，db_session fixture 失效）"""
    cleanup_models = [
        StockMove,
        AccountMove,
        ReturnOrder,
        SaleOrder,
        PurchaseOrder,
        StockLot,
        StockTake,
        ReorderRule,
        Budget,
        CostCenter,
        ExchangeRate,
        Customer,
        Product,
        Supplier,
        Warehouse,
        AuditLog,
    ]
    with app.app_context():
        # 测试前清理
        for model in cleanup_models:
            db.session.query(model).delete()
        db.session.commit()
    yield
    with app.app_context():
        for model in cleanup_models:
            db.session.query(model).delete()
        db.session.commit()


@pytest.fixture
def seed_data(app):
    """创建基础测试数据：1 供应商 + 2 仓库 + 1 产品（有库存）"""
    with app.app_context():
        supplier = Supplier(name="测试供应商A", lead_days=3, rating="A")
        db.session.add(supplier)

        wh1 = Warehouse(code="WH-01", name="主仓库", location="北京")
        wh2 = Warehouse(code="WH-02", name="备用仓", location="上海")
        db.session.add_all([wh1, wh2])

        product = Product(
            sku="TEST-001",
            name="测试商品",
            cost_price=10,
            sale_price=20,
            stock_qty=100,
            safety_stock=10,
            avg_cost=10,
        )
        db.session.add(product)
        db.session.commit()
        return {"supplier_id": supplier.id, "wh1_id": wh1.id, "wh2_id": wh2.id, "product_id": product.id}


class TestERPServicePurchase:
    """采购管理测试"""

    def test_create_purchase_order(self, app, seed_data):
        """创建采购单：状态 draft，写审计日志"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=50,
                unit_price=12,
                warehouse_id=seed_data["wh1_id"],
            )
            assert po.order_no.startswith("PO-")
            assert po.status == "draft"
            assert po.qty == 50
            # 审计日志（action="create", target_type="purchase_order"）
            log = AuditLog.query.filter_by(action="create", target_type="purchase_order").first()
            assert log is not None

    def test_confirm_purchase_order(self, app, seed_data):
        """确认采购单：状态 confirmed，生成应付凭证"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=50,
                unit_price=12,
            )
            svc.confirm_purchase_order(po.id)
            assert po.status == "confirmed"
            # 应付凭证
            amt = AccountMove.query.filter_by(ref_type="payable", ref_order=po.order_no).first()
            assert amt is not None
            assert amt.amount == 50 * 12

    def test_receive_purchase_order(self, app, seed_data):
        """到货入库：库存增加 + 成本核算 + 入库流水"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=50,
                unit_price=12,
            )
            svc.confirm_purchase_order(po.id)
            svc.receive_purchase_order(po.id)
            assert po.status == "received"
            # 库存增加
            product = db.session.get(Product, seed_data["product_id"])
            assert product.stock_qty == 150  # 100 + 50
            # 移动加权平均成本：(100*10 + 50*12) / 150 = 10.67
            assert abs(float(product.avg_cost) - 10.67) < 0.01
            # 入库流水
            move = StockMove.query.filter_by(move_type="in", ref_order=po.order_no).first()
            assert move is not None
            assert move.qty == 50

    def test_cancel_purchase_order(self, app, seed_data):
        """取消采购单：状态 cancelled"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=50,
                unit_price=12,
            )
            svc.cancel_purchase_order(po.id)
            assert po.status == "cancelled"


class TestERPServiceSale:
    """销售管理测试"""

    def test_create_sale_order(self, app, seed_data):
        """创建销售单：扣减库存 + 出库流水 + 应收凭证"""
        with app.app_context():
            svc = ERPService()
            so = svc.create_sale_order(
                product_id=seed_data["product_id"],
                qty=30,
                unit_price=20,
                customer="测试客户",
            )
            assert so.order_no.startswith("SO-")
            # 库存扣减
            product = db.session.get(Product, seed_data["product_id"])
            assert product.stock_qty == 70  # 100 - 30
            # 出库流水
            move = StockMove.query.filter_by(move_type="out", ref_order=so.order_no).first()
            assert move is not None
            assert move.qty == -30
            # 应收凭证
            amt = AccountMove.query.filter_by(ref_type="receivable", ref_order=so.order_no).first()
            assert amt is not None

    def test_create_sale_order_insufficient_stock(self, app, seed_data):
        """库存不足时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            with pytest.raises(ValueError):
                svc.create_sale_order(
                    product_id=seed_data["product_id"],
                    qty=200,  # 超过库存 100
                    unit_price=20,
                    customer="测试客户",
                )


class TestERPServiceReturn:
    """退货管理测试"""

    def test_create_return_order(self, app, seed_data):
        """创建退货单：库存回增 + 退货入库流水 + 红字应收凭证"""
        with app.app_context():
            svc = ERPService()
            # 先创建销售单
            so = svc.create_sale_order(
                product_id=seed_data["product_id"],
                qty=30,
                unit_price=20,
                customer="测试客户",
            )
            # 创建退货单
            ro = svc.create_return_order(
                original_order_no=so.order_no,
                product_id=seed_data["product_id"],
                qty=10,
                refund_amount=200,
                reason="质量问题",
            )
            assert ro.return_no.startswith("RO-")
            # 库存回增
            product = db.session.get(Product, seed_data["product_id"])
            assert product.stock_qty == 80  # 100 - 30 + 10
            # 退货入库流水
            move = StockMove.query.filter_by(move_type="return_in", ref_order=ro.return_no).first()
            assert move is not None
            assert move.qty == 10

    def test_create_return_order_product_not_found(self, app, seed_data):
        """产品不存在时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            with pytest.raises(ValueError):
                svc.create_return_order(
                    original_order_no="SO-NOTEXIST",
                    product_id=99999,  # 不存在的产品 ID
                    qty=10,
                    refund_amount=200,
                )


class TestERPServiceTransfer:
    """库存调拨测试"""

    def test_transfer_stock(self, app, seed_data):
        """调拨：生成出入库两条流水"""
        with app.app_context():
            svc = ERPService()
            # P1-10: 先在源仓库 wh1 入库（采购入库创建 in 流水，使 wh1 有库存可调拨）
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=50,
                unit_price=12,
                warehouse_id=seed_data["wh1_id"],
            )
            svc.confirm_purchase_order(po.id)
            svc.receive_purchase_order(po.id)
            ref = svc.transfer_stock(
                product_id=seed_data["product_id"],
                qty=20,
                from_warehouse_id=seed_data["wh1_id"],
                to_warehouse_id=seed_data["wh2_id"],
            )
            assert ref.startswith("TR-")
            # 两条 transfer 流水（一负一正）
            moves = StockMove.query.filter_by(move_type="transfer", ref_order=ref).all()
            assert len(moves) == 2
            qtys = sorted([m.qty for m in moves])
            assert qtys == [-20, 20]

    def test_transfer_stock_same_warehouse(self, app, seed_data):
        """源仓库 = 目标仓库时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            with pytest.raises(ValueError):
                svc.transfer_stock(
                    product_id=seed_data["product_id"],
                    qty=20,
                    from_warehouse_id=seed_data["wh1_id"],
                    to_warehouse_id=seed_data["wh1_id"],
                )


class TestERPServiceQuery:
    """查询接口测试"""

    def test_get_inventory_summary(self, app, seed_data):
        """库存汇总返回列表"""
        with app.app_context():
            svc = ERPService()
            result = svc.get_inventory_summary()
            assert isinstance(result, list)
            assert len(result) >= 1
            item = next(i for i in result if i["sku"] == "TEST-001")
            assert item["stock_qty"] == 100

    def test_get_recent_orders(self, app, seed_data):
        """近期订单返回列表 + limit 生效"""
        with app.app_context():
            svc = ERPService()
            # 创建几个销售单
            for i in range(5):
                svc.create_sale_order(
                    product_id=seed_data["product_id"],
                    qty=1,
                    unit_price=20,
                    customer=f"客户{i}",
                )
            result = svc.get_recent_orders(limit=3)
            assert len(result) <= 3

    def test_get_account_summary(self, app, seed_data):
        """会计汇总返回 dict"""
        with app.app_context():
            svc = ERPService()
            # 创建采购单并确认（生成应付）
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=50,
                unit_price=12,
            )
            svc.confirm_purchase_order(po.id)
            result = svc.get_account_summary()
            assert isinstance(result, dict)

    def test_get_warehouses(self, app, seed_data):
        """仓库列表查询"""
        with app.app_context():
            from routes.erp import warehouses

            # 通过直接查询验证
            whs = Warehouse.query.filter_by(is_active=True).all()
            assert len(whs) >= 2

    def test_get_warehouse_summary(self, app, seed_data):
        """仓库汇总：按仓库聚合 StockMove 得净库存"""
        with app.app_context():
            svc = ERPService()
            # 先入库到 wh1
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=50,
                unit_price=12,
                warehouse_id=seed_data["wh1_id"],
            )
            svc.confirm_purchase_order(po.id)
            svc.receive_purchase_order(po.id)
            summary = svc.get_warehouse_summary()
            assert isinstance(summary, list)
            # 至少有一条记录（wh1 有入库流水）
            assert len(summary) >= 1
            # 按 net_stock 降序排列
            stocks = [s["net_stock"] for s in summary]
            assert stocks == sorted(stocks, reverse=True)

    def test_get_account_summary_with_tax_and_fx(self, app, seed_data):
        """会计汇总：含税合计 + 多币种按汇率折算"""
        with app.app_context():
            svc = ERPService()
            # 创建 USD 汇率（1 USD = 7.2 CNY）
            rate = ExchangeRate(from_currency="USD", to_currency="CNY", rate=7.2, date=date.today())
            db.session.add(rate)
            db.session.commit()

            # 创建 USD 应付凭证（直接构造 AccountMove，绕过创建流程）
            usd_acc = AccountMove(
                ref_type="payable",
                ref_order="PO-TEST-USD",
                amount=Decimal("100.00"),
                account="应付账款",
                tax_amount=Decimal("13.00"),
                currency_code="USD",
            )
            cny_acc = AccountMove(
                ref_type="receivable",
                ref_order="SO-TEST-CNY",
                amount=Decimal("200.00"),
                account="应收账款",
                tax_amount=Decimal("26.00"),
                currency_code="CNY",
            )
            db.session.add_all([usd_acc, cny_acc])
            db.session.commit()

            result = svc.get_account_summary()
            # USD 100+13=113，折算 CNY = 113 * 7.2 = 813.6
            # CNY 应收 200+26=226
            assert isinstance(result, dict)
            assert "subtotal" in result
            assert "tax_total" in result
            assert "total" in result
            # 总额 = 813.6 + 226 = 1039.6（允许浮点误差）
            assert abs(result["total"] - 1039.6) < 0.5
            # 净应收 = 应收 - 退款
            assert "net_receivable" in result


class TestERPServicePurchaseReturn:
    """采购退货测试（P2-27）"""

    def test_create_purchase_return_success(self, app, seed_data):
        """采购退货：库存减少 + 红字应付凭证 + 状态置 returned"""
        with app.app_context():
            svc = ERPService()
            # 创建采购单 → 确认 → 入库
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=50,
                unit_price=12,
                warehouse_id=seed_data["wh1_id"],
                tax_rate=Decimal("0.13"),
            )
            svc.confirm_purchase_order(po.id)
            svc.receive_purchase_order(po.id)
            # 入库后库存 = 100 + 50 = 150
            product_before = db.session.get(Product, seed_data["product_id"])
            assert product_before.stock_qty == 150

            # 退货 20 件
            order = svc.create_purchase_return(
                order_no=po.order_no,
                qty=20,
                reason="质量问题",
            )
            assert order.status == "returned"
            # 库存减少 20
            product_after = db.session.get(Product, seed_data["product_id"])
            assert product_after.stock_qty == 130
            # 出库流水（return_out，负数）
            move = StockMove.query.filter_by(move_type="return_out", ref_order=po.order_no).first()
            assert move is not None
            assert move.qty == -20
            # 红字应付凭证（负数金额）
            acc = AccountMove.query.filter_by(ref_type="payable", ref_order=po.order_no).all()
            # 应有两条：原应付（正）+ 红字冲销（负）
            negative_accs = [a for a in acc if a.amount < 0]
            assert len(negative_accs) == 1
            assert negative_accs[0].amount == Decimal("-240.00")  # -20 * 12

    def test_create_purchase_return_order_not_found(self, app, seed_data):
        """采购单不存在时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            with pytest.raises(ValueError, match="采购单不存在"):
                svc.create_purchase_return(order_no="PO-NOTEXIST", qty=10, reason="test")

    def test_create_purchase_return_wrong_status(self, app, seed_data):
        """采购单状态非 received 时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=50,
                unit_price=12,
            )
            # 状态为 draft，未确认未入库
            with pytest.raises(ValueError, match="状态非 received"):
                svc.create_purchase_return(order_no=po.order_no, qty=10, reason="test")

    def test_create_purchase_return_qty_exceeds(self, app, seed_data):
        """退货数量超过已收货数量时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=10,
                unit_price=12,
                warehouse_id=seed_data["wh1_id"],
            )
            svc.confirm_purchase_order(po.id)
            svc.receive_purchase_order(po.id)
            with pytest.raises(ValueError, match="退货数量超过已收货数量"):
                svc.create_purchase_return(order_no=po.order_no, qty=100, reason="test")

    def test_create_purchase_return_invalid_qty(self, app, seed_data):
        """退货数量非正时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=10,
                unit_price=12,
                warehouse_id=seed_data["wh1_id"],
            )
            svc.confirm_purchase_order(po.id)
            svc.receive_purchase_order(po.id)
            with pytest.raises(ValueError, match="退货数量必须为正数"):
                svc.create_purchase_return(order_no=po.order_no, qty=0, reason="test")


class TestERPServiceBudgetRefresh:
    """预算实际金额回填测试（P2-20）"""

    def test_refresh_budget_actual_success(self, app, seed_data):
        """刷新预算实际金额：按 cost_center + period 聚合凭证"""
        with app.app_context():
            svc = ERPService()
            # 创建成本中心 + 预算（当月）
            cc = CostCenter(code="CC-TEST", name="测试成本中心")
            db.session.add(cc)
            db.session.flush()

            now = datetime.now()
            budget = Budget(
                cost_center_id=cc.id,
                period=now.strftime("%Y-%m"),
                budget_amount=Decimal("10000"),
                actual_amount=Decimal("0"),
            )
            db.session.add(budget)
            db.session.flush()

            # 创建关联到该成本中心的凭证（当月）
            acc1 = AccountMove(
                ref_type="payable",
                ref_order="PO-BUDGET-1",
                amount=Decimal("500"),
                account="应付账款",
                tax_amount=Decimal("65"),
                currency_code="CNY",
                cost_center_id=cc.id,
                created_at=now,
            )
            acc2 = AccountMove(
                ref_type="receivable",
                ref_order="SO-BUDGET-1",
                amount=Decimal("300"),
                account="应收账款",
                tax_amount=Decimal("39"),
                currency_code="CNY",
                cost_center_id=cc.id,
                created_at=now,
            )
            db.session.add_all([acc1, acc2])
            db.session.commit()

            actual = svc.refresh_budget_actual(budget.id)
            # 实际金额 = (500+65) + (300+39) = 904
            assert actual == Decimal("904.00")
            # 数据库已更新
            budget_db = db.session.get(Budget, budget.id)
            assert budget_db.actual_amount == Decimal("904.00")
            # 审计日志已写入
            log = AuditLog.query.filter_by(action="refresh", target_type="budget").first()
            assert log is not None

    def test_refresh_budget_actual_not_found(self, app, seed_data):
        """预算不存在时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            with pytest.raises(ValueError, match="预算不存在"):
                svc.refresh_budget_actual(99999)

    def test_refresh_budget_actual_invalid_period(self, app, seed_data):
        """period 格式非法时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            cc = CostCenter(code="CC-BAD", name="bad")
            db.session.add(cc)
            db.session.flush()
            budget = Budget(
                cost_center_id=cc.id,
                period="invalid-period",
                budget_amount=Decimal("1000"),
            )
            db.session.add(budget)
            db.session.commit()
            with pytest.raises(ValueError, match="period 格式非法"):
                svc.refresh_budget_actual(budget.id)


class TestERPServicePartialReceive:
    """部分到货测试（P1-12）"""

    def test_partial_receive_two_batches(self, app, seed_data):
        """部分到货：先收部分 → 再收剩余 → 状态变更"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=100,
                unit_price=10,
                warehouse_id=seed_data["wh1_id"],
            )
            svc.confirm_purchase_order(po.id)
            # 第一次到货 40 件
            svc.receive_purchase_order(po.id, qty=40)
            assert po.status == "confirmed"  # 未全到，状态仍 confirmed
            assert po.received_qty == 40
            product = db.session.get(Product, seed_data["product_id"])
            assert product.stock_qty == 140  # 100 + 40

            # 第二次到货剩余 60 件
            svc.receive_purchase_order(po.id, qty=60)
            assert po.status == "received"
            assert po.received_qty == 100
            product = db.session.get(Product, seed_data["product_id"])
            assert product.stock_qty == 200  # 100 + 100

    def test_partial_receive_exceeds_remaining(self, app, seed_data):
        """本次到货数量超过未交数量时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=10,
                unit_price=10,
                warehouse_id=seed_data["wh1_id"],
            )
            svc.confirm_purchase_order(po.id)
            with pytest.raises(ValueError, match="本次到货数量超过未交数量"):
                svc.receive_purchase_order(po.id, qty=20)

    def test_partial_receive_invalid_qty(self, app, seed_data):
        """本次到货数量非正时抛 ValueError"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=10,
                unit_price=10,
                warehouse_id=seed_data["wh1_id"],
            )
            svc.confirm_purchase_order(po.id)
            with pytest.raises(ValueError, match="本次到货数量必须为正数"):
                svc.receive_purchase_order(po.id, qty=0)

    def test_receive_with_lot(self, app, seed_data):
        """带批次到货：创建 StockLot 关联到入库流水"""
        with app.app_context():
            svc = ERPService()
            po = svc.create_purchase_order(
                supplier_id=seed_data["supplier_id"],
                product_id=seed_data["product_id"],
                qty=50,
                unit_price=12,
                warehouse_id=seed_data["wh1_id"],
            )
            svc.confirm_purchase_order(po.id)
            svc.receive_purchase_order(
                po.id,
                lot_no="LOT-202607-001",
                manufacture_date=datetime(2026, 7, 1),
                expiry_date=datetime(2026, 12, 31),
            )
            # 批次记录已创建
            lot = StockLot.query.filter_by(lot_no="LOT-202607-001").first()
            assert lot is not None
            assert lot.quantity == 50
            assert lot.remaining_qty == 50
            assert lot.status == "active"
            # 入库流水关联批次
            move = StockMove.query.filter_by(move_type="in", ref_order=po.order_no).first()
            assert move is not None
            assert move.lot_id == lot.id


class TestERPServiceReorderRules:
    """库存预警测试（P1-9）"""

    def test_check_reorder_rules_with_alert(self, app, seed_data):
        """库存低于安全库存时触发预警"""
        with app.app_context():
            svc = ERPService()
            # 把产品库存压低到安全库存以下
            product = db.session.get(Product, seed_data["product_id"])
            product.stock_qty = 5  # safety_stock=10，触发预警
            db.session.commit()

            alerts = svc.check_reorder_rules()
            assert isinstance(alerts, list)
            assert len(alerts) >= 1
            alert = next(a for a in alerts if a["product_id"] == seed_data["product_id"])
            assert alert["current_qty"] == 5
            assert alert["suggest_qty"] > 0

    def test_check_reorder_rules_with_rule(self, app, seed_data):
        """命中 ReorderRule 时按 rule.min_qty 阈值判定"""
        with app.app_context():
            svc = ERPService()
            # 创建 ReorderRule（min_qty=80, max_qty=200）
            rule = ReorderRule(
                product_id=seed_data["product_id"],
                warehouse_id=seed_data["wh1_id"],
                min_qty=80,
                max_qty=200,
                supplier_id=seed_data["supplier_id"],
                is_active=True,
            )
            db.session.add(rule)
            db.session.commit()
            # 当前库存 100，min_qty=80，不应触发
            alerts = svc.check_reorder_rules()
            alert_for_product = [a for a in alerts if a["product_id"] == seed_data["product_id"]]
            assert len(alert_for_product) == 0  # 100 > 80，未触发

            # 把库存压低到 70，触发
            product = db.session.get(Product, seed_data["product_id"])
            product.stock_qty = 70
            db.session.commit()
            alerts = svc.check_reorder_rules()
            alert_for_product = [a for a in alerts if a["product_id"] == seed_data["product_id"]]
            assert len(alert_for_product) == 1
            assert alert_for_product[0]["supplier_id"] == seed_data["supplier_id"]


class TestERPModels:
    """ERP 扩展模型创建测试"""

    def test_exchange_rate_model(self, app):
        """ExchangeRate 模型创建 + _get_latest_rate 取最新"""
        with app.app_context():
            svc = ERPService()
            # 创建多条汇率（不同日期）
            for d, r in [(date(2026, 7, 1), 7.1), (date(2026, 7, 20), 7.2), (date(2026, 7, 10), 7.15)]:
                rate = ExchangeRate(from_currency="USD", to_currency="CNY", rate=r, date=d)
                db.session.add(rate)
            db.session.commit()

            latest = svc._get_latest_rate("USD", "CNY")
            assert float(latest) == 7.2  # 取 date 最大的（Numeric 列返回 Decimal，转 float 比较）

    def test_stocktake_model(self, app, seed_data):
        """StockTake 盘点模型创建"""
        with app.app_context():
            st = StockTake(
                product_id=seed_data["product_id"],
                warehouse_id=seed_data["wh1_id"],
                system_qty=100,
                actual_qty=98,
                diff=-2,
                status="draft",
            )
            db.session.add(st)
            db.session.commit()
            assert st.id is not None
            assert st.diff == -2
            assert st.status == "draft"

    def test_customer_model(self, app):
        """Customer 客户模型创建"""
        with app.app_context():
            cust = Customer(
                name="测试客户A",
                credit_limit=Decimal("10000.00"),
                balance=Decimal("0"),
            )
            db.session.add(cust)
            db.session.commit()
            assert cust.id is not None
            assert cust.is_active is True
            assert float(cust.credit_limit) == 10000.0

    def test_budget_and_cost_center_model(self, app):
        """Budget + CostCenter 模型创建"""
        with app.app_context():
            cc = CostCenter(code="CC-001", name="销售部", description="销售成本中心")
            db.session.add(cc)
            db.session.flush()
            budget = Budget(
                cost_center_id=cc.id,
                period="2026-07",
                budget_amount=Decimal("50000"),
                actual_amount=Decimal("0"),
                category="general",
            )
            db.session.add(budget)
            db.session.commit()
            assert cc.id is not None
            assert budget.id is not None
            assert budget.is_active is True


class TestERPServiceStockTake:
    """P1: 库存盘点（cycle count）测试"""

    def test_create_stock_take_records_system_qty(self, app, seed_data):
        """创建盘点单：记录当前系统库存"""
        with app.app_context():
            product = Product.query.filter_by(sku="TEST-001").first()
            take = ERPService().create_stock_take(product_id=product.id)
            assert take.id is not None
            assert take.system_qty == 100  # seed_data 中 product.stock_qty=100
            assert take.actual_qty == 100  # 默认实际=系统
            assert take.diff == 0
            assert take.status == "draft"
            # 审计日志
            log = AuditLog.query.filter_by(action="create", target_type="stock_take").first()
            assert log is not None

    def test_create_stock_take_nonexistent_product(self, app):
        """创建盘点单：产品不存在抛 ValueError"""
        with app.app_context(), pytest.raises(ValueError, match="产品不存在"):
            ERPService().create_stock_take(product_id=99999)

    def test_confirm_stock_take_with_loss(self, app, seed_data):
        """确认盘点单：盘亏（actual < system）→ 库存减少 + 负差 + adjust 流水"""
        with app.app_context():
            product = Product.query.filter_by(sku="TEST-001").first()
            take = ERPService().create_stock_take(product_id=product.id)
            # 盘亏：实际 95，系统 100，diff=-5
            result = ERPService().confirm_stock_take(take_id=take.id, actual_qty=95)
            assert result.status == "confirmed"
            assert result.diff == -5
            assert result.actual_qty == 95
            # 库存调整
            db.session.refresh(product)
            assert product.stock_qty == 95
            # adjust 流水
            move = StockMove.query.filter_by(move_type="adjust", ref_order=f"STK-{take.id}").first()
            assert move is not None
            assert move.qty == -5

    def test_confirm_stock_take_with_gain(self, app, seed_data):
        """确认盘点单：盘盈（actual > system）→ 库存增加 + 正差"""
        with app.app_context():
            product = Product.query.filter_by(sku="TEST-001").first()
            take = ERPService().create_stock_take(product_id=product.id)
            result = ERPService().confirm_stock_take(take_id=take.id, actual_qty=105)
            assert result.diff == 5
            db.session.refresh(product)
            assert product.stock_qty == 105

    def test_confirm_stock_take_no_diff(self, app, seed_data):
        """确认盘点单：无差异（actual == system）→ 不生成 adjust 流水"""
        with app.app_context():
            product = Product.query.filter_by(sku="TEST-001").first()
            take = ERPService().create_stock_take(product_id=product.id)
            ERPService().confirm_stock_take(take_id=take.id, actual_qty=100)
            # 无 adjust 流水
            move = StockMove.query.filter_by(move_type="adjust").first()
            assert move is None

    def test_confirm_stock_take_invalid_status(self, app, seed_data):
        """确认盘点单：状态非 draft 抛 ValueError"""
        with app.app_context():
            product = Product.query.filter_by(sku="TEST-001").first()
            take = ERPService().create_stock_take(product_id=product.id)
            ERPService().confirm_stock_take(take_id=take.id, actual_qty=100)
            # 已 confirmed，再次确认应失败
            with pytest.raises(ValueError, match="状态非 draft"):
                ERPService().confirm_stock_take(take_id=take.id, actual_qty=90)

    def test_confirm_stock_take_nonexistent(self, app):
        """确认盘点单：不存在抛 ValueError"""
        with app.app_context(), pytest.raises(ValueError, match="盘点单不存在"):
            ERPService().confirm_stock_take(take_id=99999, actual_qty=10)

    def test_confirm_stock_take_negative_qty(self, app, seed_data):
        """确认盘点单：负数实际数量抛 ValueError"""
        with app.app_context():
            product = Product.query.filter_by(sku="TEST-001").first()
            take = ERPService().create_stock_take(product_id=product.id)
            with pytest.raises(ValueError, match="非负整数"):
                ERPService().confirm_stock_take(take_id=take.id, actual_qty=-1)

    def test_list_stock_takes_filter(self, app, seed_data):
        """查询盘点单列表：按状态过滤"""
        with app.app_context():
            product = Product.query.filter_by(sku="TEST-001").first()
            ERPService().create_stock_take(product_id=product.id)
            take2 = ERPService().create_stock_take(product_id=product.id)
            ERPService().confirm_stock_take(take_id=take2.id, actual_qty=95)

            # 全部
            all_takes = ERPService().list_stock_takes()
            assert len(all_takes) == 2
            # 仅 draft
            drafts = ERPService().list_stock_takes(status="draft")
            assert len(drafts) == 1
            assert drafts[0]["status"] == "draft"
            # 仅 confirmed
            confirmed = ERPService().list_stock_takes(status="confirmed")
            assert len(confirmed) == 1
            assert confirmed[0]["status"] == "confirmed"
            assert confirmed[0]["diff"] == -5
