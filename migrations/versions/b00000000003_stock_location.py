# -*- coding: utf-8 -*-
"""P3 第二批迁移：库位表 + StockMove 库位列（幂等）。"""
import sqlalchemy as sa
from alembic import op

revision = "b00000000003"
down_revision = "b00000000002"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("erp_stock_location"):
        op.create_table(
            "erp_stock_location",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "warehouse_id",
                sa.Integer(),
                sa.ForeignKey("erp_warehouse.id"),
                nullable=False,
            ),
            sa.Column("code", sa.String(32), nullable=False),
            sa.Column("name", sa.String(64)),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ux_stock_location_wh_code",
            "erp_stock_location",
            ["warehouse_id", "code"],
            unique=True,
        )
        op.create_index(
            op.f("ix_erp_stock_location_warehouse_id"),
            "erp_stock_location",
            ["warehouse_id"],
        )

    move_cols = {c["name"] for c in insp.get_columns("erp_stock_move")}
    if "from_location_id" not in move_cols:
        op.add_column(
            "erp_stock_move",
            sa.Column("from_location_id", sa.Integer(), sa.ForeignKey("erp_stock_location.id"), nullable=True),
        )
    if "to_location_id" not in move_cols:
        op.add_column(
            "erp_stock_move",
            sa.Column("to_location_id", sa.Integer(), sa.ForeignKey("erp_stock_location.id"), nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    move_cols = {c["name"] for c in insp.get_columns("erp_stock_move")}
    if "from_location_id" in move_cols:
        op.drop_column("erp_stock_move", "from_location_id")
    if "to_location_id" in move_cols:
        op.drop_column("erp_stock_move", "to_location_id")
    if insp.has_table("erp_stock_location"):
        op.drop_table("erp_stock_location")
