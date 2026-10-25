# 部署文档

> 中型企业智能运营平台 · 开发与生产环境部署指南

## 1. 环境要求

### 1.1 运行时依赖

| 组件 | 版本要求 | 说明 |
|------|----------|------|
| Python | 3.12+ | 最低 3.10，CI 矩阵覆盖 3.10 / 3.11 / 3.12 |
| PostgreSQL | 15+（推荐 16） | 生产主数据库，alpine 镜像 |
| Redis | 7+ | 缓存 + 限流 + Celery broker，alpine 镜像 |
| Docker | 24+ | 容器化部署 |
| Docker Compose | v2+ | 多容器编排 |
| Nginx | 1.27+ | 反向代理 + TLS 终止 |

### 1.2 硬件资源建议

| 部署规模 | CPU | 内存 | 磁盘 | 适用场景 |
|----------|-----|------|------|----------|
| 开发 | 2 核 | 4GB | 20GB | 本地联调 |
| 小型生产 | 4 核 | 8GB | 50GB | 日活 < 1000 |
| 中型生产 | 8 核 | 16GB | 100GB+ | 日活 < 10000 |

> 磁盘需预留备份空间：单次备份约 50-200MB，保留 7 天约 1-1.5GB

### 1.3 网络端口

| 端口 | 服务 | 暴露范围 |
|------|------|----------|
| 80 | Nginx HTTP（301 跳转 HTTPS） | 公网 |
| 443 | Nginx HTTPS | 公网 |
| 5000 | Flask/Gunicorn | 仅容器内网 |
| 5432 | PostgreSQL | 仅容器内网 |
| 6379 | Redis | 仅容器内网 |

## 2. 环境变量完整清单

以下变量从 `config.py` 与 `.env.example` 提取，复制 `.env.example` 为 `.env` 后按实际填写：

### 2.1 必须设置（生产环境）

| 变量 | 默认值 | 说明 | 生成方式 |
|------|--------|------|----------|
| `SECRET_KEY` | `dev-secret-key-change-in-production` | JWT 签名密钥，生产缺失则启动报错 | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DB_PASSWORD` | - | PostgreSQL 密码，docker-compose.prod.yml 强制校验 | 强密码生成器 |
| `CORS_ORIGINS` | `*` | 允许的前端源（逗号分隔），生产不可为 `*` | `https://ops.example.com,https://admin.example.com` |
| `REDIS_PASSWORD` | `change_me_in_production` | Redis 认证密码 | 强密码生成器 |

### 2.2 Flask 环境

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FLASK_ENV` | `development` | `development` / `production` |
| `FLASK_DEBUG` | `0` | 调试模式开关 |

### 2.3 数据库

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `sqlite:///ops_platform.db` | 开发默认 SQLite；生产必须用 PostgreSQL，示例 `postgresql://ops:pass@db:5432/ops_platform` |

**连接池配置**（硬编码在 `config.py`，不可通过环境变量调整）：

| 参数 | 值 | 说明 |
|------|----|------|
| `pool_pre_ping` | `True` | 连接前检查，避免用断连 |
| `pool_recycle` | `1800` | 30 分钟回收（比默认 3600 更积极） |
| `pool_size` | `10` | 连接池大小 |
| `max_overflow` | `20` | 最大溢出 |
| `pool_timeout` | `30` | 获取连接超时 30 秒 |

### 2.4 Redis / 缓存

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CACHE_TYPE` | `simple` | `simple`（内存）/ `redis` |
| `CACHE_REDIS_URL` | - | Redis 连接（启用 redis 模式时必填），示例 `redis://:password@redis:6379/0` |
| `CACHE_DEFAULT_TIMEOUT` | `3600` | 缓存默认过期时间（秒） |
| `RATELIMIT_STORAGE_URL` | `memory://` | 限流存储（生产必须用 redis），示例 `redis://:password@redis:6379/1` |
| `RATELIMIT_DEFAULT` | `200/hour` | 全局默认限流 |
| `RATELIMIT_AUTH` | `10/minute` | 登录接口限流 |

### 2.5 认证 / RBAC

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RBAC_ENABLED` | `1` | `1`=开启权限校验（生产强制），`0`=dev demo 模式 |

### 2.6 CORS

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CORS_ORIGINS` | `*` | 生产必须显式指定，缺失则启动报错 |

