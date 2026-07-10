"""FDE 数仓分层模型
ODS 贴源 / DWD 明细 / DWS 汇总 / ADS 应用
+ ETL 元数据 + 数据质量日志 + 血缘追踪
"""
from datetime import date, datetime

from extensions import db

# ==================== ODS 贴源层 ====================

class OdsSaleOrder(db.Model):
    __tablename__ = 'ods_sale_order'
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(40), index=True)
    product_id = db.Column(db.Integer, index=True)
    qty = db.Column(db.Integer)
    unit_price = db.Column(db.Numeric(10, 2))
    customer = db.Column(db.String(64))
    platform = db.Column(db.String(32))
    dt = db.Column(db.Date, index=True)
    etl_time = db.Column(db.DateTime, default=datetime.now)
    etl_batch = db.Column(db.String(32))  # ETL 批次号


class OdsPurchaseOrder(db.Model):
    __tablename__ = 'ods_purchase_order'
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(40), index=True)
    supplier_id = db.Column(db.Integer)
    product_id = db.Column(db.Integer, index=True)
    qty = db.Column(db.Integer)
    unit_price = db.Column(db.Numeric(10, 2))
    status = db.Column(db.String(16))
    dt = db.Column(db.Date, index=True)
    etl_time = db.Column(db.DateTime, default=datetime.now)
    etl_batch = db.Column(db.String(32))


class OdsStockMove(db.Model):
    __tablename__ = 'ods_stock_move'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, index=True)
    qty = db.Column(db.Integer)
    move_type = db.Column(db.String(16))
    ref_order = db.Column(db.String(40))
    dt = db.Column(db.Date, index=True)
    etl_time = db.Column(db.DateTime, default=datetime.now)
    etl_batch = db.Column(db.String(32))


# ==================== DWD 明细层 ====================

class DwdSalesFact(db.Model):
    """销售事实表（星型模型事实表）"""
    __tablename__ = 'dwd_sales_order_di'
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(40), index=True)
    product_id = db.Column(db.Integer, index=True)
    product_name = db.Column(db.String(64))
    qty = db.Column(db.Integer)
    amount = db.Column(db.Numeric(12, 2))
    customer = db.Column(db.String(64))
    platform = db.Column(db.String(32))
    dt = db.Column(db.Date, index=True)


class DwdStockFact(db.Model):
    """库存移动事实表"""
    __tablename__ = 'dwd_stock_move_di'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, index=True)
    product_name = db.Column(db.String(64))
    qty = db.Column(db.Integer)
    move_type = db.Column(db.String(16))
    ref_order = db.Column(db.String(40))
    dt = db.Column(db.Date, index=True)


# ==================== DWS 汇总层 ====================

class DwsSalesSkuDaily(db.Model):
    """SKU 日销售汇总"""
    __tablename__ = 'dws_sales_sku_daily'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, index=True)
    product_name = db.Column(db.String(64))
    dt = db.Column(db.Date, index=True)
    sale_qty = db.Column(db.Integer)
    sale_amount = db.Column(db.Numeric(12, 2))


class DwsInventoryDaily(db.Model):
    """库存日快照"""
    __tablename__ = 'dws_inventory_snapshot_daily'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, index=True)
    product_name = db.Column(db.String(64))
    dt = db.Column(db.Date, index=True)
    stock_qty = db.Column(db.Integer)


# ==================== ADS 应用层 ====================

class AdsReplenishmentSuggest(db.Model):
    """补货建议源数据"""
    __tablename__ = 'ads_replenishment_suggest'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, index=True)
    product_name = db.Column(db.String(64))
    recent_7d_sales = db.Column(db.Integer)
    current_stock = db.Column(db.Integer)
    in_transit = db.Column(db.Integer)
    suggested_qty = db.Column(db.Integer)
    suggested_supplier_id = db.Column(db.Integer)
    suggested_supplier_name = db.Column(db.String(64))
    reason = db.Column(db.Text)
    dt = db.Column(db.Date, index=True)


class AdsDailyOpsReport(db.Model):
    """经营日报"""
    __tablename__ = 'ads_daily_ops_report'
    id = db.Column(db.Integer, primary_key=True)
    dt = db.Column(db.Date, unique=True, index=True)
    total_sales_amount = db.Column(db.Numeric(12, 2))
    total_purchase_amount = db.Column(db.Numeric(12, 2))
    total_sale_qty = db.Column(db.Integer)
    inventory_value = db.Column(db.Numeric(12, 2))
    top_sku = db.Column(db.String(64))
    low_stock_count = db.Column(db.Integer)


# ==================== ETL 元数据与治理 ====================

class EtlMeta(db.Model):
    """ETL 执行元数据（增量拉取的水位线）"""
    __tablename__ = 'etl_meta'
    id = db.Column(db.Integer, primary_key=True)
    layer = db.Column(db.String(8), nullable=False, index=True)  # ODS/DWD/DWS/ADS
    table_name = db.Column(db.String(64), nullable=False, index=True)
    last_run_at = db.Column(db.DateTime, default=datetime.now)
    last_success_at = db.Column(db.DateTime)
    last_watermark = db.Column(db.DateTime)  # 增量水位线（最后处理的业务时间）
    rows_processed = db.Column(db.Integer, default=0)
    status = db.Column(db.String(16), default='pending')  # pending/running/success/failed
    error_msg = db.Column(db.Text)


class DataQualityLog(db.Model):
    """数据质量测试日志（模拟 dbt tests）"""
    __tablename__ = 'data_quality_log'
    id = db.Column(db.Integer, primary_key=True)
    table_name = db.Column(db.String(64), nullable=False, index=True)
    test_name = db.Column(db.String(64), nullable=False)  # not_null/unique/accepted_values/range
    column_name = db.Column(db.String(64))
    status = db.Column(db.String(16), nullable=False)  # pass/fail
    failures = db.Column(db.Integer, default=0)
    detail = db.Column(db.Text)
    checked_at = db.Column(db.DateTime, default=datetime.now, index=True)


class DataLineage(db.Model):
    """数据血缘追踪（上游表 → 下游表）"""
    __tablename__ = 'data_lineage'
    id = db.Column(db.Integer, primary_key=True)
    upstream_table = db.Column(db.String(64), nullable=False, index=True)
    downstream_table = db.Column(db.String(64), nullable=False, index=True)
    layer = db.Column(db.String(8))  # ODS→DWD / DWD→DWS / DWS→ADS
    transformation = db.Column(db.String(128))  # 转换逻辑描述
    created_at = db.Column(db.DateTime, default=datetime.now)
