"""add finance reconciliation + bom/mrp modules

Revision ID: a7b8c9d0e1f2
Revises: e5f6a7b8c9d0
Create Date: 2026-07-30 00:00:00

P2-17 新增模块：
  1. 财务对账（finance_reconciliation_batch / finance_reconciliation_entry）
     用于 ERP 账务与银行流水/外部系统对账，按期间批次化处理。
  2. BOM 物料清单（bom / bom_item）
     支持按产品维护多版本 BOM，含损耗率、行序号。
  3. MRP 物料需求计划（mrp_plan / mrp_requirement）
     基于 BOM 展开子件净需求与建议下单量。

注意：
  - 所有新表均不破坏既有功能，纯新增
  - Decimal 字段：财务用 Numeric(18,2)，物料用 Numeric(18,4)（精度更高）
  - 索引：batch_no / status / period / entry_date / code / plan_no 等高频过滤字段
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a7b8c9d0e1f2"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    """创建财务对账 + BOM/MRP 表"""

    # ---------- 1. 财务对账 ----------

    # 1.1 对账批次
    op.create_table(
        "finance_reconciliation_batch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_no", sa.String(length=40), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("total_system_amount", sa.Numeric(precision=18, scale=2), nullable=False, server_default="0"),
        sa.Column("total_external_amount", sa.Numeric(precision=18, scale=2), nullable=False, server_default="0"),
        sa.Column("discrepancy_amount", sa.Numeric(precision=18, scale=2), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_no", name="uq_finance_batch_no"),
    )
    op.create_index("ix_finance_recon_batch_batch_no", "finance_reconciliation_batch", ["batch_no"], unique=False)
    op.create_index("ix_finance_recon_batch_period", "finance_reconciliation_batch", ["period"], unique=False)
    op.create_index("ix_finance_recon_batch_status", "finance_reconciliation_batch", ["status"], unique=False)
    op.create_index("ix_finance_recon_batch_created_at", "finance_reconciliation_batch", ["created_at"], unique=False)

    # 1.2 对账明细（自引用 FK，需在表创建后单独 add）
    op.create_table(
        "finance_reconciliation_entry",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("account_code", sa.String(length=32), nullable=False),
        sa.Column("entry_date", sa.DateTime(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("reference_no", sa.String(length=64), nullable=True),
        sa.Column("matched", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("matched_entry_id", sa.Integer(), nullable=True),
        sa.Column("mismatch_reason", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["batch_id"], ["finance_reconciliation_batch.id"]),
        sa.ForeignKeyConstraint(["matched_entry_id"], ["finance_reconciliation_entry.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_finance_recon_entry_batch_id", "finance_reconciliation_entry", ["batch_id"], unique=False)
    op.create_index("ix_finance_recon_entry_source", "finance_reconciliation_entry", ["source"], unique=False)
    op.create_index("ix_finance_recon_entry_entry_date", "finance_reconciliation_entry", ["entry_date"], unique=False)
    op.create_index("ix_finance_recon_entry_reference_no", "finance_reconciliation_entry", ["reference_no"], unique=False)
    op.create_index("ix_finance_recon_entry_matched", "finance_reconciliation_entry", ["matched"], unique=False)

    # ---------- 2. BOM 物料清单 ----------

    # 2.1 BOM 头
    op.create_table(
        "bom",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False, server_default="1.0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False, server_default="1"),
        sa.Column("unit", sa.String(length=8), nullable=False, server_default="个"),
        sa.Column("description", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["erp_product.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_bom_code"),
    )
    op.create_index("ix_bom_product_id", "bom", ["product_id"], unique=False)
    op.create_index("ix_bom_code", "bom", ["code"], unique=False)
    op.create_index("ix_bom_status", "bom", ["status"], unique=False)

    # 2.2 BOM 行
    op.create_table(
        "bom_item",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bom_id", sa.Integer(), nullable=False),
        sa.Column("component_product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("unit", sa.String(length=8), nullable=False, server_default="个"),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("scrap_rate", sa.Numeric(precision=5, scale=4), nullable=False, server_default="0"),
        sa.Column("notes", sa.String(length=256), nullable=True),
        sa.ForeignKeyConstraint(["bom_id"], ["bom.id"]),
        sa.ForeignKeyConstraint(["component_product_id"], ["erp_product.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bom_item_bom_id", "bom_item", ["bom_id"], unique=False)
    op.create_index("ix_bom_item_component_product_id", "bom_item", ["component_product_id"], unique=False)

    # ---------- 3. MRP 物料需求计划 ----------

    # 3.1 MRP 计划单
    op.create_table(
        "mrp_plan",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_no", sa.String(length=40), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("bom_id", sa.Integer(), nullable=True),
        sa.Column("planned_qty", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("planned_date", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["erp_product.id"]),
        sa.ForeignKeyConstraint(["bom_id"], ["bom.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_no", name="uq_mrp_plan_no"),
    )
    op.create_index("ix_mrp_plan_plan_no", "mrp_plan", ["plan_no"], unique=False)
    op.create_index("ix_mrp_plan_product_id", "mrp_plan", ["product_id"], unique=False)
    op.create_index("ix_mrp_plan_bom_id", "mrp_plan", ["bom_id"], unique=False)
    op.create_index("ix_mrp_plan_status", "mrp_plan", ["status"], unique=False)

    # 3.2 MRP 需求明细
    op.create_table(
        "mrp_requirement",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("component_product_id", sa.Integer(), nullable=False),
        sa.Column("required_qty", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("on_hand_qty", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("net_requirement", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("suggested_order_qty", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("suggested_order_date", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["mrp_plan.id"]),
        sa.ForeignKeyConstraint(["component_product_id"], ["erp_product.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mrp_requirement_plan_id", "mrp_requirement", ["plan_id"], unique=False)
    op.create_index("ix_mrp_requirement_component_product_id", "mrp_requirement", ["component_product_id"], unique=False)


def downgrade():
    """回滚 P2-17：删除财务对账 + BOM/MRP 表"""

    # 3.2 删除 MRP 需求明细
    op.drop_index("ix_mrp_requirement_component_product_id", table_name="mrp_requirement")
    op.drop_index("ix_mrp_requirement_plan_id", table_name="mrp_requirement")
    op.drop_table("mrp_requirement")

    # 3.1 删除 MRP 计划单
    op.drop_index("ix_mrp_plan_status", table_name="mrp_plan")
    op.drop_index("ix_mrp_plan_bom_id", table_name="mrp_plan")
    op.drop_index("ix_mrp_plan_product_id", table_name="mrp_plan")
    op.drop_index("ix_mrp_plan_plan_no", table_name="mrp_plan")
    op.drop_table("mrp_plan")

    # 2.2 删除 BOM 行
    op.drop_index("ix_bom_item_component_product_id", table_name="bom_item")
    op.drop_index("ix_bom_item_bom_id", table_name="bom_item")
    op.drop_table("bom_item")

    # 2.1 删除 BOM 头
    op.drop_index("ix_bom_status", table_name="bom")
    op.drop_index("ix_bom_code", table_name="bom")
    op.drop_index("ix_bom_product_id", table_name="bom")
    op.drop_table("bom")

    # 1.2 删除对账明细
    op.drop_index("ix_finance_recon_entry_matched", table_name="finance_reconciliation_entry")
    op.drop_index("ix_finance_recon_entry_reference_no", table_name="finance_reconciliation_entry")
    op.drop_index("ix_finance_recon_entry_entry_date", table_name="finance_reconciliation_entry")
    op.drop_index("ix_finance_recon_entry_source", table_name="finance_reconciliation_entry")
    op.drop_index("ix_finance_recon_entry_batch_id", table_name="finance_reconciliation_entry")
    op.drop_table("finance_reconciliation_entry")

    # 1.1 删除对账批次
    op.drop_index("ix_finance_recon_batch_created_at", table_name="finance_reconciliation_batch")
    op.drop_index("ix_finance_recon_batch_status", table_name="finance_reconciliation_batch")
    op.drop_index("ix_finance_recon_batch_period", table_name="finance_reconciliation_batch")
    op.drop_index("ix_finance_recon_batch_batch_no", table_name="finance_reconciliation_batch")
    op.drop_table("finance_reconciliation_batch")
