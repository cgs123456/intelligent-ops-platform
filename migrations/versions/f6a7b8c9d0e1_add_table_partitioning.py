"""add table partitioning for audit_log (monthly range partitions)

Revision ID: f6a7b8c9d0e1
Revises: a7b8c9d0e1f2
Create Date: 2026-07-30 02:00:00

P2-14: audit_log 表分区策略

分区策略原因：
  audit_log 是审计日志表，写入量大且持续增长，单表过大会拖慢按时间范围
  的查询与归档操作。按 created_at 字段做 RANGE 分区（按月）后：
    1. 按月查询/归档只需扫描对应分区，IO 大幅降低；
    2. 历史分区可单独归档/删除（DROP PARTITION 比 DELETE 快几个数量级）；
    3. VACUUM/索引维护可按分区执行，避免全表锁；
    4. 配合只读从库做报表查询，分区裁剪显著降低延迟。

迁移分步策略（audit_log 已有数据，需在线平滑迁移）：
  1. 创建新分区表 audit_log_partitioned（结构与原表一致，含 hash chain 字段）；
  2. 将旧表数据迁移到新分区表（按 created_at 分发到各月分区）；
  3. 删除旧 audit_log 表；
  4. 重命名 audit_log_partitioned 为 audit_log。

同时创建当前月份起未来 6 个月的分区（用 for 循环生成）。

注意：
  - PostgreSQL 原生支持声明式分区；SQLite 不支持分区，本迁移在 SQLite 下
    会降级为普通表（仅创建结构，不创建分区子表），保证开发/测试环境可运行。
  - 此迁移是可选优化，生产环境数据量大时需停机或使用 pg_repack/在线 DDL
    工具执行，避免长事务锁表。
  - 声明式分区要求分区键（created_at）纳入主键，故主键改为 (id, created_at)。
"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime, timedelta


# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    """判断当前是否 PostgreSQL（SQLite 不支持分区，降级处理）。"""
    bind = op.get_bind()
    return bind.dialect.name == "postgresql"


def _month_range(n: int):
    """生成当前月份起 n 个月的 (分区名, 起始日期, 结束日期) 三元组。

    起始日期为当月 1 号 00:00:00，结束日期为下月 1 号 00:00:00（左闭右开）。
    """
    today = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    parts = []
    for i in range(n):
        start = today + timedelta(days=32 * i)
        start = start.replace(day=1)
        # 下个月 1 号
        end = (start + timedelta(days=32)).replace(day=1)
        name = f"audit_log_{start.strftime('%Y_%m')}"
        parts.append((name, start, end))
    return parts


def upgrade():
    """audit_log 表分区：创建分区表 → 迁移数据 → 删旧表 → 重命名。"""
    bind = op.get_bind()

    # ---------- 1. 创建新分区表 audit_log_partitioned ----------
    if _is_postgres():
        # PostgreSQL：声明式分区表，PARTITION BY RANGE (created_at)
        op.create_table(
            "audit_log_partitioned",
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
            # 分区表主键必须包含分区键 created_at
            sa.PrimaryKeyConstraint("id", "created_at"),
            postgresql_partition_by="RANGE (created_at)",
        )
        # 索引（分区表索引会自动应用到所有子分区）
        op.create_index(
            "idx_audit_part_actor_action_created",
            "audit_log_partitioned",
            ["actor", "action", "created_at"],
        )
        op.create_index("idx_audit_part_action", "audit_log_partitioned", ["action"])
        op.create_index("idx_audit_part_request_id", "audit_log_partitioned", ["request_id"])
        op.create_index("idx_audit_part_hash", "audit_log_partitioned", ["hash"])

        # ---------- 2. 创建未来 6 个月分区（for 循环生成） ----------
        for name, start, end in _month_range(6):
            op.execute(
                f"CREATE TABLE {name} PARTITION OF audit_log_partitioned "
                f"FOR VALUES FROM ('{start.strftime('%Y-%m-%d')}') "
                f"TO ('{end.strftime('%Y-%m-%d')}')"
            )
        # 默认分区：捕获不在任何显式分区范围内的数据，避免插入失败
        op.execute("CREATE TABLE audit_log_default PARTITION OF audit_log_partitioned DEFAULT")
    else:
        # SQLite：不支持分区，降级为普通表（结构与原表一致）
        op.create_table(
            "audit_log_partitioned",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("actor", sa.String(length=64)),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("target_type", sa.String(length=32)),
            sa.Column("target_id", sa.String(length=64)),
            sa.Column("detail", sa.Text()),
            sa.Column("ip", sa.String(length=64)),
            sa.Column("request_id", sa.String(length=64)),
            sa.Column("created_at", sa.DateTime()),
            sa.Column("prev_hash", sa.String(length=64)),
            sa.Column("hash", sa.String(length=64)),
        )
        op.create_index(
            "idx_audit_part_actor_action_created",
            "audit_log_partitioned",
            ["actor", "action", "created_at"],
        )
        op.create_index("idx_audit_part_action", "audit_log_partitioned", ["action"])
        op.create_index("idx_audit_part_request_id", "audit_log_partitioned", ["request_id"])
        op.create_index("idx_audit_part_hash", "audit_log_partitioned", ["hash"])

    # ---------- 3. 迁移旧数据到新表 ----------
    op.execute(
        """
        INSERT INTO audit_log_partitioned
            (id, actor, action, target_type, target_id, detail, ip,
             request_id, created_at, prev_hash, hash)
        SELECT id, actor, action, target_type, target_id, detail, ip,
               request_id, created_at, prev_hash, hash
        FROM audit_log
        """
    )

    # ---------- 4. 删除旧表 + 重命名新表 ----------
    op.drop_index("idx_audit_actor_action_created", table_name="audit_log")
    op.drop_table("audit_log")
    op.rename_table("audit_log_partitioned", "audit_log")


def downgrade():
    """回滚分区策略：重建普通表 → 迁回数据 → 删分区表 → 重命名。"""
    if _is_postgres():
        # PostgreSQL：重建普通 audit_log 表
        op.create_table(
            "audit_log_plain",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("actor", sa.String(length=64), nullable=True),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("target_type", sa.String(length=32), nullable=True),
            sa.Column("target_id", sa.String(length=64), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("ip", sa.String(length=64), nullable=True),
            sa.Column("request_id", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("prev_hash", sa.String(length=64), nullable=True),
            sa.Column("hash", sa.String(length=64), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "idx_audit_plain_actor_action_created",
            "audit_log_plain",
            ["actor", "action", "created_at"],
        )

        # 迁回数据
        op.execute(
            """
            INSERT INTO audit_log_plain
                (id, actor, action, target_type, target_id, detail, ip,
                 request_id, created_at, prev_hash, hash)
            SELECT id, actor, action, target_type, target_id, detail, ip,
                   request_id, created_at, prev_hash, hash
            FROM audit_log
            """
        )

        # 删除分区表（含所有子分区与默认分区）
        op.execute("DROP TABLE IF EXISTS audit_log_default")
        for name, _start, _end in _month_range(6):
            op.execute(f"DROP TABLE IF EXISTS {name}")
        op.drop_index("idx_audit_actor_action_created", table_name="audit_log")
        op.drop_index("idx_audit_part_action", table_name="audit_log")
        op.drop_index("idx_audit_part_request_id", table_name="audit_log")
        op.drop_index("idx_audit_part_hash", table_name="audit_log")
        op.drop_table("audit_log")

        # 重命名回普通表
        op.rename_table("audit_log_plain", "audit_log")
        # 恢复原始索引名
        op.execute(
            "ALTER INDEX IF EXISTS idx_audit_plain_actor_action_created "
            "RENAME TO idx_audit_actor_action_created"
        )
    else:
        # SQLite：非分区表，仅需重建索引名
        op.drop_index("idx_audit_part_action", table_name="audit_log")
        op.drop_index("idx_audit_part_request_id", table_name="audit_log")
        op.drop_index("idx_audit_part_hash", table_name="audit_log")
        op.create_index("idx_audit_action", "audit_log", ["action"])
        op.create_index("idx_audit_request_id", "audit_log", ["request_id"])
        op.create_index("idx_audit_hash", "audit_log", ["hash"])
