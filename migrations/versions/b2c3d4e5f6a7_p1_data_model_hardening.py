"""P1 data model hardening: foreign keys + composite indexes + request_id

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-29 20:00:00.000000

P1-12: 关键外键约束（SuggestionFeedback→Suggestion, TokenBlacklist→User）
P1-13: 高频查询复合索引（AuditLog/ChatHistory/DwsSalesSkuDaily/AdsReplenishmentSuggest/TokenBlacklist）
P1-17: AuditLog 加 request_id 字段关联请求日志
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    """P1 数据模型加固"""
    # P1-17: AuditLog 新增 request_id 字段
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.add_column(sa.Column("request_id", sa.String(length=64), nullable=True))
        batch_op.create_index("idx_audit_actor_action_created", ["actor", "action", "created_at"], unique=False)

    # P1-13: ChatHistory 复合索引
    with op.batch_alter_table("aigc_chat_history", schema=None) as batch_op:
        batch_op.create_index("idx_chat_session_created", ["session_id", "created_at"], unique=False)

    # P1-12: SuggestionFeedback 加外键约束（SQLite batch 模式下需重建表）
    with op.batch_alter_table("aigc_suggestion_feedback", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_feedback_suggestion", "aigc_suggestion", ["suggestion_id"], ["id"], ondelete="CASCADE"
        )

    # P1-12 + P1-13: TokenBlacklist 加外键 + 复合索引
    with op.batch_alter_table("sys_token_blacklist", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_tokenblacklist_user", "sys_user", ["user_id"], ["id"], ondelete="CASCADE"
        )
        batch_op.create_index("idx_token_user_expires", ["user_id", "expires_at"], unique=False)

    # P1-13: DwsSalesSkuDaily 唯一约束（防止重复 ETL 写入）
    with op.batch_alter_table("dws_sales_sku_daily", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_dws_sales_sku_product_dt", ["product_id", "dt"])

    # P1-13: AdsReplenishmentSuggest 复合索引
    with op.batch_alter_table("ads_replenishment_suggest", schema=None) as batch_op:
        batch_op.create_index("idx_ads_replenishment_product_dt", ["product_id", "dt"], unique=False)


def downgrade():
    """回滚数据模型加固"""
    with op.batch_alter_table("ads_replenishment_suggest", schema=None) as batch_op:
        batch_op.drop_index("idx_ads_replenishment_product_dt")

    with op.batch_alter_table("dws_sales_sku_daily", schema=None) as batch_op:
        batch_op.drop_constraint("uq_dws_sales_sku_product_dt", type_="unique")

    with op.batch_alter_table("sys_token_blacklist", schema=None) as batch_op:
        batch_op.drop_index("idx_token_user_expires")
        batch_op.drop_constraint("fk_tokenblacklist_user", type_="foreignkey")

    with op.batch_alter_table("aigc_suggestion_feedback", schema=None) as batch_op:
        batch_op.drop_constraint("fk_feedback_suggestion", type_="foreignkey")

    with op.batch_alter_table("aigc_chat_history", schema=None) as batch_op:
        batch_op.drop_index("idx_chat_session_created")

    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_index("idx_audit_actor_action_created")
        batch_op.drop_column("request_id")
