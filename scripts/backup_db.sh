#!/bin/bash
# ===================================================================
# PostgreSQL 备份脚本
# 用法：
#   bash scripts/backup_db.sh                       # 默认备份
#   BACKUP_RETENTION_DAYS=14 bash scripts/backup_db.sh  # 保留 14 天
#
# 环境变量（全部可选，有默认值）：
#   POSTGRES_HOST       默认 db
#   POSTGRES_PORT       默认 5432
#   POSTGRES_USER       默认 ops
#   POSTGRES_DB         默认 ops_platform
#   PGPASSWORD          必须通过环境变量传入（不写入命令行参数）
#   BACKUP_DIR          默认 /backups（容器内）
#   BACKUP_RETENTION_DAYS 默认 7（保留最近 7 天）
# ===================================================================
set -euo pipefail

POSTGRES_HOST="${POSTGRES_HOST:-db}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-ops}"
POSTGRES_DB="${POSTGRES_DB:-ops_platform}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/${POSTGRES_DB}_${TS}.sql.gz"

mkdir -p "${BACKUP_DIR}"

echo "[$(date)] 开始备份数据库 ${POSTGRES_DB} ..."
echo "  host     : ${POSTGRES_HOST}:${POSTGRES_PORT}"
echo "  user     : ${POSTGRES_USER}"
echo "  target   : ${BACKUP_FILE}"
echo "  retention: ${BACKUP_RETENTION_DAYS} 天"

if [ -z "${PGPASSWORD:-}" ]; then
    echo "[ERROR] PGPASSWORD 环境变量未设置，无法认证" >&2
    exit 1
fi

# pg_dump + gzip 压缩，写入本地文件
pg_dump \
    -h "${POSTGRES_HOST}" \
    -p "${POSTGRES_PORT}" \
    -U "${POSTGRES_USER}" \
    -d "${POSTGRES_DB}" \
    --format=plain \
    --no-owner \
    --no-privileges \
    | gzip -9 > "${BACKUP_FILE}"

# 校验文件非空
if [ ! -s "${BACKUP_FILE}" ]; then
    echo "[ERROR] 备份文件为空，可能数据库连接失败" >&2
    rm -f "${BACKUP_FILE}"
    exit 1
fi

FILE_SIZE=$(du -h "${BACKUP_FILE}" | cut -f1)
echo "[$(date)] 备份完成: ${BACKUP_FILE} (${FILE_SIZE})"

# 清理超期备份
DELETED=$(find "${BACKUP_DIR}" -name "${POSTGRES_DB}_*.sql.gz" -mtime +${BACKUP_RETENTION_DAYS} -print -delete | wc -l)
if [ "${DELETED}" -gt 0 ]; then
    echo "[$(date)] 清理超期备份 ${DELETED} 个（> ${BACKUP_RETENTION_DAYS} 天）"
fi

# 校验备份完整性（gunzip -t 测试解压）
if ! gunzip -t "${BACKUP_FILE}" 2>/dev/null; then
    echo "[ERROR] 备份文件损坏（gunzip -t 失败）" >&2
    exit 1
fi
echo "[$(date)] 备份完整性校验通过"
