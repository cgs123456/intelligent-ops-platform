#!/bin/bash
# P0-6: 一键回滚脚本
# 用法: ./scripts/rollback.sh [tag]
#   无参数: 回滚到上一个版本（读取 .deploy/previous.json）
#   指定 tag: 回滚到指定镜像 tag
#
# 前提:
#   - 已部署过至少一次（.deploy/current.json 存在）
#   - Docker Compose 生产配置可用
#   - Nginx 容器名为 ops_nginx

set -euo pipefail

cd "$(dirname "$0")/.."
DEPLOY_DIR=".deploy"
COMPOSE_FILE="docker-compose.prod.yml"
HEALTH_URL="http://localhost:5000/health/ready"
MAX_RETRIES=6
RETRY_INTERVAL=5

echo "=========================================="
echo "  智能运营平台 - 一键回滚"
echo "=========================================="

# 读取回滚目标 tag
if [ -n "${1:-}" ]; then
    ROLLBACK_TAG="$1"
    echo "回滚到指定 tag: $ROLLBACK_TAG"
else
    if [ ! -f "$DEPLOY_DIR/previous.json" ]; then
        echo "ERROR: 未找到上次部署记录 ($DEPLOY_DIR/previous.json)，无法回滚"
        echo "请指定 tag: ./scripts/rollback.sh <tag>"
        exit 1
    fi
    ROLLBACK_TAG=$(python3 -c "import json; print(json.load(open('$DEPLOY_DIR/previous.json'))['tag'])")
    echo "回滚到上一版本 tag: $ROLLBACK_TAG"
fi

# 记录当前版本（回滚后可作为"前进"目标）
if [ -f "$DEPLOY_DIR/current.json" ]; then
    cp "$DEPLOY_DIR/current.json" "$DEPLOY_DIR/rolled_back_from.json"
    echo "已备份当前版本到 $DEPLOY_DIR/rolled_back_from.json"
fi

# 拉取目标镜像
REGISTRY="${REGISTRY:-ghcr.io}"
IMAGE_NAME="${IMAGE_NAME:-cgs123456/intelligent-ops-platform}"
FULL_IMAGE="$REGISTRY/$IMAGE_NAME:$ROLLBACK_TAG"

echo "拉取镜像: $FULL_IMAGE"
docker pull "$FULL_IMAGE" || {
    echo "ERROR: 镜像拉取失败: $FULL_IMAGE"
    exit 1
}

# 更新 docker-compose tag
export IMAGE_TAG="$ROLLBACK_TAG"
echo "回滚部署..."
docker-compose -f "$COMPOSE_FILE" up -d --no-deps

# 健康检查
echo "等待服务就绪..."
for i in $(seq 1 $MAX_RETRIES); do
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        echo "✓ 健康检查通过 (attempt $i/$MAX_RETRIES)"
        # 重载 nginx
        docker exec ops_nginx nginx -s reload 2>/dev/null || echo "WARN: nginx reload 跳过（容器可能未运行）"
        echo "=========================================="
        echo "  ✓ 回滚成功: $ROLLBACK_TAG"
        echo "=========================================="
        # 更新部署记录
        echo "{\"tag\":\"$ROLLBACK_TAG\",\"deployed_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"rolled_back\":true}" > "$DEPLOY_DIR/current.json"
        exit 0
    fi
    echo "  健康检查失败 (attempt $i/$MAX_RETRIES)，${RETRY_INTERVAL}s 后重试..."
    sleep $RETRY_INTERVAL
done

echo "=========================================="
echo "  ✗ 回滚失败：健康检查未通过"
echo "  请检查日志: docker-compose -f $COMPOSE_FILE logs"
echo "=========================================="
exit 1
