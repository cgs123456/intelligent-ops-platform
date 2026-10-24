#!/bin/sh
# Alertmanager 启动前渲染配置：用环境变量替换 ${VAR} 占位符
# 必须设置的环境变量（在 docker-compose.monitoring.yml 中注入）：
#   ALERTMANAGER_DEFAULT_WEBHOOK  兜底告警通知 URL
#   ALERTMANAGER_CRITICAL_WEBHOOK critical 告警通知 URL（钉钉/企业微信/邮件网关）
#   ALERTMANAGER_WARNING_WEBHOOK  warning 告警通知 URL
# 未设置时使用本脚本内置默认值（指向本地 5001 端口的告警转发服务）
#
# P0 修复：原实现用 envsubst，但 envsubst 不支持 ${VAR:default} 语法——
# 无论环境变量是否设置，${ALERTMANAGER_XXX_WEBHOOK:...} 都不会被替换，
# 渲染产物携带字面量占位符导致告警全部发往非法 URL。改用 POSIX sh 参数展开 + sed。

set -e

TEMPLATE=/etc/alertmanager/alertmanager.yml.template
CONFIG=/etc/alertmanager/alertmanager.yml

if [ ! -f "$TEMPLATE" ]; then
    echo "[alertmanager-entrypoint] 模板文件不存在: $TEMPLATE" >&2
    exit 1
fi

DEFAULT_URL="${ALERTMANAGER_DEFAULT_WEBHOOK:-http://127.0.0.1:5001/alertmanager/default}"
CRITICAL_URL="${ALERTMANAGER_CRITICAL_WEBHOOK:-http://127.0.0.1:5001/alertmanager/critical}"
WARNING_URL="${ALERTMANAGER_WARNING_WEBHOOK:-http://127.0.0.1:5001/alertmanager/warning}"

# sed 替换串中的 & 和 \ 是元字符，先转义
_escape_sed() {
    printf '%s' "$1" | sed 's/[&\]/\\&/g'
}

sed \
    -e "s|\${ALERTMANAGER_DEFAULT_WEBHOOK}|$(_escape_sed "$DEFAULT_URL")|g" \
    -e "s|\${ALERTMANAGER_CRITICAL_WEBHOOK}|$(_escape_sed "$CRITICAL_URL")|g" \
    -e "s|\${ALERTMANAGER_WARNING_WEBHOOK}|$(_escape_sed "$WARNING_URL")|g" \
    "$TEMPLATE" > "$CONFIG"

# 渲染结果自检：url 行不允许残留未替换的占位符
# （只匹配 ${ALERTMANAGER_ 形态；注释中的变量名说明不算残留）
if grep -q '\${ALERTMANAGER_' "$CONFIG"; then
    echo "[alertmanager-entrypoint] 渲染后仍存在占位符，配置异常:" >&2
    grep -n '\${ALERTMANAGER_' "$CONFIG" >&2 || true
    exit 1
fi
echo "[alertmanager-entrypoint] 配置已渲染: $CONFIG"

# 启动 Alertmanager
exec alertmanager --config.file="$CONFIG" "$@"
