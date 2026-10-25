"""business enhancements: SKU 分级列 + 会计期间表

Revision ID: b00000000002
Revises: b00000000001
Create Date: 2026-10-25

业务功能增强（P3 首批）：
  1. ads_replenishment_suggest 新增 ABC/XYZ 分级列
     （abc_class/xyz_class/revenue_share_90d/cv_90d，滚动 90 天窗口 ETL 重算）；
  2. 新增 erp_accounting_period 会计期间表（关账后期间禁止过账）。

幂等说明：空库路径下 baseline 的 create_all 按最新模型建表，新列/新表已存在，
本迁移用 inspector 检查后跳过，避免 Duplicate column/table（b2 前车之鉴）。
"""
import sqlalchemy as sa
from alembic import op

import models  # noqa: F401  # 导入即把全部模型注册进 db.metadata
from extensions import db

# revision identifiers, used by Alembic.
revision = "b00000000002"
down_revision = "b00000000001"
branch_labels = None
depends_on = None


def _cols(bind, table):
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return None
    return {c["name"] for c in insp.get_columns(table)}


def upgrade():
    bind = op.get_bind()

    # 1. ads_replenishment_suggest 分级列
    cols = _cols(bind, "ads_replenishment_suggest")
    if cols is not None:
        if "abc_class" not in cols:
            op.add_column("ads_replenishment_suggest", sa.Column("abc_class", sa.String(1), nullable=True))
        if "xyz_class" not in cols:
            op.add_column("ads_replenishment_suggest", sa.Column("xyz_class", sa.String(1), nullable=True))
        if "revenue_share_90d" not in cols:
            op.add_column("ads_replenishment_suggest", sa.Column("revenue_share_90d", sa.Numeric(8, 6), nullable=True))
        if "cv_90d" not in cols:
            op.add_column("ads_replenishment_suggest", sa.Column("cv_90d", sa.Numeric(8, 4), nullable=True))

    # 2. 会计期间表
    if _cols(bind, "erp_accounting_period") is None:
        op.create_table(
            "erp_accounting_period",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(7), nullable=False),
            sa.Column("is_closed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("closed_at", sa.DateTime(), nullable=True),
            sa.Column("closed_by", sa.String(64), nullable=True),
        )
        op.create_index("ix_erp_accounting_period_name", "erp_accounting_period", ["name"], unique=True)


def downgrade():
    bind = op.get_bind()
    cols = _cols(bind, "ads_replenishment_suggest")
    if cols is not None:
        for col in ("abc_class", "xyz_class", "revenue_share_90d", "cv_90d"):
            if col in cols:
                op.drop_column("ads_replenishment_suggest", col)
    if _cols(bind, "erp_accounting_period") is not None:
        op.drop_index("ix_erp_accounting_period_name", table_name="erp_accounting_period")
        op.drop_table("erp_accounting_period")
