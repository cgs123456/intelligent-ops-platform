"""baseline schema（全链 squash）

Revision ID: b00000000001
Revises: None
Create Date: 2026-10-24

P0 修复：原先 11 个迁移全部是"对已存在库的增量补丁"——链上没有任何建表迁移，
空库（生产首次部署）执行 `flask db upgrade` 会在第一个迁移就报
UndefinedTable/DuplicateTable 崩溃。dev 环境"能用"纯粹是因为 app 启动时
db.create_all() 已把表建好，alembic 只是重复打补丁。

处理方式（项目尚无任何存量部署库，无兼容义务，删除优于兼容）：
  1. 本迁移 = 唯一基线：全部表由当前模型元数据 create_all 生成（模型即真相，
     schema 永远与代码同步，不存在 autogenerate 漂移）；
  2. PostgreSQL 上 audit_log 按原 P2-14 分区策略建为 RANGE(created_at) 声明式
     分区表（月分区 + 默认分区），该逻辑自 f6a7b8c9d0e1 迁移整体搬入；
  3. 删除其余 10 个增量迁移文件；后续 schema 变更一律新增迁移，不再依赖
     create_all 兜底。

SQLite（开发/测试）：audit_log 为普通表，由模型元数据直接生成。
"""
from datetime import datetime, timedelta

import sqlalchemy as sa
from alembic import op

import models  # noqa: F401  # 导入即把全部模型注册进 db.metadata
from extensions import db

# revision identifiers, used by Alembic.
revision = "b00000000001"
down_revision = None
branch_labels = None
depends_on = None


def _month_range(n: int):
    """生成当前月份起 n 个月的 (分区名, 起始日期, 结束日期) 三元组。

    起始日期为当月 1 号 00:00:00，结束日期为下月 1 号 00:00:00（左闭右开）。
    """
    today = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    parts = []
    for i in range(n):
        start = today + timedelta(days=32 * i)
        start = start.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
        name = f"audit_log_{start.strftime('%Y_%m')}"
        parts.append((name, start, end))
    return parts


def _create_partitioned_audit_log():
    """PostgreSQL：audit_log 建为 RANGE(created_at) 声明式分区表（P2-14）。

    分区键必须纳入主键，故主键为 (id, created_at)，与模型 AuditLog 的
    __table_args__ 一致。分区表索引自动应用到所有子分区。

    dev 环境 app 启动会先 db.create_all() 抢跑（audit_log 已作为普通表存在），
    此处守卫跳过，保证迁移幂等；生产空库走完整分区建表路径。
    """
    import sqlalchemy as sa

    bind = op.get_bind()
    if sa.inspect(bind).has_table("audit_log"):
        return
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("hash", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", "created_at"),
        postgresql_partition_by="RANGE (created_at)",
    )
    op.create_index(
        "idx_audit_actor_action_created",
        "audit_log",
        ["actor", "action", "created_at"],
    )
    op.create_index("idx_audit_target_type", "audit_log", ["target_type"])
    op.create_index("ix_audit_log_actor", "audit_log", ["actor"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_request_id", "audit_log", ["request_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_hash", "audit_log", ["hash"])

    # 未来 6 个月月分区 + 默认分区（捕获不在任何显式分区内的数据，避免插入失败）
    for name, start, end in _month_range(6):
        op.execute(
            f"CREATE TABLE {name} PARTITION OF audit_log "
            f"FOR VALUES FROM ('{start.strftime('%Y-%m-%d')}') "
            f"TO ('{end.strftime('%Y-%m-%d')}')"
        )
    op.execute("CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT")


def _ensure_default_tenant():
    """幂等插入默认租户 id=1（多租户业务行 FK 指向它，生产 PG 严格外键）"""
    from sqlalchemy import text

    bind = op.get_bind()
    if bind.execute(text("SELECT 1 FROM sys_tenant WHERE id = 1")).first():
        return
    bind.execute(
        text(
            "INSERT INTO sys_tenant (id, code, name, is_active, created_at) "
            # TRUE 字面量：PG boolean 列拒绝整数 1（psycopg 无隐式转换），SQLite 3.23+ 同样支持
            "VALUES (1, 'default', '默认租户', TRUE, CURRENT_TIMESTAMP)"
        )
    )


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # 除 audit_log 外全部表由模型元数据生成（checkfirst 幂等）
        tables = [t for t in db.metadata.sorted_tables if t.name != "audit_log"]
        db.metadata.create_all(bind=bind, tables=tables)
        _create_partitioned_audit_log()
        _ensure_default_tenant()
    else:
        # SQLite：不支持分区，audit_log 由模型生成普通表
        db.metadata.create_all(bind=bind)
        _ensure_default_tenant()


def downgrade():
    # PG 上 DROP 父分区表会级联删除全部子分区（含默认分区）
    db.metadata.drop_all(bind=op.get_bind())
