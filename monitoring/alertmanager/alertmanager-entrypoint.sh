#!/bin/sh
# Alertmanager 启动前渲染配置：用环境变量替换 ${VAR} 占位符
# 必须设置的环境变量（在 docker-compose.monitoring.yml 中注入）：
#   ALERTMANAGER_DEFAULT_WEBHOOK  兜底告警通知 URL
#   ALERTMANAGER_CRITICAL_WEBHOOK critical 告警通知 URL（钉钉/企业微信/邮件网关）
#   ALERTMANAGER_WARNING_WEBHOOK  warning 告警通知 URL
# 未设置时使用配置文件中的默认值（指向本地 5001 端口的告警转发服务）

set -e

TEMPLATE=/etc/alertmanager/alertmanager.yml.template
CONFIG=/etc/alertmanager/alertmanager.yml

if [ ! -f "$TEMPLATE" ]; then
    echo "[alertmanager-entrypoint] 模板文件不存在: $TEMPLATE" >&2
    exit 1
fi

# 使用 envsubst 渲染（保留未设置变量的默认值 ${VAR:default} 语法需要 bash）
if command -v envsubst >/dev/null 2>&1; then
    envsubst < "$TEMPLATE" > "$CONFIG"
    echo "[alertmanager-entrypoint] 配置已渲染: $CONFIG"
else
    # 无 envsubst 时直接复制（使用默认值）
    cp "$TEMPLATE" "$CONFIG"
    echo "[alertmanager-entrypoint] envsubst 不可用，使用默认配置: $CONFIG"
fi

# 启动 Alertmanager
exec alertmanager --config.file="$CONFIG" "$@"
