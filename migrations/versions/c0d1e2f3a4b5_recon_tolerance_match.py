"""P0-ERP5: reconciliation entry tolerance matching support

Revision ID: c0d1e2f3a4b5
Revises: f6a7b8c9d0e1
Create Date: 2026-07-30 03:00:00.000000

P0-ERP5: 对账金额改为容差匹配
  - finance_reconciliation_entry 新增 tax_rate 字段（系统侧明细税率，用于计算匹配容差）
  - finance_reconciliation_entry 新增 match_type 字段（匹配类型 exact/tolerance，便于审计）

背景：AccountMove.amount 为不含税金额，银行流水为含税实收金额，
      原 (reference_no, amount) 精确匹配必然失败。改为容差匹配：
      |system_amount - external_amount| <= max(system_amount * tax_rate, 0.01)
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c0d1e2f3a4b5"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    """P0-ERP5: 对账明细表新增 tax_rate / match_type 字段"""
    with op.batch_alter_table("finance_reconciliation_entry", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("tax_rate", sa.Numeric(precision=5, scale=4), nullable=True)
        )
        batch_op.add_column(sa.Column("match_type", sa.String(length=16), nullable=True))


def downgrade():
    """回滚容差匹配字段"""
    with op.batch_alter_table("finance_reconciliation_entry", schema=None) as batch_op:
        batch_op.drop_column("match_type")
        batch_op.drop_column("tax_rate")
