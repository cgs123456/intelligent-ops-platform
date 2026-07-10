"""ERP 层模型（模拟 Odoo 进销存+财务）
完整度：采购/销售/退货/调拨/库存移动/会计凭证/多仓库/成本核算
"""
from datetime import datetime

from extensions import db


class Product(db.Model):
    """产品主数据（SKU）"""
    __tablename__ = 'erp_product'
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    cost_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    sale_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    stock_qty = db.Column(db.Integer, nullable=False, default=0)
    safety_stock = db.Column(db.Integer, nullable=False, default=10)
    avg_cost = db.Column(db.Numeric(10, 2), default=0)  # 移动加权平均成本
    category = db.Column(db.String(32), default='默认')  # 品类
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class Supplier(db.Model):
    """供应商主数据"""
    __tablename__ = 'erp_supplier'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, index=True)
    lead_days = db.Column(db.Integer, nullable=False, default=3)
    rating = db.Column(db.String(1), nullable=False, default='A')
    contact = db.Column(db.String(64))  # 联系人
    phone = db.Column(db.String(32))
    is_active = db.Column(db.Boolean, default=True)


class Warehouse(db.Model):
    """仓库主数据（多仓库支持）"""
    __tablename__ = 'erp_warehouse'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), unique=True, nullable=False)  # WH-01
    name = db.Column(db.String(64), nullable=False)
    location = db.Column(db.String(128))
    is_active = db.Column(db.Boolean, default=True)


class PurchaseOrder(db.Model):
    """采购单（对应 Odoo purchase.order）"""
    __tablename__ = 'erp_purchase_order'
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(40), unique=True, nullable=False, index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('erp_supplier.id'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('erp_product.id'), nullable=False, index=True)
    qty = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(16), nullable=False, default='draft', index=True)  # draft/confirmed/received/cancelled
    ext_order_no = db.Column(db.String(64), index=True)
    suggestion_id = db.Column(db.Integer, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('erp_warehouse.id'))  # 入库仓库
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    confirmed_at = db.Column(db.DateTime)
    received_at = db.Column(db.DateTime)


class SaleOrder(db.Model):
    """销售单（对应 Odoo sale.order）"""
    __tablename__ = 'erp_sale_order'
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(40), unique=True, nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('erp_product.id'), nullable=False, index=True)
    qty = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    customer = db.Column(db.String(64), nullable=False, index=True)
    platform = db.Column(db.String(32), default='线下', index=True)
    status = db.Column(db.String(16), nullable=False, default='confirmed', index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('erp_warehouse.id'))
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)


class StockMove(db.Model):
    """库存移动（对应 Odoo stock.move）
    move_type: in(采购入库) / out(销售出库) / return_in(退货入库) / return_out(退货出库) / transfer(调拨)
    """
    __tablename__ = 'erp_stock_move'
    __table_args__ = (
        db.Index('idx_stockmove_product_warehouse', 'product_id', 'warehouse_id'),
        db.Index('idx_stockmove_type_date', 'move_type', 'created_at'),
    )
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('erp_product.id'), nullable=False, index=True)
    qty = db.Column(db.Integer, nullable=False)  # 正=入库, 负=出库
    move_type = db.Column(db.String(16), nullable=False, index=True)
    ref_order = db.Column(db.String(40), index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('erp_warehouse.id'))
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)


class AccountMove(db.Model):
    """会计凭证（对应 Odoo account.move）"""
    __tablename__ = 'erp_account_move'
    id = db.Column(db.Integer, primary_key=True)
    ref_type = db.Column(db.String(16), nullable=False, index=True)  # payable/receivable/refund
    ref_order = db.Column(db.String(40), index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    account = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)


class ReturnOrder(db.Model):
    """退货单（销售退货 → 红字销售单 + 入库 + 红字应收凭证）"""
    __tablename__ = 'erp_return_order'
    id = db.Column(db.Integer, primary_key=True)
    return_no = db.Column(db.String(40), unique=True, nullable=False, index=True)
    original_order_no = db.Column(db.String(40), nullable=False, index=True)  # 原销售单号
    product_id = db.Column(db.Integer, db.ForeignKey('erp_product.id'), nullable=False, index=True)
    qty = db.Column(db.Integer, nullable=False)
    refund_amount = db.Column(db.Numeric(12, 2), nullable=False)
    reason = db.Column(db.String(256))
    status = db.Column(db.String(16), default='confirmed', index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
