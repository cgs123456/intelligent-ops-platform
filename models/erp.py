"""ERP 层模型（模拟 Odoo 进销存+财务）
完整度：采购/销售/退货/调拨/库存移动/会计凭证/多仓库/成本核算
"""

from datetime import datetime

from sqlalchemy.ext.hybrid import hybrid_property

from extensions import db


def _encrypt_field(value):
    """加密字段值（延迟导入避免循环依赖）"""
    from services.crypto import field_crypto

    return field_crypto.encrypt(value)


def _decrypt_field(value):
    """解密字段值（延迟导入避免循环依赖）"""
    from services.crypto import field_crypto

    return field_crypto.decrypt(value)


class Product(db.Model):
    """产品主数据（SKU）"""

    __tablename__ = "erp_product"
    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    cost_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    sale_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    stock_qty = db.Column(db.Integer, nullable=False, default=0)
    safety_stock = db.Column(db.Integer, nullable=False, default=10)
    avg_cost = db.Column(db.Numeric(10, 2), default=0)  # 移动加权平均成本
    category = db.Column(db.String(32), default="默认")  # 品类
    # P1-9: 多币种 + 多税率支持
    currency_code = db.Column(db.String(3), nullable=False, default="CNY")  # ISO 4217 三字母代码
    tax_rate = db.Column(db.Numeric(5, 4), nullable=False, default=0.13)  # 增值税率（如 0.13 表示 13%）
    # P1-9: 成本中心（用于预算管控，从产品维度归集成本，创建凭证时继承到 AccountMove）
    cost_center_id = db.Column(db.Integer, db.ForeignKey("erp_cost_center.id"), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class Supplier(db.Model):
    """供应商主数据"""

    __tablename__ = "erp_supplier"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False, index=True)
    lead_days = db.Column(db.Integer, nullable=False, default=3)
    rating = db.Column(db.String(1), nullable=False, default="A")
    # P0-fix: PII 字段加密存储（密文存储，通过 _contact/_phone 访问明文）
    _contact = db.Column("contact", db.String(256))  # 联系人（加密）
    _phone = db.Column("phone", db.String(256))  # 手机号（加密）
    is_active = db.Column(db.Boolean, default=True)

    @hybrid_property
    def contact(self):
        """联系人（明文读取）"""
        return _decrypt_field(self._contact)

    @contact.setter
    def contact(self, value):
        self._contact = _encrypt_field(value) if value else None

    @hybrid_property
    def phone(self):
        """手机号（明文读取）"""
        return _decrypt_field(self._phone)

    @phone.setter
    def phone(self, value):
        self._phone = _encrypt_field(value) if value else None


class Warehouse(db.Model):
    """仓库主数据（多仓库支持）"""

    __tablename__ = "erp_warehouse"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), unique=True, nullable=False)  # WH-01
    name = db.Column(db.String(64), nullable=False)
    location = db.Column(db.String(128))
    is_active = db.Column(db.Boolean, default=True)


class PurchaseOrder(db.Model):
    """采购单（对应 Odoo purchase.order）"""

    __tablename__ = "erp_purchase_order"
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(40), unique=True, nullable=False, index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("erp_supplier.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)
    qty = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="draft", index=True)  # draft/confirmed/received/cancelled
    ext_order_no = db.Column(db.String(64), index=True, unique=True)  # P2-1: 外部平台单号唯一，防重复同步建单
    suggestion_id = db.Column(db.Integer, index=True, unique=True)  # P2-1: 一条补货建议最多一张采购单（幂等键）
    warehouse_id = db.Column(db.Integer, db.ForeignKey("erp_warehouse.id"))  # 入库仓库
    # P1-9: 多币种 + 多税率支持
    currency_code = db.Column(db.String(3), nullable=False, default="CNY")
    tax_rate = db.Column(db.Numeric(5, 4), nullable=False, default=0.13)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    confirmed_at = db.Column(db.DateTime)
    received_at = db.Column(db.DateTime)
    # P1-12: 部分到货支持 —— 累计已收货数量，当 received_qty >= qty 时状态置 received
    received_qty = db.Column(db.Integer, nullable=False, default=0)


