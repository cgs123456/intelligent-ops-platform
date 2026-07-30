"""add token_version to sys_user

Revision ID: a1b2c3d4e5f6
Revises: 4e06934ab2d0
Create Date: 2026-07-10 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "4e06934ab2d0"
branch_labels = None
depends_on = None


def upgrade():
    """新增 sys_user.token_version 字段（P0-1: 改密后使旧 token 失效）"""
    # SQLite 不支持直接 add column with default，用 batch_alter_table 兼容
    with op.batch_alter_table("sys_user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    """移除 sys_user.token_version 字段"""
    with op.batch_alter_table("sys_user", schema=None) as batch_op:
        batch_op.drop_column("token_version")
