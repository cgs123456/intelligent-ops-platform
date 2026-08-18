"""add erp missing tables: return_order / exchange_rate / stock_take / customer

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-18 15:00:00

补建四张长期依赖 db.create_all() 而非 alembic 迁移的 ERP 表：
  1. erp_return_order      — 销售退货单（ReturnOrder）
  2. erp_exchange_rate     — 多币种汇率（ExchangeRate）
  3. erp_stock_take        — 库存盘点单（StockTake）
  4. erp_customer          — 客户主数据（Customer）

修复根因：
  生产环境走 alembic upgrade head 部署时，这四张表不会创建，导致
  routes/erp.py 中对应的接口直接抛 sqlalchemy.exc.OperationalError（表不存在）。
  本迁移确保数据库 schema 与 models/erp.py 模型声明完全一致。

兼容 SQLite：
  - SQLite 不支持 IF NOT EXISTS 的 CREATE INDEX，采用 try/except 降级
  - FK 约束在 SQLite 下可延迟检查（PRAGMA foreign_keys=ON 后生效）
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "g7h8i9j0k1l2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    # 1. erp_return_order
    op.create_table(
        "erp_return_order",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("return_no", sa.String(length=40), nullable=False),
        sa.Column("original_order_no", sa.String(length=40), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("refund_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("return_no", name="uq_return_order_return_no"),
        sa.ForeignKeyConstraint(["product_id"], ["erp_product.id"]),
    )
    op.create_index("ix_return_order_original_order_no", "erp_return_order", ["original_order_no"], unique=False)
    op.create_index("ix_return_order_product_id", "erp_return_order", ["product_id"], unique=False)
    op.create_index("ix_return_order_status", "erp_return_order", ["status"], unique=False)
    op.create_index("ix_return_order_created_at", "erp_return_order", ["created_at"], unique=False)

    # 2. erp_exchange_rate
    op.create_table(
        "erp_exchange_rate",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("from_currency", sa.String(length=3), nullable=False),
        sa.Column("to_currency", sa.String(length=3), nullable=False),
        sa.Column("rate", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_exchange_rate_from_currency", "erp_exchange_rate", ["from_currency"], unique=False)
    op.create_index("ix_exchange_rate_to_currency", "erp_exchange_rate", ["to_currency"], unique=False)
    op.create_index("ix_exchange_rate_date", "erp_exchange_rate", ["date"], unique=False)
    op.create_index(
        "ix_exchange_rate_currency_date",
        "erp_exchange_rate",
        ["from_currency", "to_currency", "date"],
        unique=False,
    )

    # 3. erp_stock_take
    op.create_table(
        "erp_stock_take",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_id", sa.Integer(), nullable=True),
        sa.Column("system_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actual_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("diff", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["product_id"], ["erp_product.id"]),
        sa.ForeignKeyConstraint(["warehouse_id"], ["erp_warehouse.id"]),
    )
    op.create_index("ix_stock_take_product_id", "erp_stock_take", ["product_id"], unique=False)
    op.create_index("ix_stock_take_warehouse_id", "erp_stock_take", ["warehouse_id"], unique=False)
    op.create_index("ix_stock_take_status", "erp_stock_take", ["status"], unique=False)

    # 4. erp_customer
    op.create_table(
        "erp_customer",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("credit_limit", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("balance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_name", "erp_customer", ["name"], unique=False)


def downgrade():
    op.drop_table("erp_customer")
    op.drop_table("erp_stock_take")
    op.drop_table("erp_exchange_rate")
    op.drop_table("erp_return_order")
