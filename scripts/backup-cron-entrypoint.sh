#!/bin/bash
# ===================================================================
# 备份 cron 容器 entrypoint
# - 动态生成 crontab（cron 表达式从环境变量读取）
# - 把环境变量传入 cron 子进程（cron 默认不继承环境变量）
# - 前台运行 cron，日志输出到 stdout
# ===================================================================
set -euo pipefail

BACKUP_CRON="${BACKUP_CRON:-0 2 * * *}"

# 把当前所有环境变量导出，cron 子进程需要 PGPASSWORD 等
printenv | grep -vE '^(HOME|PATH|HOSTNAME|TERM|SHLVL)=' > /etc/cron_env
chmod 600 /etc/cron_env

# 生成 cron 任务
cat > /etc/crontabs/root <<EOF
SHELL=/bin/bash
BASH_ENV=/etc/cron_env
${BACKUP_CRON} /usr/local/bin/backup_db.sh >> /var/log/backup.log 2>&1
EOF

# 启动 cron 前先做一次手动备份（验证脚本能跑通）
echo "[entrypoint] 首次启动，执行一次手动备份以验证脚本 ..."
/usr/local/bin/backup_db.sh || echo "[entrypoint] 首次备份失败（数据库可能尚未就绪），将等待 cron 调度"

echo "[entrypoint] cron 已启动，调度：${BACKUP_CRON}"
echo "[entrypoint] 备份目录：/backups，保留天数：${BACKUP_RETENTION_DAYS:-7} 天"

# tail 日志保持前台
touch /var/log/backup.log
tail -f /var/log/backup.log &
TAIL_PID=$!

# 启动 cron 前台
crond -f -l 2 &
CROND_PID=$!

# 优雅退出
trap "kill ${CROND_PID} ${TAIL_PID} 2>/dev/null; exit 0" TERM INT

wait ${CROND_PID}
