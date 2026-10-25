# -*- coding: utf-8 -*-
"""P3-RAG 迁移：aigc_chat_history 加 question/sql 列（幂等）。"""
import sqlalchemy as sa
from alembic import op

revision = "b00000000004"
down_revision = "b00000000003"
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("aigc_chat_history")}
    if "question" not in cols:
        op.add_column("aigc_chat_history", sa.Column("question", sa.Text(), nullable=True))
    if "sql" not in cols:
        op.add_column("aigc_chat_history", sa.Column("sql", sa.Text(), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("aigc_chat_history")}
    if "sql" in cols:
        op.drop_column("aigc_chat_history", "sql")
    if "question" in cols:
        op.drop_column("aigc_chat_history", "question")
