"""add unique constraints and composite indexes; merge migration branches (P1-5/M6)

Revision ID: h9i0j1k2l3m4
Revises: c0d1e2f3a4b5, g7h8i9j0k1l2
Create Date: 2026-08-30 20:00:00

两部分职责：

1. 合并迁移分叉 —— c0d1e2f3a4b5 与 g7h8i9j0k1l2 此前都基于 f6a7b8c9d0e1，
   形成 2 个 head，`alembic upgrade head` 在生产会直接报
   "Multiple head revisions are present" 而拒绝执行。本迁移以元组 down_revision
   作为 merge 点，将两条分支收敛为单一链路。

2. 补齐模型层缺失的唯一约束与复合索引，使 alembic 路径与 db.create_all() 路径的
   schema 合流（此前模型 __table_args__ 与迁移各改各的，dev 走 create_all 不会建
   迁移里的复合索引，如 erp_exchange_rate 的 (currency, date) 复合索引）。

修复的重复数据风险点：
  - dws_inventory_snapshot_daily 缺 (product_id, dt) 唯一 → 重复 ETL 库存快照翻倍
  - etl_meta 缺 (layer, table_name) 唯一 → 增量水位线可能双行
  - ods_sale_order / ods_purchase_order 的 order_no 无唯一 → ETL 重复抽取重复入库
  - bom 缺 (product_id, version) 唯一 / bom_item 缺 (bom_id, component) 唯一
  - erp_reorder_rule 缺 (product_id, warehouse_id) 唯一 → 同品同仓重复规则
  - erp_budget 缺 (cost_center_id, period, category) 唯一 → 聚合口径二义
  - erp_exchange_rate 缺 (from, to, date) 唯一 → 同日汇率重复

兼容性：
  - 唯一性全部用 UNIQUE INDEX 表达（PG/SQLite 均无需重建表）
  - 去重 DELETE 保留每组最小 id，幂等可重跑
"""
from alembic import op

# revision identifiers, used by Alembic.
# 元组 down_revision = merge 迁移（收敛 c0d1e2f3a4b5 / g7h8i9j0k1l2 两条分支）
revision = "h9i0j1k2l3m4"
down_revision = ("c0d1e2f3a4b5", "g7h8i9j0k1l2")
branch_labels = None
depends_on = None


# (表名, 唯一索引名, 列, 分组去重键)
_UNIQUE_INDEXES = [
    ("dws_inventory_snapshot_daily", "ux_dws_inventory_product_dt", ["product_id", "dt"]),
    ("etl_meta", "ux_etl_meta_layer_table", ["layer", "table_name"]),
    ("ods_sale_order", "ux_ods_sale_order_no", ["order_no"]),
    ("ods_purchase_order", "ux_ods_purchase_order_no", ["order_no"]),
    ("bom", "ux_bom_product_version", ["product_id", "version"]),
    ("bom_item", "ux_bom_item_bom_component", ["bom_id", "component_product_id"]),
    ("erp_reorder_rule", "ux_reorder_rule_product_wh", ["product_id", "warehouse_id"]),
    ("erp_budget", "ux_budget_cc_period_cat", ["cost_center_id", "period", "category"]),
    ("erp_exchange_rate", "ux_exchange_rate_pair_date", ["from_currency", "to_currency", "date"]),
]

# 普通复合索引（查询热点）
_PLAIN_INDEXES = [
    ("erp_stock_lot", "idx_stock_lot_product_status_expiry", ["product_id", "status", "expiry_date"]),
    ("finance_reconciliation_entry", "idx_recon_entry_batch_source_matched", ["batch_id", "source", "matched"]),
    ("audit_log", "idx_audit_target_type", ["target_type"]),
]


def _dedup(table, columns):
    """删除 (columns) 分组内除最小 id 外的重复行。order_no 允许 NULL（唯一索引不约束 NULL）。"""
    cols = ", ".join(columns)
    null_filter = ""
    if columns == ["order_no"]:
        null_filter = " WHERE order_no IS NOT NULL "
    op.execute(
        f"DELETE FROM {table} WHERE id NOT IN ("
        f"SELECT MIN(id) FROM {table}{null_filter} GROUP BY {cols})"
    )


def upgrade():
    for table, index_name, columns in _UNIQUE_INDEXES:
        _dedup(table, columns)
        op.create_index(index_name, table, columns, unique=True)

    for table, index_name, columns in _PLAIN_INDEXES:
        op.create_index(index_name, table, columns, unique=False)


def downgrade():
    for table, index_name, _columns in reversed(_PLAIN_INDEXES):
        op.drop_index(index_name, table_name=table)

    for table, index_name, _columns in reversed(_UNIQUE_INDEXES):
        op.drop_index(index_name, table_name=table)
