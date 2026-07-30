"""add business modules: currency/tax + stock lot + reorder rule + cost center + budget

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-29 22:00:00

P1-9 业务模块扩展（合并迁移）：
  1. 多币种 + 多税率支持：
     - Product / PurchaseOrder / SaleOrder 添加 currency_code(String(3) default 'CNY')、
       tax_rate(Numeric(5,4) default 0.13)
     - AccountMove 添加 tax_amount(Numeric(12,2) default 0)、currency_code(String(3) default 'CNY')、
       cost_center_id(FK to erp_cost_center.id, nullable)
  2. 批次管理 StockLot：新表 erp_stock_lot + StockMove.lot_id FK
  3. 库存预警规则 ReorderRule：新表 erp_reorder_rule
  4. 成本中心 CostCenter：新表 erp_cost_center（AccountMove.cost_center_id 引用此表）
  5. 预算管理 Budget：新表 erp_budget

注意：
  - SQLite batch_alter_table 兼容（开发/测试环境）
  - 新增列均带 server_default，保证旧数据迁移后可用
  - AccountMove.cost_center_id 必须在 erp_cost_center 表创建之后添加（外键依赖）
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    """P1-9 业务模块扩展（合并）"""

    # ---------- 1. 新建表（先建被依赖的表） ----------

    # 1.1 成本中心（AccountMove.cost_center_id 引用此表，必须先建）
    op.create_table(
        "erp_cost_center",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_cost_center_code"),
    )
    op.create_index("ix_erp_cost_center_code", "erp_cost_center", ["code"], unique=False)

    # 1.2 批次管理 StockLot
    op.create_table(
        "erp_stock_lot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lot_no", sa.String(length=32), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("manufacture_date", sa.DateTime(), nullable=True),
        sa.Column("expiry_date", sa.DateTime(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("remaining_qty", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["erp_product.id"]),
        sa.ForeignKeyConstraint(["supplier_id"], ["erp_supplier.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lot_no", name="uq_stock_lot_lot_no"),
    )
    op.create_index("ix_erp_stock_lot_lot_no", "erp_stock_lot", ["lot_no"], unique=False)
    op.create_index("ix_erp_stock_lot_product_id", "erp_stock_lot", ["product_id"], unique=False)
    op.create_index("ix_erp_stock_lot_status", "erp_stock_lot", ["status"], unique=False)

    # 1.3 补货预警规则 ReorderRule
    op.create_table(
        "erp_reorder_rule",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("min_qty", sa.Integer(), nullable=False),
        sa.Column("max_qty", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["product_id"], ["erp_product.id"]),
        sa.ForeignKeyConstraint(["supplier_id"], ["erp_supplier.id"]),
        sa.ForeignKeyConstraint(["warehouse_id"], ["erp_warehouse.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_erp_reorder_rule_product_id", "erp_reorder_rule", ["product_id"], unique=False)

    # 1.4 预算管理 Budget
    op.create_table(
        "erp_budget",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cost_center_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("budget_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("actual_amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["cost_center_id"], ["erp_cost_center.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_erp_budget_cost_center_id", "erp_budget", ["cost_center_id"], unique=False)
    op.create_index("ix_erp_budget_period", "erp_budget", ["period"], unique=False)

    # ---------- 2. 既有表新增字段 ----------

    # 2.1 Product 添加 currency_code / tax_rate
    with op.batch_alter_table("erp_product", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("currency_code", sa.String(length=3), nullable=False, server_default="CNY")
        )
        batch_op.add_column(
            sa.Column("tax_rate", sa.Numeric(precision=5, scale=4), nullable=False, server_default="0.1300")
        )

    # 2.2 PurchaseOrder 添加 currency_code / tax_rate
    with op.batch_alter_table("erp_purchase_order", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("currency_code", sa.String(length=3), nullable=False, server_default="CNY")
        )
        batch_op.add_column(
            sa.Column("tax_rate", sa.Numeric(precision=5, scale=4), nullable=False, server_default="0.1300")
        )

    # 2.3 SaleOrder 添加 currency_code / tax_rate
    with op.batch_alter_table("erp_sale_order", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("currency_code", sa.String(length=3), nullable=False, server_default="CNY")
        )
        batch_op.add_column(
            sa.Column("tax_rate", sa.Numeric(precision=5, scale=4), nullable=False, server_default="0.1300")
        )

    # 2.4 StockMove 添加 lot_id（FK to erp_stock_lot）
    with op.batch_alter_table("erp_stock_move", schema=None) as batch_op:
        batch_op.add_column(sa.Column("lot_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_stockmove_stocklot", "erp_stock_lot", ["lot_id"], ["id"]
        )
    op.create_index("ix_erp_stock_move_lot_id", "erp_stock_move", ["lot_id"], unique=False)

    # 2.5 AccountMove 添加 tax_amount / currency_code / cost_center_id
    #      （cost_center_id 引用 erp_cost_center，表已在 1.1 创建）
    with op.batch_alter_table("erp_account_move", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("tax_amount", sa.Numeric(precision=12, scale=2), nullable=True, server_default="0")
        )
        batch_op.add_column(
            sa.Column("currency_code", sa.String(length=3), nullable=True, server_default="CNY")
        )
        batch_op.add_column(sa.Column("cost_center_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_accountmove_costcenter", "erp_cost_center", ["cost_center_id"], ["id"]
        )
    op.create_index(
        "ix_erp_account_move_cost_center_id", "erp_account_move", ["cost_center_id"], unique=False
    )


def downgrade():
    """回滚 P1-9 业务模块扩展"""

    # 2.5 AccountMove 移除字段
    with op.batch_alter_table("erp_account_move", schema=None) as batch_op:
        batch_op.drop_index("ix_erp_account_move_cost_center_id")
        batch_op.drop_constraint("fk_accountmove_costcenter", type_="foreignkey")
        batch_op.drop_column("cost_center_id")
        batch_op.drop_column("currency_code")
        batch_op.drop_column("tax_amount")

    # 2.4 StockMove 移除 lot_id
    with op.batch_alter_table("erp_stock_move", schema=None) as batch_op:
        batch_op.drop_index("ix_erp_stock_move_lot_id")
        batch_op.drop_constraint("fk_stockmove_stocklot", type_="foreignkey")
        batch_op.drop_column("lot_id")

    # 2.3 SaleOrder 移除字段
    with op.batch_alter_table("erp_sale_order", schema=None) as batch_op:
        batch_op.drop_column("tax_rate")
        batch_op.drop_column("currency_code")

    # 2.2 PurchaseOrder 移除字段
    with op.batch_alter_table("erp_purchase_order", schema=None) as batch_op:
        batch_op.drop_column("tax_rate")
        batch_op.drop_column("currency_code")

    # 2.1 Product 移除字段
    with op.batch_alter_table("erp_product", schema=None) as batch_op:
        batch_op.drop_column("tax_rate")
        batch_op.drop_column("currency_code")

    # 1.4 删除 Budget
    op.drop_index("ix_erp_budget_period", table_name="erp_budget")
    op.drop_index("ix_erp_budget_cost_center_id", table_name="erp_budget")
    op.drop_table("erp_budget")

    # 1.3 删除 ReorderRule
    op.drop_index("ix_erp_reorder_rule_product_id", table_name="erp_reorder_rule")
    op.drop_table("erp_reorder_rule")

    # 1.2 删除 StockLot
    op.drop_index("ix_erp_stock_lot_status", table_name="erp_stock_lot")
    op.drop_index("ix_erp_stock_lot_product_id", table_name="erp_stock_lot")
    op.drop_index("ix_erp_stock_lot_lot_no", table_name="erp_stock_lot")
    op.drop_table("erp_stock_lot")

    # 1.1 删除 CostCenter
    op.drop_index("ix_erp_cost_center_code", table_name="erp_cost_center")
    op.drop_table("erp_cost_center")