### 2.7 LLM（AIGC 模块）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_PROVIDER` | `rule` | `rule`（规则引擎）/ `glm` / `qwen` / `openai` |
| `LLM_API_KEY` | - | LLM 密钥（留空走规则引擎） |
| `LLM_API_URL` | - | LLM 接口地址 |
| `LLM_MODEL` | `glm-4-flash` | 模型名称 |

### 2.8 RPA 调度

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RPA_SCHEDULE_ENABLED` | `0` | `1`=启用 RPA 定时任务 |
| `RPA_QUOTE_CRON` | `0 9 * * *` | 报价采集 cron（默认每日 09:00） |
| `RPA_SYNC_CRON` | `*/30 * * * *` | 订单同步 cron（默认每 30 分钟） |
| `RPA_MAX_RETRY` | `3` | 最大重试次数 |
| `RPA_RETRY_BACKOFF` | `2.0` | 指数退避基数（秒） |
| `RPA_BACKEND` | `mock` | `mock` / `selenium` |

### 2.9 FDE / ETL

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FDE_INCREMENTAL` | `1` | `1`=增量拉取 |
| `FDE_DQ_STRICT` | `1` | `1`=数据质量强校验 |

### 2.10 闭环自动化

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LOOP_TIMEOUT` | `60` | 单步超时秒数 |
| `LOOP_AUTO_TRIGGER` | `0` | `1`=低库存自动触发闭环 |

### 2.11 监控 / API 文档

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `METRICS_ENABLED` | `0` | `1`=启用 Prometheus 指标 `/metrics` |
| `API_DOCS_ENABLED` | `1` | `1`=启用 Swagger 文档 `/docs/swagger` |

### 2.12 Gunicorn

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GUNICORN_WORKERS` | `cpu*2+1` | worker 数量 |
| `GUNICORN_LOG_LEVEL` | `info` | `debug` / `info` / `warning` / `error` |

### 2.13 备份调度

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BACKUP_CRON` | `0 2 * * *` | 备份 cron（默认每天 02:00） |
| `BACKUP_RETENTION_DAYS` | `7` | 备份保留天数 |

### 2.14 告警通知

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ALERT_DINGTALK_WEBHOOK` | - | 钉钉机器人 webhook（留空不发送） |
| `ALERT_WECOM_WEBHOOK` | - | 企业微信机器人 webhook（留空不发送） |
| `ALERT_MAIL_TO` | - | 邮件告警收件人 |
| `ALERT_MAIL_SMTP_HOST` | - | SMTP 主机 |
| `ALERT_MAIL_SMTP_PORT` | `465` | SMTP 端口 |
| `ALERT_MAIL_SMTP_USER` | - | SMTP 用户名 |
| `ALERT_MAIL_SMTP_PASS` | - | SMTP 密码 |
| `ALERT_MAIL_FROM` | `ops-platform-alert@local` | 发件人地址 |

### 2.15 Celery（可选）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CELERY_BROKER_URL` | `memory://` | Celery broker（生产不可用 `memory://`），示例 `redis://:password@redis:6379/2` |
| `CELERY_RESULT_BACKEND` | - | Celery result backend，示例 `redis://:password@redis:6379/3` |

## 3. 开发环境部署

### 3.1 本地直接运行（最简）

```bash
# 安装依赖
pip install -r requirements.txt

# 启动（默认 SQLite + 内存缓存，无需 Redis/PostgreSQL）
python app.py
```

访问 `http://localhost:5000`，开发环境会自动加载种子数据。

### 3.2 Docker 开发环境（docker-compose.yml）

开发版包含 web + db + redis 三个容器：

```bash
# 启动
docker compose up -d

# 查看状态
docker compose ps

# 查看日志
docker compose logs -f web

# 进入 web 容器
docker compose exec web bash

# 停止
docker compose down
```

**开发版特点**：

- PostgreSQL 密码默认 `ops_dev_only`（可通过 `DB_PASSWORD` 环境变量覆盖）
- Redis 密码 `dev_only`，内存上限 256MB
- 不暴露 5432 / 6379 端口到宿主机（仅容器内网通信）
- 不含 Nginx 与备份容器

### 3.3 开发工具链

```bash
# 安装开发依赖（pytest/ruff/black/mypy/bandit/pre-commit）
pip install -r requirements-dev.txt

# 安装 pre-commit 钩子
pre-commit install

# 数据库迁移
flask db upgrade

# 运行测试
pytest                              # 或 make test
pytest --cov=. --cov-report=html    # 带覆盖率，生成 htmlcov/
```

