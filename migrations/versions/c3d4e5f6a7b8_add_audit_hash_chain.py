"""add audit hash chain

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-29 20:00:00

P0-1: 为 AuditLog 添加 prev_hash 和 hash 字段，实现 hash chain 防篡改。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    """新增 audit_log.prev_hash 和 audit_log.hash 字段"""
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.add_column(sa.Column("prev_hash", sa.String(64), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("hash", sa.String(64), nullable=False, server_default="", index=True))


def downgrade():
    """移除 hash chain 字段"""
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_column("hash")
        batch_op.drop_column("prev_hash")
