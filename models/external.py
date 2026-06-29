"""外部数据源模型（供 RPA 采集）"""
from datetime import datetime, date
from extensions import db


class ExtSupplierQuote(db.Model):
    """外部供应商报价（模拟供应商网站数据）"""
    __tablename__ = 'ext_supplier_quote'
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, nullable=False, index=True)
    product_id = db.Column(db.Integer, nullable=False, index=True)
    quote_price = db.Column(db.Numeric(10, 2), nullable=False)
    quote_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    source = db.Column(db.String(64), default='供应商官网')
    collected_at = db.Column(db.DateTime, default=datetime.now)


class ExtEcommerceOrder(db.Model):
    """外部电商订单（模拟电商平台后台数据）"""
    __tablename__ = 'ext_ecommerce_order'
    __table_args__ = (
        db.Index('idx_extorder_synced_platform', 'synced', 'platform'),
    )
    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(32), nullable=False, index=True)
    product_sku = db.Column(db.String(32), nullable=False, index=True)
    qty = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    customer = db.Column(db.String(64), nullable=False)
    order_time = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    synced = db.Column(db.Boolean, default=False, index=True)
    sync_error = db.Column(db.String(256))  # 同步失败原因
    retry_count = db.Column(db.Integer, default=0)  # 重试次数