## 4. 生产环境部署

### 4.1 准备环境变量

```bash
cp .env.example .env
vim .env
```

**必填项**：

```bash
SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
DB_PASSWORD=<强密码>
REDIS_PASSWORD=<强密码>
CORS_ORIGINS=https://ops.example.com,https://admin.example.com
```

**可选项**（按需配置）：

```bash
# LLM
LLM_PROVIDER=glm
LLM_API_KEY=xxx
LLM_API_URL=https://open.bigmodel.cn/api/paas/v4

# 告警
ALERT_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx

# Celery（生产强制配置，不可用 memory://）
CELERY_BROKER_URL=redis://:${REDIS_PASSWORD}@redis:6379/2
CELERY_RESULT_BACKEND=redis://:${REDIS_PASSWORD}@redis:6379/3
```

### 4.2 启动生产服务

```bash
# 启动全部服务（nginx + web + db + redis + db-backup）
docker compose -f docker-compose.prod.yml up -d

# 查看健康状态
docker compose -f docker-compose.prod.yml ps

# 查看实时日志
docker compose -f docker-compose.prod.yml logs -f

# 查看单个服务日志
docker compose -f docker-compose.prod.yml logs -f web
docker compose -f docker-compose.prod.yml logs -f nginx
docker compose -f docker-compose.prod.yml logs -f db
```

### 4.3 生产配置强校验

`ProdConfig` 启动时会校验以下条件，缺失任一项即启动失败：

| 校验项 | 失败原因 |
|--------|----------|
| `SECRET_KEY` 未设置 | 生产环境必须设置 SECRET_KEY |
| `DATABASE_URL` 缺失或为 SQLite | 生产必须用 PostgreSQL/MySQL |
| `CELERY_BROKER_URL` 为 `memory://` | 生产必须配置 CELERY_BROKER_URL |
| `RATELIMIT_STORAGE_URL` 为 `memory://` | 生产必须配置限流存储 |
| `CORS_ORIGINS` 为 `*` | 生产必须指定 CORS_ORIGINS |

### 4.4 资源限制

`docker-compose.prod.yml` 为每个容器设置了资源上限：

| 容器 | 内存上限 | CPU 上限 |
|------|----------|----------|
| nginx | 256M | 0.5 |
| web | 512M | 1.0 |
| db | 1G | 1.5 |
| redis | 512M | - |
| db-backup | 256M | 0.3 |

## 5. Nginx 反向代理配置

### 5.1 配置文件位置

- 配置文件：`nginx/conf.d/app.conf`（挂载到容器 `/etc/nginx/conf.d/app.conf`）
- 证书目录：`nginx/certs/`（挂载到容器 `/etc/nginx/certs/`）
- 静态资源：`static/`（挂载到容器 `/app/static/`）
- ACME 验证：`/var/www/certbot`

### 5.2 关键配置说明

```nginx
# 上游 Flask + Gunicorn
upstream ops_backend {
    server web:5000;
    keepalive 32;
}

# 限流区：登录 10r/m，全局 200r/m
limit_req_zone $binary_remote_addr zone=auth_zone:10m rate=10r/m;
limit_req_zone $binary_remote_addr zone=api_zone:10m rate=200r/m;
```

**路由规则**：

| location | 说明 |
|----------|------|
| `/.well-known/acme-challenge/` | Let's Encrypt ACME 验证 |
| `/static/` | 静态资源（nginx 直接服务，7 天缓存） |
| `/metrics` | Prometheus 指标（仅内网放行） |
| `/health*` | 健康检查（直通，不记 access_log） |
| `/api/v1/auth/(login\|refresh)` | 认证接口（auth_zone 限流） |
| `/api/v1/(loop/stream\|aigc/query-stream)` | SSE 流式（关闭 buffering，300s 超时） |
| `/api/` | API 主路由（api_zone 限流，120s 超时） |
| `/` | 首页 |

### 5.3 安全头

```nginx
add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
add_header X-Frame-Options           "SAMEORIGIN"                 always;
add_header X-Content-Type-Options    "nosniff"                    always;
add_header X-XSS-Protection          "1; mode=block"              always;
add_header Referrer-Policy            "strict-origin-when-cross-origin" always;
add_header Content-Security-Policy    "default-src 'self'; ..."    always;
```

