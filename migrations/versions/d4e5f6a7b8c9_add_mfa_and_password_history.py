"""add mfa and password history

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-29 21:00:00

P0-8: 为 User 添加 MFA TOTP 字段（mfa_secret/mfa_enabled/mfa_backup_codes）
      和密码历史字段（password_history）
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    """新增 MFA + 密码历史字段"""
    with op.batch_alter_table("sys_user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mfa_secret", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("mfa_backup_codes", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("password_history", sa.Text(), nullable=True))


def downgrade():
    """移除 MFA + 密码历史字段"""
    with op.batch_alter_table("sys_user", schema=None) as batch_op:
        batch_op.drop_column("password_history")
        batch_op.drop_column("mfa_backup_codes")
        batch_op.drop_column("mfa_enabled")
        batch_op.drop_column("mfa_secret")