class SaleOrder(db.Model):
    """销售单（对应 Odoo sale.order）"""

    __tablename__ = "erp_sale_order"
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(40), unique=True, nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)
    qty = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    customer = db.Column(db.String(64), nullable=False, index=True)
    platform = db.Column(db.String(32), default="线下", index=True)
    status = db.Column(db.String(16), nullable=False, default="confirmed", index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("erp_warehouse.id"))
    # P1-9: 多币种 + 多税率支持
    currency_code = db.Column(db.String(3), nullable=False, default="CNY")
    tax_rate = db.Column(db.Numeric(5, 4), nullable=False, default=0.13)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)


class StockMove(db.Model):
    """库存移动（对应 Odoo stock.move）
    move_type: in(采购入库) / out(销售出库) / return_in(退货入库) / return_out(退货出库) / transfer(调拨)
    """

    __tablename__ = "erp_stock_move"
    __table_args__ = (
        db.Index("idx_stockmove_product_warehouse", "product_id", "warehouse_id"),
        db.Index("idx_stockmove_type_date", "move_type", "created_at"),
    )
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)
    qty = db.Column(db.Integer, nullable=False)  # 正=入库, 负=出库
    move_type = db.Column(db.String(16), nullable=False, index=True)
    ref_order = db.Column(db.String(40), index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("erp_warehouse.id"))
    # P1-9: 批次管理（关联 StockLot，可空，非批次商品为 NULL）
    lot_id = db.Column(db.Integer, db.ForeignKey("erp_stock_lot.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)


class AccountMove(db.Model):
    """会计凭证（对应 Odoo account.move）"""

    __tablename__ = "erp_account_move"
    id = db.Column(db.Integer, primary_key=True)
    ref_type = db.Column(db.String(16), nullable=False, index=True)  # payable/receivable/refund
    ref_order = db.Column(db.String(40), index=True)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    account = db.Column(db.String(32), nullable=False)
    # P1-9: 多币种 + 税额 + 成本中心
    tax_amount = db.Column(db.Numeric(12, 2), default=0)  # 税额（按订单 tax_rate 计算）
    currency_code = db.Column(db.String(3), default="CNY")  # 币种 ISO 4217
    cost_center_id = db.Column(db.Integer, db.ForeignKey("erp_cost_center.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)


class ReturnOrder(db.Model):
    """退货单（销售退货 → 红字销售单 + 入库 + 红字应收凭证）"""

    __tablename__ = "erp_return_order"
    id = db.Column(db.Integer, primary_key=True)
    return_no = db.Column(db.String(40), unique=True, nullable=False, index=True)
    original_order_no = db.Column(db.String(40), nullable=False, index=True)  # 原销售单号
    product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)
    qty = db.Column(db.Integer, nullable=False)
    refund_amount = db.Column(db.Numeric(12, 2), nullable=False)
    reason = db.Column(db.String(256))
    status = db.Column(db.String(16), default="confirmed", index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)


# ==================== P1-9 业务模块扩展 ====================


class StockLot(db.Model):
    """批次/批次号管理（对应 Odoo stock.production.lot）

    用于食品/医药等有保质期要求的商品，按批次追溯库存与到期日。
    StockLot.remaining_qty 与 Product.stock_qty 解耦：
        - Product.stock_qty 为商品总库存（含所有批次）
        - StockLot.remaining_qty 为单批次剩余数量
    """

    __tablename__ = "erp_stock_lot"
    # P1-5: FEFO 出库高频查询（product_id + status + expiry_date）复合索引
    __table_args__ = (db.Index("idx_stock_lot_product_status_expiry", "product_id", "status", "expiry_date"),)
    id = db.Column(db.Integer, primary_key=True)
    lot_no = db.Column(db.String(32), unique=True, nullable=False, index=True)  # 批次号
    product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("erp_supplier.id"), nullable=True)  # 供货来源
    manufacture_date = db.Column(db.DateTime)  # 生产日期
    expiry_date = db.Column(db.DateTime, nullable=True)  # 过期日期（无保质期商品为空）
    quantity = db.Column(db.Integer, nullable=False, default=0)  # 入库数量
    remaining_qty = db.Column(db.Integer, nullable=False, default=0)  # 剩余数量（出库时递减）
    status = db.Column(db.String(16), default="active", index=True)  # active/expired/exhausted
    created_at = db.Column(db.DateTime, default=datetime.now)


class ReorderRule(db.Model):
    """补货预警规则（对应 Odoo stock.warehouse.orderpoint）

    触发条件：Product.stock_qty <= min_qty
    建议补货量：max_qty - stock_qty
    """

    __tablename__ = "erp_reorder_rule"
    # P1-5: (product_id, warehouse_id) 唯一 —— 同品同仓一条规则，防止预警重复触发
    __table_args__ = (db.Index("ux_reorder_rule_product_wh", "product_id", "warehouse_id", unique=True),)
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("erp_warehouse.id"), nullable=True)
    min_qty = db.Column(db.Integer, nullable=False, default=10)  # 最低库存阈值
    max_qty = db.Column(db.Integer, nullable=False, default=50)  # 目标库存上限
    supplier_id = db.Column(db.Integer, db.ForeignKey("erp_supplier.id"), nullable=True)  # 默认补货供应商
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class CostCenter(db.Model):
    """成本中心（对应 Odoo account.analytic.account）

    用于会计凭证按部门/项目/产品线分摊成本，支持预算管控。
    """

    __tablename__ = "erp_cost_center"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(16), unique=True, nullable=False, index=True)  # 如 CC-01
    name = db.Column(db.String(64), nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class Budget(db.Model):
    """预算管理（对应 Odoo account.budget）

    按 cost_center + period + category 维度制定预算，actual_amount 由凭证汇总回填。
    period 格式 YYYY-MM（如 '2026-07'）。
    """

    __tablename__ = "erp_budget"
    # P1-5: (cost_center_id, period, category) 唯一 —— 防止同期同成本中心重复预算，
    # refresh_budget_actual 的聚合口径才会唯一
    __table_args__ = (db.Index("ux_budget_cc_period_cat", "cost_center_id", "period", "category", unique=True),)
    id = db.Column(db.Integer, primary_key=True)
    cost_center_id = db.Column(db.Integer, db.ForeignKey("erp_cost_center.id"), nullable=False, index=True)
    period = db.Column(db.String(7), nullable=False, index=True)  # YYYY-MM
    budget_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    actual_amount = db.Column(db.Numeric(12, 2), default=0)
    category = db.Column(db.String(32), default="general")  # 预算类别
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class ExchangeRate(db.Model):
    """汇率换算（对应 Odoo res.currency.rate）

    用于多币种场景下将外币金额折算为本位币（CNY）。
    get_account_summary 在检测到非 CNY 币种时，按 from_currency + to_currency
    取 date 最新的一条 rate 进行折算。
    """

    __tablename__ = "erp_exchange_rate"
    # P1-5: (from, to, date) 唯一 —— 同日同币对一条汇率（此前迁移里有复合索引但模型缺失）
    __table_args__ = (db.Index("ux_exchange_rate_pair_date", "from_currency", "to_currency", "date", unique=True),)
    id = db.Column(db.Integer, primary_key=True)
    from_currency = db.Column(db.String(3), nullable=False, index=True)  # ISO 4217 三字母代码
    to_currency = db.Column(db.String(3), nullable=False, index=True)  # 通常为 CNY
    rate = db.Column(db.Numeric(18, 8), nullable=False)  # 汇率：1 from_currency = rate to_currency
    date = db.Column(db.Date, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class StockTake(db.Model):
    """库存盘点（对应 Odoo stock.inventory）

    记录盘点单：系统库存 vs 实际库存，差异 diff = actual_qty - system_qty。
    状态流转：draft -> confirmed。
    """

    __tablename__ = "erp_stock_take"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("erp_product.id"), nullable=False, index=True)
    warehouse_id = db.Column(db.Integer, db.ForeignKey("erp_warehouse.id"), nullable=True, index=True)
    system_qty = db.Column(db.Integer, nullable=False, default=0)  # 盘点时系统库存
    actual_qty = db.Column(db.Integer, nullable=False, default=0)  # 实际盘点数量
    diff = db.Column(db.Integer, nullable=False, default=0)  # 差异 = actual_qty - system_qty
    status = db.Column(db.String(16), default="draft", index=True)  # draft/confirmed
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class Customer(db.Model):
    """客户主数据（对应 Odoo res.partner）

    管理客户档案、信用额度与当前余额，支持销售订单信用管控。
    """

    __tablename__ = "erp_customer"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, index=True)
    credit_limit = db.Column(db.Numeric(12, 2), nullable=False, default=0)  # 信用额度
    balance = db.Column(db.Numeric(12, 2), nullable=False, default=0)  # 当前应收余额
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