### 5.4 重载配置

```bash
# 修改 nginx 配置后重载（不中断连接）
docker compose -f docker-compose.prod.yml exec nginx nginx -s reload

# 或通过容器名
docker exec ops_nginx nginx -s reload
```

## 6. SSL 证书申请

### 6.1 本地预览（自签证书）

```bash
cd nginx
bash gen-self-signed-cert.sh
```

生成 `nginx/certs/fullchain.pem` 与 `privkey.pem`，仅用于本地预览。

### 6.2 生产环境（Let's Encrypt）

**方式一：Certbot 容器**

```bash
# 1. 申请证书（首次）
docker run -it --rm \
  -v $(pwd)/nginx/certs:/etc/letsencrypt \
  -v $(pwd)/nginx/certbot:/var/www/certbot \
  certbot/certbot certonly --webroot \
  -w /var/www/certbot \
  -d ops.example.com \
  -d admin.example.com \
  --email admin@example.com \
  --agree-tos --no-eff-email

# 2. 软链接或复制到 nginx/certs/
cp $(pwd)/nginx/certs/live/ops.example.com/fullchain.pem $(pwd)/nginx/certs/
cp $(pwd)/nginx/certs/live/ops.example.com/privkey.pem $(pwd)/nginx/certs/

# 3. 重载 nginx
docker compose -f docker-compose.prod.yml exec nginx nginx -s reload
```

**方式二：自动续期**

```bash
# 添加 cron 每月 1 号检查续期
echo "0 3 1 * * docker run --rm -v $(pwd)/nginx/certs:/etc/letsencrypt -v $(pwd)/nginx/certbot:/var/www/certbot certbot/certbot renew --quiet && docker exec ops_nginx nginx -s reload" | crontab -
```

证书有效期 90 天，建议提前 30 天续期。

## 7. 数据库迁移

### 7.1 应用迁移

```bash
# 应用最新迁移到数据库
flask db upgrade

# 在 Docker 容器中执行
docker compose -f docker-compose.prod.yml exec web flask db upgrade
```

### 7.2 创建新迁移

修改 `models/` 下的数据模型后：

```bash
# 自动生成迁移脚本
flask db migrate -m "描述变更内容"

# 检查生成的迁移文件
ls migrations/versions/

# 应用迁移
flask db upgrade
```

### 7.3 回滚迁移

```bash
# 回滚一个版本
flask db downgrade

# 回滚到指定版本
flask db downgrade <revision_id>

# 查看当前版本
flask db current

# 查看历史
flask db history
```

### 7.4 迁移注意事项

- 迁移文件必须纳入版本控制，禁止手工修改数据库结构
- 生产环境迁移前先在测试环境验证
- 大表 DDL（如加索引）建议在低峰期执行，避免锁表
- 迁移失败时检查 `alembic_version` 表，必要时手工回退版本号

## 8. 备份恢复

### 8.1 自动备份

`db-backup` 容器内置 cron，默认每天 02:00 自动执行 `pg_dump`：

```bash
# 查看备份容器状态
docker compose -f docker-compose.prod.yml logs db-backup

# 查看已生成的备份
docker compose -f docker-compose.prod.yml exec db-backup ls -lh /backups
```

**备份脚本特性**（`scripts/backup_db.sh`）：

- `pg_dump --format=plain --no-owner --no-privileges` + `gzip -9` 压缩
- `gunzip -t` 完整性校验
- 自动清理超期备份（默认保留 7 天）
- 备份文件命名：`ops_platform_YYYYMMDD_HHMMSS.sql.gz`

### 8.2 手动备份

```bash
# 容器内手动触发
docker compose -f docker-compose.prod.yml exec db-backup /backup_db.sh

# 自定义保留天数
docker compose -f docker-compose.prod.yml exec \
  -e BACKUP_RETENTION_DAYS=14 \
  db-backup /backup_db.sh
```

### 8.3 恢复

恢复脚本 `scripts/restore_db.sh` 包含二次确认 + 完整性校验 + 行数抽检：

```bash
# 交互式恢复（会提示输入 YES 确认）
docker compose -f docker-compose.prod.yml exec db-backup \
  /restore_db.sh /backups/ops_platform_20260629_020000.sql.gz
```

**恢复流程**：

