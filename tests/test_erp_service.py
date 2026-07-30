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

import pytest

from extensions import db
from models.erp import (
    AccountMove,
    Product,
    PurchaseOrder,
    ReturnOrder,
    SaleOrder,
    StockMove,
    Supplier,
    Warehouse,
)
from models.system import AuditLog
from services.erp_service import ERPService


@pytest.fixture(autouse=True)
def _cleanup_erp_tables(app):
    """每个测试前后清理 ERP 相关表，避免数据污染（ERPService 方法会 commit，db_session fixture 失效）"""
    with app.app_context():
        # 测试前清理
        for model in [StockMove, AccountMove, ReturnOrder, SaleOrder, PurchaseOrder, Product, Supplier, Warehouse, AuditLog]:
            db.session.query(model).delete()
        db.session.commit()
    yield
    with app.app_context():
        for model in [StockMove, AccountMove, ReturnOrder, SaleOrder, PurchaseOrder, Product, Supplier, Warehouse, AuditLog]:
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
