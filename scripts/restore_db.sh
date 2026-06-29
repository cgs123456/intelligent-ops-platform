#!/bin/bash
# ===================================================================
# PostgreSQL 恢复脚本
# 用法：
#   bash scripts/restore_db.sh /backups/ops_platform_20260629_120000.sql.gz
#
# 恢复演练：
#   1. 在测试环境启动一个空数据库
#   2. 执行：bash scripts/restore_db.sh <backup_file>
#   3. 连接到测试数据库验证表结构和数据
#   4. 演练通过即可正式投产
#
# 注意：恢复会覆盖现有数据（DROP + CREATE）
# ===================================================================
set -euo pipefail

POSTGRES_HOST="${POSTGRES_HOST:-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-ops}"
POSTGRES_DB="${POSTGRES_DB:-ops_platform}"

if [ -z "${PGPASSWORD:-}" ]; then
    echo "[ERROR] PGPASSWORD 环境变量未设置" >&2
    exit 1
fi

if [ $# -lt 1 ]; then
    echo "用法: $0 <backup_file.sql.gz>"
    echo "示例: $0 /backups/ops_platform_20260629_120000.sql.gz"
    exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "${BACKUP_FILE}" ]; then
    echo "[ERROR] 备份文件不存在: ${BACKUP_FILE}" >&2
    exit 1
fi

echo "===================================================="
echo "  PostgreSQL 数据库恢复"
echo "===================================================="
echo "  源备份   : ${BACKUP_FILE}"
echo "  目标库   : ${POSTGRES_USER}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
echo "  警告     : 恢复将覆盖现有数据"
echo "===================================================="

# 二次确认
read -r -p "确认恢复？输入 YES 继续: " CONFIRM
if [ "${CONFIRM}" != "YES" ]; then
    echo "已取消恢复"
    exit 0
fi

echo "[$(date)] 开始恢复 ..."

# 校验备份完整性
if ! gunzip -t "${BACKUP_FILE}" 2>/dev/null; then
    echo "[ERROR] 备份文件损坏" >&2
    exit 1
fi

# 断开所有现有连接，避免恢复时被阻塞
psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d postgres <<EOF
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
WHERE datname='${POSTGRES_DB}' AND pid <> pg_backend_pid();
EOF

# 恢复：gunzip 解压 + psql 执行
gunzip -c "${BACKUP_FILE}" \
    | psql \
        -h "${POSTGRES_HOST}" \
        -p "${POSTGRES_PORT}" \
        -U "${POSTGRES_USER}" \
        -d "${POSTGRES_DB}" \
        -v ON_ERROR_STOP=1

echo "[$(date)] 恢复完成"

# 验证：统计每个 schema 的表数量
echo "---- 恢复后表清单 ----"
psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -c \
    "SELECT schemaname, count(*) AS table_count FROM pg_tables WHERE schemaname='public' GROUP BY schemaname;"

echo "---- 恢复后行数抽检（关键表）----"
psql -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -c \
    "SELECT 'sys_user' AS tbl, count(*) FROM sys_user
     UNION ALL SELECT 'erp_product', count(*) FROM erp_product
     UNION ALL SELECT 'audit_log', count(*) FROM audit_log
     UNION ALL SELECT 'aigc_suggestion', count(*) FROM aigc_suggestion;"

echo "==== 恢复演练完成 ===="