1. 校验备份文件完整性（`gunzip -t`）
2. 断开所有现有数据库连接（`pg_terminate_backend`）
3. `gunzip -c` 解压 + `psql` 执行（`ON_ERROR_STOP=1`）
4. 统计表数量与关键表行数抽检（sys_user / erp_product / audit_log / aigc_suggestion）

### 8.4 恢复演练

建议每月执行一次恢复演练：

```bash
# 1. 在测试环境启动空数据库
docker compose -f docker-compose.prod.yml up -d db

# 2. 从生产备份恢复
docker compose -f docker-compose.prod.yml exec db-backup \
  /restore_db.sh /backups/ops_platform_YYYYMMDD.sql.gz

# 3. 验证数据
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c "SELECT count(*) FROM sys_user;"

# 4. 演练完成后清理测试环境
docker compose -f docker-compose.prod.yml down -v
```

## 9. 监控接入

### 9.1 启用 Prometheus 指标

```bash
# .env 中设置
METRICS_ENABLED=1
```

重启 web 容器后，访问 `http://<host>/metrics` 获取 Prometheus 格式指标。

> Nginx 配置中 `/metrics` 仅允许内网访问（10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、127.0.0.0/8）

### 9.2 Prometheus 抓取配置

```yaml
# prometheus.yml
scrape_configs:
  - job_name: 'ops-platform'
    scrape_interval: 15s
    metrics_path: /metrics
    static_configs:
      - targets: ['<生产服务器IP>:443']
        labels:
          env: production
    scheme: https
    # 如需内网直连，可绕过 nginx 直连 5000 端口
    # targets: ['web:5000']
    # scheme: http
```

### 9.3 Grafana 仪表板

推荐关注的指标（基于 `prometheus_flask_exporter`）：

| 指标 | 说明 | 告警阈值 |
|------|------|----------|
| `flask_http_request_total` | HTTP 请求总数 | - |
| `flask_http_request_duration_seconds` | 请求耗时 | P99 > 2s |
| `flask_http_request_status` | 按状态码统计 | 5xx 比例 > 1% |
| `flask_http_request_exceptions` | 异常请求数 | > 0 |

### 9.4 健康检查端点

| 端点 | 用途 | 检查内容 |
|------|------|----------|
| `/health` | 深度健康检查 | 数据库 + Redis + 磁盘空间 |
| `/health/live` | 存活探针 | 轻量，不检查依赖 |
| `/health/ready` | 就绪探针 | 数据库 + Redis 连通性 |

## 10. 日志聚合

### 10.1 容器日志配置

`docker-compose.prod.yml` 已为每个容器配置日志大小限制：

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"   # web/db 单文件 50MB
    max-file: "5"     # 保留 5 个文件
```

### 10.2 Loki + Promtail 配置

**Promtail 配置**（采集 Docker 容器日志）：

```yaml
# promtail-config.yml
server:
  http_listen_port: 9080

positions:
  filename: /tmp/positions.yaml

clients:
  - url: http://loki:3100/loki/api/v1/push

scrape_configs:
  - job_name: docker
    docker_sd_configs:
      - host: unix:///var/run/docker.sock
        filters:
          - name: name
            values: ['ops-*']
    relabel_configs:
      - source_labels: ['__meta_docker_container_name']
        target_label: container
      - source_labels: ['__meta_docker_container_log_stream']
        target_label: stream
    pipeline_stages:
      # 解析 request_id
      - regex:
          expression: 'request_id=(?P<request_id>[a-f0-9]+)'
      - labels:
          request_id:
```

**Loki 启动**：

```yaml
# docker-compose.monitoring.yml
version: '3.8'
services:
  loki:
    image: grafana/loki:2.9.0
    ports:
      - "3100:3100"
    volumes:
      - loki_data:/loki

  promtail:
    image: grafana/promtail:2.9.0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - ./promtail-config.yml:/etc/promtail/config.yml:ro
    command: -config.file=/etc/promtail/config.yml

  grafana:
    image: grafana/grafana:10.0.0
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=<强密码>
    volumes:
      - grafana_data:/var/lib/grafana

volumes:
  loki_data:
  grafana_data:
```

### 10.3 应用日志格式

应用日志通过 `middleware.py` 注入 `request_id`，所有日志自动带 `request_id` 字段，便于全链路追踪：

```
[2026-07-29 10:00:00] INFO request_id=a1b2c3d4 [services.closed_loop] 闭环 step1 完成
```

## 11. 蓝绿部署流程

### 11.1 前置条件

- 镜像已推送到镜像仓库（`ghcr.io/cgs123456/intelligent-ops-platform:<tag>`）
- `.deploy/` 目录用于存储部署记录
- `scripts/rollback.sh` 可用

### 11.2 CI/CD Pipeline

`.github/workflows/ci.yml` 定义 4 个 job：

| Job | 说明 | 触发条件 |
|-----|------|----------|
| lint | ruff check + black --check + mypy | push/PR 到 main |
| test | Python 3.10/3.11/3.12 矩阵运行 pytest + coverage | lint 通过 |
| security | bandit 安全扫描 | lint 通过 |
| build | Docker 镜像构建并推送 | lint + test + security 全部通过 |

### 11.3 蓝绿部署步骤

```bash
# 1. 拉取新镜像
docker pull ghcr.io/cgs123456/intelligent-ops-platform:<new_tag>

# 2. 备份当前部署记录
mkdir -p .deploy
cp .deploy/current.json .deploy/previous.json 2>/dev/null || true

# 3. 更新镜像 tag 并启动
export IMAGE_TAG=<new_tag>
docker compose -f docker-compose.prod.yml up -d --no-deps web

# 4. 健康检查
for i in $(seq 1 6); do
  if curl -sf http://localhost:5000/health/ready > /dev/null; then
    echo "✓ 新版本就绪"
    break
  fi
  echo "等待就绪... ($i/6)"
  sleep 5
done

# 5. 记录部署信息
echo "{\"tag\":\"$IMAGE_TAG\",\"deployed_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" > .deploy/current.json

# 6. 重载 nginx
docker exec ops_nginx nginx -s reload
```

### 11.4 回滚

```bash
# 回滚到上一版本（自动读取 .deploy/previous.json）
./scripts/rollback.sh

# 回滚到指定 tag
./scripts/rollback.sh <tag>
```

**回滚脚本流程**：

1. 读取回滚目标 tag（参数或 `.deploy/previous.json`）
2. 备份当前版本到 `.deploy/rolled_back_from.json`
3. 拉取目标镜像
4. `docker-compose up -d --no-deps` 启动旧版本
5. 健康检查（最多 6 次重试，每次间隔 5 秒）
6. 重载 nginx
7. 更新 `.deploy/current.json`

## 12. 性能调优

### 12.1 Gunicorn worker 配置

`gunicorn.conf.py` 关键参数：

| 参数 | 默认值 | 调优建议 |
|------|--------|----------|
| `workers` | `cpu*2+1` | CPU 密集型任务减少到 `cpu+1`，IO 密集型可增加 |
| `worker_class` | `sync` | 同步模式，适合当前架构 |
| `timeout` | `120` | 长耗时接口（如 LLM 调用）需适当调大 |
| `graceful_timeout` | `30` | 优雅关闭等待时间 |
| `max_requests` | `1000` | 单 worker 处理 1000 请求后重启，防内存泄漏 |
| `max_requests_jitter` | `50` | 随机抖动，避免所有 worker 同时重启 |
| `keepalive` | `5` | 长连接保活 |
| `preload_app` | `True` | 预加载应用，减少 worker 内存占用 |
| `worker_tmp_dir` | `/dev/shm` | 使用内存文件系统，避免磁盘 IO |

**调整 worker 数量**：

```bash
# .env
GUNICORN_WORKERS=8  # 根据 CPU 核数与负载调整
```

### 12.2 数据库连接池

连接池参数硬编码在 `config.py`，如需调整需修改源码：

```python
SQLALCHEMY_ENGINE_OPTIONS = {
    "pool_pre_ping": True,     # 连接前检查，避免用断连
    "pool_recycle": 1800,      # 30 分钟回收
    "pool_size": 10,           # 连接池大小
    "max_overflow": 20,        # 最大溢出（总连接 = 10 + 20 = 30）
    "pool_timeout": 30,        # 获取连接超时
}
```

**调优建议**：

- 单 worker 最大连接数 = `pool_size + max_overflow = 30`
- 总连接数 = `workers * 30`，需确保 PostgreSQL `max_connections` 足够
- PostgreSQL 默认 `max_connections=100`，5 worker 时已接近上限，建议调到 200+
- IO 密集型场景可增大 `pool_size`，CPU 密集型保持默认

**PostgreSQL 调优**（`postgresql.conf`）：

```conf
max_connections = 200
shared_buffers = 256MB          # 内存 * 25%
effective_cache_size = 1GB      # 内存 * 75%
work_mem = 4MB
maintenance_work_mem = 64MB
random_page_cost = 1.1          # SSD 优化
checkpoint_completion_target = 0.9
wal_buffers = 16MB
```

### 12.3 缓存策略

**Flask-Caching 配置**：

- 默认超时 3600 秒（1 小时）
- 生产使用 Redis 模式（`CACHE_TYPE=redis`）
- 关键缓存键：
  - ERP 库存清单（高频读）
  - FDE ADS 层聚合数据
  - 用户权限信息

**Redis 调优**（`docker-compose.prod.yml`）：

```yaml
redis:
  command: >
    redis-server
    --maxmemory 256mb
    --maxmemory-policy allkeys-lru
    --requirepass ${REDIS_PASSWORD}
```

- `maxmemory 256mb`：内存上限
- `allkeys-lru`：内存满时 LRU 淘汰
- 建议根据实际缓存命中率调整 `maxmemory`，监控 `redis-cli info stats` 的 `evicted_keys`

### 12.4 Nginx 性能优化

已内置的优化：

- `keepalive 32`：上游连接复用
- `gzip on`：压缩响应体
- 静态资源 7 天缓存 + `immutable`
- SSE 接口关闭 `proxy_buffering`

**额外调优**（如需）：

```nginx
# worker 进程数（通常 = CPU 核数）
worker_processes auto;

# 每个 worker 的最大连接数
worker_connections 10240;

# 启用 sendfile
sendfile on;
tcp_nopush on;
tcp_nodelay on;
```

## 13. Kubernetes 部署（P2-15）

本章节说明如何通过 Helm Chart 将智能运营平台部署到 Kubernetes 集群。Chart 位于 `deploy/helm/intelligent-ops-platform/`。

### 13.1 前置条件

- 已有可用 K8s 集群（1.21+）并配置好 `kubectl` 上下文
- 已安装 Helm 3.8+
- 集群内已部署 ingress-nginx（或兼容的 Ingress Controller）
- 已存在外部 PostgreSQL 与 Redis（本 Chart 采用 externalDatabase 模式，不在集群内部署数据库 / 缓存，便于复用现有运维体系）
- 镜像 `intelligent-ops-platform:latest` 已推送至集群可访问的镜像仓库

### 13.2 安装步骤

1. 配置 values（覆盖默认占位值）：

   ```bash
   # 生产环境建议用独立 values 文件覆盖
   cp deploy/helm/intelligent-ops-platform/values.yaml my-values.yaml
   # 编辑 my-values.yaml：镜像仓库、ingress 域名、外部 DB/Redis 地址等
   ```

2. 创建命名空间并安装：

   ```bash
   kubectl create namespace ops-platform

   helm install ops-platform deploy/helm/intelligent-ops-platform \
     -n ops-platform \
     -f my-values.yaml
   ```

3. 验证部署：

   ```bash
   # 查看 release
   helm list -n ops-platform

   # 查看 Pod（等待 Running / Ready）
   kubectl get pods -n ops-platform -l "app.kubernetes.io/instance=ops-platform"

   # 查看 HPA
   kubectl get hpa -n ops-platform

   # 查看 Ingress
   kubectl get ingress -n ops-platform
   ```

4. 升级 / 回滚：

   ```bash
   # 升级（修改 values 或镜像 tag 后）
   helm upgrade ops-platform deploy/helm/intelligent-ops-platform -n ops-platform -f my-values.yaml

   # 回滚到上一版本
   helm rollback ops-platform -n ops-platform

   # 查看历史
   helm history ops-platform -n ops-platform
   ```

### 13.3 关键 values 配置说明

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `image.repository` / `image.tag` | 镜像仓库地址与 tag | `intelligent-ops-platform` / `latest` |
| `replicaCount` | 副本数（HPA 启用时由 HPA 接管） | `2` |
| `service.port` | Service 端口 | `5000` |
| `ingress.enabled` / `ingress.className` | 是否启用 Ingress 及控制器类型 | `true` / `nginx` |
| `ingress.hosts` / `ingress.tls` | 域名与 TLS 配置 | 需按环境覆盖 |
| `resources.requests/limits` | 容器资源配额 | cpu 250m/500m, mem 256Mi/512Mi |
| `probes.liveness.path` | 存活探针路径 | `/health/live` |
| `probes.readiness.path` | 就绪探针路径 | `/health/ready` |
| `postgresql.externalDatabase.*` | 外部 PostgreSQL 连接信息（密码从 Secret 引用） | 占位值 |
| `redis.host` / `redis.port` | 外部 Redis 地址（密码从 Secret 引用） | 占位值 |
| `env.*` | 非敏感环境变量（写入 ConfigMap） | production 配置 |

> 注意：`DATABASE_URL` / `CACHE_REDIS_URL` / `RATELIMIT_STORAGE_URL` / `CELERY_BROKER_URL` 等连接串由 Chart 基于上述参数与 Secret 动态拼接，无需手工指定。

### 13.4 HPA 弹性伸缩策略

Chart 默认启用 HPA（`hpa.enabled=true`），基于 CPU 与内存利用率自动伸缩：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `hpa.minReplicas` | 2 | 最小副本数 |
| `hpa.maxReplicas` | 6 | 最大副本数 |
| `hpa.targetCPUUtilizationPercentage` | 70 | CPU 平均利用率超过 70% 触发扩容 |
| `hpa.targetMemoryUtilizationPercentage` | 80 | 内存平均利用率超过 80% 触发扩容 |

缩容行为（`hpa.behavior.scaleDown`）：

- `stabilizationWindowSeconds: 300`：指标回落后需稳定 5 分钟才缩容，避免流量抖动导致反复伸缩
- `policies`：同时定义 Percent（每 60s 最多缩 50%）与 Pods（每 60s 最多缩 1 个），`selectPolicy: Max` 取两者中更保守的上限，实现平滑缩容

> 启用 HPA 后，`Deployment.spec.replicas` 由 HPA 接管，`values.replicaCount` 不再生效。查看实时状态：`kubectl describe hpa -n ops-platform`。

### 13.5 生产 Secret 管理建议

Chart 内置 `templates/secret.yaml` 仅为本地测试提供占位 Secret，**生产环境严禁使用**。推荐方案：

1. **关闭内置 Secret**：设置 `secret.create=false`，Chart 不再渲染 Secret 模板。

2. **External Secrets Operator（推荐）**：从 Vault / AWS Secrets Manager / 阿里云 KMS 等外部密钥源同步：

   ```bash
   # 示例：创建 ExternalSecret 引用外部密钥
   kubectl apply -f - <<EOF
   apiVersion: external-secrets.io/v1beta1
   kind: ExternalSecret
   metadata:
     name: intelligent-ops-platform-secret
     namespace: ops-platform
   spec:
     secretStoreRef:
       name: vault-backend
       kind: ClusterSecretStore
     target:
       name: intelligent-ops-platform-secret
     data:
       - secretKey: SECRET_KEY
         remoteRef:
           key: ops-platform/secret-key
       - secretKey: DB_PASSWORD
         remoteRef:
           key: ops-platform/db-password
       - secretKey: REDIS_PASSWORD
         remoteRef:
           key: ops-platform/redis-password
       - secretKey: CORS_ORIGINS
         remoteRef:
           key: ops-platform/cors-origins
   EOF
   ```

3. **Sealed Secrets**：将加密后的 Secret 安全提交到 Git 仓库，集群内 controller 解密。

4. **手工创建**：CI/CD 流水线在部署阶段通过 `kubectl create secret` 注入：

   ```bash
   kubectl create secret generic intelligent-ops-platform-secret -n ops-platform \
     --from-literal=SECRET_KEY="$(openssl rand -hex 32)" \
     --from-literal=DB_PASSWORD="<强随机密码>" \
     --from-literal=REDIS_PASSWORD="<强随机密码>" \
     --from-literal=CORS_ORIGINS="https://ops.example.com"
   ```

无论采用哪种方案，均需保证 Secret 名称与 `secret.name`（默认 `intelligent-ops-platform-secret`）及各 key（`SECRET_KEY` / `DB_PASSWORD` / `REDIS_PASSWORD` / `CORS_ORIGINS`）一致。

### 13.6 本地模板渲染验证

安装前可使用 `helm template` 离线校验渲染结果：

```bash
# 渲染并查看所有资源
helm template ops-platform deploy/helm/intelligent-ops-platform -f my-values.yaml

# 仅校验语法（lint）
helm lint deploy/helm/intelligent-ops-platform -f my-values.yaml
```
