# 运维手册

> 中型企业智能运营平台 · 故障排查 / 日常运维 / 应急响应 / 安全检查

## 1. 故障排查 SOP

### 1.1 服务无法启动

**症状**：`docker compose up` 后容器退出或反复重启。

**排查步骤**：

```bash
# 1. 查看容器状态（关注 Restarting / Exited 状态）
docker compose -f docker-compose.prod.yml ps

# 2. 查看退出日志
docker compose -f docker-compose.prod.yml logs --tail=100 web

# 3. 检查端口占用（5000/80/443）
netstat -tlnp | grep -E ':(80|443|5000)\s'

# 4. 检查数据库连接
docker compose -f docker-compose.prod.yml exec db pg_isready -U ops -d ops_platform

# 5. 检查环境变量是否齐全
docker compose -f docker-compose.prod.yml exec web env | grep -E 'SECRET_KEY|DATABASE_URL|CORS_ORIGINS'
```

**常见原因与处理**：

| 原因 | 现象 | 解决方案 |
|------|------|----------|
| `SECRET_KEY` 未设置 | web 容器启动即退出，日志含 "生产环境必须设置 SECRET_KEY" | 在 `.env` 设置 `SECRET_KEY` |
| `DATABASE_URL` 为 SQLite | web 退出，日志含 "生产必须用 PostgreSQL" | 改为 `postgresql://...` |
| `CORS_ORIGINS` 为 `*` | web 退出，日志含 "必须指定 CORS_ORIGINS" | 显式指定域名 |
| `CELERY_BROKER_URL` 为 `memory://` | web 退出，日志含 "不可用 memory://" | 配置 `redis://...` |
| 端口冲突 | nginx 启动失败 | 停止占用进程或修改端口映射 |
| 数据库未就绪 | web 日志连接超时 | `depends_on: db: service_healthy` 已配置，检查 db 健康 |
| 迁移未执行 | 接口 500，日志含 "relation does not exist" | 执行 `flask db upgrade` |

### 1.2 数据库连接耗尽

**症状**：接口响应缓慢或 500，日志含 `QueuePool limit of size 10 overflow 20 reached` 或 `timeout waiting for connection`。

**排查步骤**：

```bash
# 1. 查看当前连接数
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT count(*) FROM pg_stat_activity WHERE datname='ops_platform';"

# 2. 查看 max_connections
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c "SHOW max_connections;"

# 3. 查看慢查询（执行时间 > 1s）
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT pid, now()-query_start AS duration, query
   FROM pg_stat_activity
   WHERE state='active' AND now()-query_start > interval '1 second'
   ORDER BY duration DESC;"

# 4. 终止长时间运行的查询
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT pg_terminate_backend(<pid>);"
```

**根因与处理**：

| 根因 | 处理方案 |
|------|----------|
| worker 数过多导致连接数超限 | 调整 `GUNICORN_WORKERS` 或增大 PostgreSQL `max_connections` |
| 慢查询占用连接 | 优化查询（加索引 / 拆分大查询） |
| 连接泄漏（未释放） | 检查代码是否显式 `db.session.close()` 或使用 `with` 上下文 |
| `pool_recycle` 过大 | 默认 1800 秒已较积极，无需调整 |

**临时扩容**：

```bash
# 临时增大 PostgreSQL 连接数（重启失效）
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c "ALTER SYSTEM SET max_connections = 200;"
docker compose -f docker-compose.prod.yml restart db
```

### 1.3 Redis 连接失败

**症状**：缓存失效、限流不生效、Celery 任务无法执行，日志含 `Connection refused` 或 `NOAUTH`。

**排查步骤**：

```bash
# 1. 检查 Redis 容器状态
docker compose -f docker-compose.prod.yml ps redis

# 2. 测试连通性
docker compose -f docker-compose.prod.yml exec redis redis-cli ping

# 3. 如配置了密码，测试认证
docker compose -f docker-compose.prod.yml exec redis \
  redis-cli -a "${REDIS_PASSWORD}" ping

# 4. 检查内存使用
docker compose -f docker-compose.prod.yml exec redis \
  redis-cli -a "${REDIS_PASSWORD}" info memory

# 5. 检查网络（web 容器能否访问 redis）
docker compose -f docker-compose.prod.yml exec web \
  python -c "import redis; r=redis.from_url('redis://:${REDIS_PASSWORD}@redis:6379/0'); print(r.ping())"
```

**常见原因**：

| 原因 | 现象 | 解决方案 |
|------|------|----------|
| Redis 容器未启动 | `Connection refused` | `docker compose up -d redis` |
| 密码不匹配 | `NOAUTH Authentication required` | 检查 `.env` 中 `REDIS_PASSWORD` 与连接 URL 是否一致 |
| 内存超限被 OOM | 容器退出 | 增大 `--maxmemory` 或检查是否有大 key |
| 网络隔离 | web 无法 ping 通 redis | 检查是否在同一 docker network |
| 连接 URL 缺密码 | `NOAUTH` | 确保格式为 `redis://:password@redis:6379/0` |

### 1.4 502 / 503 错误

**症状**：浏览器访问返回 502（Bad Gateway）或 503（Service Unavailable）。

**502 排查**（Nginx 无法连接后端）：

```bash
# 1. 检查 web 容器是否健康
docker compose -f docker-compose.prod.yml ps web

# 2. 检查 web 是否监听 5000
docker compose -f docker-compose.prod.yml exec web curl -sf http://localhost:5000/health/live

# 3. 检查 nginx upstream 配置
docker compose -f docker-compose.prod.yml exec nginx nginx -t

# 4. 查看 nginx 错误日志
docker compose -f docker-compose.prod.yml exec nginx \
  tail -50 /var/log/nginx/error.log
```

**503 排查**（后端就绪检查失败）：

```bash
# 1. 健康检查端点返回什么
curl -v https://<host>/health/ready

# 2. 检查数据库
docker compose -f docker-compose.prod.yml exec web \
  python -c "from extensions import db; db.session.execute(db.text('SELECT 1')); print('DB OK')"

# 3. 检查 Redis
docker compose -f docker-compose.prod.yml exec web \
  python -c "from extensions import cache; cache.set('_t','1',5); print('Redis OK')"
```

**处理方案**：

| 场景 | 处理 |
|------|------|
| web 容器未启动 | `docker compose up -d web` |
| web 健康检查失败 | 查看日志定位数据库/Redis 问题 |
| nginx 配置错误 | `nginx -t` 校验配置后 reload |
| 后端过载 | 扩容 worker 或排查慢请求 |

### 1.5 闭环步骤失败

**症状**：五步闭环某步骤执行失败，LoopState 状态异常。

**排查步骤**：

```bash
# 1. 查询当前闭环状态
curl -H "Authorization: Bearer <token>" \
  https://<host>/api/v1/loop/status

# 2. 查看 web 日志中的闭环相关记录
docker compose -f docker-compose.prod.yml logs web | grep -i "closed_loop\|loop_step\|rollback"

# 3. 查询数据库 LoopState 表
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT id, current_step, status, updated_at FROM loop_state ORDER BY id DESC LIMIT 5;"

# 4. 查询审计日志中的闭环操作
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT id, actor, action, target_type, detail, created_at
   FROM audit_log WHERE action LIKE '%loop%' OR action LIKE '%step%'
   ORDER BY id DESC LIMIT 20;"
```

**常见失败场景与回滚**：

| 失败步骤 | 失败原因 | 回滚操作 |
|----------|----------|----------|
| step1 AIGC 生成 | LLM 调用失败 | 自动降级规则引擎；手动删除 pending 建议 |
| step2 审核 | 无（人工操作） | 重新审核 |
| step3 RPA 下单 | 供应商接口超时 | 自动重试（最多 2 次）；调用 `/api/v1/loop/rollback` |
| step4 ERP 入库 | 库存数据异常 | 记审计待手工冲销；调用 rollback |
| step5 FDE 刷新 | ETL 失败 | 删除本次 ADS 数据；重新执行 ETL |

**手动回滚**：

```bash
# 回滚指定步骤（需 loop:rollback 权限）
curl -X POST -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"step_no": 3}' \
  https://<host>/api/v1/loop/rollback
```

**关键机制说明**：

- **文件锁**：跨进程 `fcntl` 锁防止并发执行，失败时锁可能残留，需检查 `/tmp/ops_loop.lock`
- **超时**：`LOOP_TIMEOUT`（默认 60s）控制单步超时，超时后自动失败
- **断点续跑**：LoopState 记录当前步骤，重启后可从断点继续

### 1.6 告警通知未发送

**症状**：异常事件触发但未收到钉钉/企业微信/邮件通知。

**排查步骤**：

```bash
# 1. 查看 notifier 日志
docker compose -f docker-compose.prod.yml logs web | grep -i "notifier\|alert\|notify"

# 2. 检查环境变量配置
docker compose -f docker-compose.prod.yml exec web env | grep ALERT_

# 3. 手动触发告警测试（如有测试接口）
# 或在数据库手工创建一条异常记录触发检测

# 4. 检查网络出站（容器能否访问钉钉/微信 API）
docker compose -f docker-compose.prod.yml exec web \
  curl -sv https://oapi.dingtalk.com/robot/send?access_token=test 2>&1 | head -20
```

**常见原因**：

| 原因 | 处理 |
|------|------|
| Webhook 未配置 | 在 `.env` 设置 `ALERT_DINGTALK_WEBHOOK` 等 |
| Webhook URL 失效 | 重新获取并更新配置 |
| 容器无法访问外网 | 检查防火墙 / DNS 解析 |
| SMTP 配置错误 | 检查 `ALERT_MAIL_SMTP_HOST` / 端口 / 密码 |
| 通知被静默 | Notifier 单渠道失败不影响其他渠道，查看日志确认哪些渠道失败 |

> Notifier 设计为异步发送，单渠道失败不影响主流程，仅记录日志。无任何配置时降级为仅记日志。

## 2. 日常运维任务

### 2.1 日志清理

**容器日志**（已配置自动轮转）：

```bash
# 查看各容器日志大小
docker compose -f docker-compose.prod.yml logs --no-log-prefix web | wc -l

# 手动清理日志（谨慎，会丢失历史日志）
truncate -s 0 /var/lib/docker/containers/*/*-json.log
```

**应用日志**（`app.log`）：

应用日志通过 Python `logging` 输出，建议配置 `RotatingFileHandler` 或 `TimedRotatingFileHandler`：

```bash
# 查看应用日志大小
docker compose -f docker-compose.prod.yml exec web ls -lh /app/*.log 2>/dev/null

# 如日志过大，调整 logging 配置增加轮转
```

**Nginx 日志**：

```bash
# 查看 nginx 日志卷
docker compose -f docker-compose.prod.yml exec nginx ls -lh /var/log/nginx/

# 日志轮转（nginx 容器内已通过 logrotate 或 docker logging driver 管理）
# 手动清空
docker compose -f docker-compose.prod.yml exec nginx truncate -s 0 /var/log/nginx/access.log
```

### 2.2 数据库维护

**VACUUM ANALYZE**（回收死元组 + 更新统计信息）：

```bash
# 定期执行（建议每周一次低峰期）
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c "VACUUM ANALYZE;"

# 仅分析（更快，更新查询规划器统计）
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c "ANALYZE;"
```

**索引重建**（碎片严重时）：

```bash
# 查看索引碎片情况
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT schemaname, tablename, indexname, idx_scan
   FROM pg_stat_user_indexes
   ORDER BY idx_scan DESC LIMIT 20;"

# 重建索引（不锁表，CONCURRENTLY）
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "REINDEX INDEX CONCURRENTLY idx_audit_log_created_at;"
```

**表膨胀检查**：

```bash
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT relname, n_live_tup, n_dead_tup,
          round(n_dead_tup::numeric / NULLIF(n_live_tup, 0) * 100, 2) AS dead_pct
   FROM pg_stat_user_tables
   WHERE n_dead_tup > 1000
   ORDER BY dead_pct DESC LIMIT 10;"
```

> `dead_pct` > 20% 时建议执行 `VACUUM`。

### 2.3 审计日志归档

审计日志（`audit_log` 表）会持续增长，建议按月归档：

```bash
# 1. 导出上月审计日志
docker compose -f docker-compose.prod.yml exec db \
  pg_dump -U ops -d ops_platform \
  --data-only --table=audit_log \
  --column-inserts \
  --where="created_at >= date_trunc('month', now() - interval '1 month')
           AND created_at < date_trunc('month', now())" \
  | gzip > /backups/audit_log_$(date -d 'last month' +%Y%m).sql.gz

# 2. 验证归档文件
gunzip -t /backups/audit_log_YYYYMM.sql.gz

# 3. 删除已归档数据（确认归档成功后执行）
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "DELETE FROM audit_log
   WHERE created_at < date_trunc('month', now() - interval '1 month');"

# 4. VACUUM 回收空间
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c "VACUUM FULL audit_log;"
```

> 注意：`VACUUM FULL` 会锁表，需在维护窗口执行。

### 2.4 Token 黑名单清理

过期 token 黑名单记录会占用空间，建议定时清理：

```bash
# 清理已过期的黑名单记录
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "DELETE FROM token_blacklist
   WHERE expires_at < now();"

# 或通过应用层清理（如配置了定时任务）
docker compose -f docker-compose.prod.yml exec web \
  python -c "
from extensions import db
from models.system import TokenBlacklist
from datetime import datetime
deleted = db.session.query(TokenBlacklist).filter(
    TokenBlacklist.expires_at < datetime.utcnow()
).delete(synchronize_session=False)
db.session.commit()
print(f'清理 {deleted} 条过期黑名单记录')
"
```

建议通过 Celery Beat 配置每日清理任务。

## 3. 监控指标说明

### 3.1 健康检查端点

| 端点 | 用途 | 检查内容 | 响应 |
|------|------|----------|------|
| `/health` | 深度健康检查 | 数据库 + Redis + 磁盘空间 | `{"status":"ok", "database":"ok", "redis":"ok", "disk_free_pct":85.2}` |
| `/health/live` | 存活探针 | 无（仅确认进程存活） | `{"status":"ok"}` |
| `/health/ready` | 就绪探针 | 数据库 + Redis 连通性 | `{"status":"ready"}` 或 `{"status":"not_ready","database":"fail"}` |

**使用场景**：

- **Kubernetes/Docker livenessProbe** → `/health/live`（轻量，不检查依赖，避免误重启）
- **Kubernetes/Docker readinessProbe** → `/health/ready`（依赖检查，决定是否接流量）
- **人工巡检 / 深度诊断** → `/health`（含磁盘空间等完整信息）

**状态码**：

- `200`：健康
- `503`：不健康或降级（`status` 为 `degraded` 或 `not_ready`）

### 3.2 Prometheus metrics

启用方式：`METRICS_ENABLED=1`，访问 `/metrics` 获取指标（基于 `prometheus_flask_exporter`）。

**核心指标**：

| 指标 | 类型 | 说明 |
|------|------|------|
| `flask_http_request_total` | Counter | HTTP 请求总数（按 method/path/status 标签） |
| `flask_http_request_duration_seconds` | Histogram | 请求处理耗时 |
| `flask_http_request_status` | Counter | 按状态码统计 |
| `flask_http_request_exceptions` | Counter | 异常请求数 |
| `flask_exporter_info` | Gauge | exporter 版本信息 |

**PromQL 查询示例**：

```promql
# QPS
rate(flask_http_request_total[1m])

# P99 延迟
histogram_quantile(0.99, rate(flask_http_request_duration_seconds_bucket[5m]))

# 5xx 错误率
rate(flask_http_request_total{status=~"5.."}[5m]) / rate(flask_http_request_total[5m])

# 异常请求
rate(flask_http_request_exceptions[5m])
```

### 3.3 告警阈值建议

| 指标 | 阈值 | 告警级别 | 说明 |
|------|------|----------|------|
| `/health/ready` 非 200 | 持续 1 分钟 | Critical | 后端不可用 |
| `/health` status=degraded | 持续 5 分钟 | Warning | 依赖降级 |
| HTTP 5xx 错误率 | > 1% 持续 5 分钟 | Critical | 服务异常 |
| P99 延迟 | > 2s 持续 5 分钟 | Warning | 性能下降 |
| PostgreSQL 连接数 | > 80% max_connections | Warning | 连接池耗尽风险 |
| Redis 内存 | > 90% maxmemory | Warning | 缓存驱逐 |
| 磁盘剩余 | < 10% | Critical | 容器写入失败 |
| 备份任务失败 | 最近一次执行失败 | Critical | 数据丢失风险 |
| 闭环步骤失败 | 单次失败 | Warning | 业务异常 |
| 审计 hash chain 校验失败 | tampered_id != null | Critical | 审计日志被篡改 |

## 4. 应急响应

### 4.1 数据库故障切换

**主库不可用时的应急流程**：

```bash
# 1. 确认数据库状态
docker compose -f docker-compose.prod.yml ps db
docker compose -f docker-compose.prod.yml logs --tail=50 db

# 2. 如数据库崩溃，尝试重启
docker compose -f docker-compose.prod.yml restart db

# 3. 等待健康检查通过
docker compose -f docker-compose.prod.yml exec db pg_isready -U ops -d ops_platform

# 4. 如无法启动，从备份恢复
# 4.1 启动一个新的空数据库
docker compose -f docker-compose.prod.yml up -d db

# 4.2 执行恢复
docker compose -f docker-compose.prod.yml exec db-backup \
  /restore_db.sh /backups/ops_platform_<最近备份>.sql.gz

# 5. 验证数据完整性
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT 'sys_user' AS tbl, count(*) FROM sys_user
   UNION ALL SELECT 'audit_log', count(*) FROM audit_log
   UNION ALL SELECT 'erp_product', count(*) FROM erp_product;"

# 6. 重启 web 服务
docker compose -f docker-compose.prod.yml restart web
```

**审计日志完整性校验**（故障恢复后必做）：

```bash
curl -H "Authorization: Bearer <admin_token>" \
  https://<host>/api/v1/audit/verify-chain
```

返回 `"ok": true` 表示审计链完整，`"tampered_id": <id>` 表示该记录被篡改。

### 4.2 回滚操作

**应用回滚**（部署失败时）：

```bash
# 回滚到上一版本
./scripts/rollback.sh

# 回滚到指定版本
./scripts/rollback.sh <tag>

# 回滚脚本会：
# 1. 读取 .deploy/previous.json 获取上一版本 tag
# 2. 拉取旧镜像
# 3. docker-compose up -d --no-deps 启动旧版本
# 4. 健康检查（最多 6 次重试）
# 5. 重载 nginx
```

**数据库回滚**（迁移失败时）：

```bash
# 查看迁移历史
docker compose -f docker-compose.prod.yml exec web flask db history

# 回滚一个版本
docker compose -f docker-compose.prod.yml exec web flask db downgrade

# 回滚到指定版本
docker compose -f docker-compose.prod.yml exec web flask db downgrade <revision>
```

**闭环回滚**（业务步骤失败时）：

```bash
# 回滚闭环指定步骤
curl -X POST -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"step_no": 3}' \
  https://<host>/api/v1/loop/rollback
```

### 4.3 密码重置

**admin 临时密码重置**（数据库直接操作）：

```bash
# 1. 生成临时密码 hash（在 web 容器中执行）
docker compose -f docker-compose.prod.yml exec web python -c "
from werkzeug.security import generate_password_hash
print(generate_password_hash('Temp@2026!'))
"

# 2. 更新 admin 密码
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "UPDATE sys_user SET password_hash='<step1生成的hash>', must_change_password=true
   WHERE username='admin';"

# 3. 通知用户临时密码，登录后强制改密
```

**Redis 密码重置**：

```bash
# 1. 修改 .env 中 REDIS_PASSWORD
vim .env

# 2. 同时更新所有连接 URL 中的密码
# CACHE_REDIS_URL / RATELIMIT_STORAGE_URL / CELERY_BROKER_URL / CELERY_RESULT_BACKEND

# 3. 重启全部服务
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d
```

## 5. 安全检查清单

### 5.1 审计 hash chain 校验

```bash
# 校验全量审计日志完整性
curl -H "Authorization: Bearer <admin_token>" \
  https://<host>/api/v1/audit/verify-chain

# 校验指定范围
curl -H "Authorization: Bearer <admin_token>" \
  "https://<host>/api/v1/audit/verify-chain?start_id=1&end_id=10000"
```

**预期响应**：

```json
{"ok": true, "tampered_id": null, "message": "审计日志完整"}
```

**异常响应**：

```json
{"ok": false, "tampered_id": 1234, "message": "记录 id=1234 被篡改或断链"}
```

**检查频率**：建议每周一次，或在故障恢复后立即执行。

### 5.2 RBAC 权限审查

**查询所有用户与角色**：

```bash
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT u.id, u.username, u.role_id, r.name AS role, u.active, u.must_change_password
   FROM sys_user u LEFT JOIN sys_role r ON u.role_id = r.id
   ORDER BY u.id;"
```

**检查要点**：

- [ ] admin 账户数量控制在最少（建议 1-2 个）
- [ ] 离职用户 `active=false`
- [ ] 测试账号已删除或禁用
- [ ] 所有用户已通过首次登录改密（`must_change_password=false`）
- [ ] RBAC_ENABLED=1（生产强制开启）

**权限点清单**：

| 权限点 | 角色 | 说明 |
|--------|------|------|
| `erp:read` | admin/operator/viewer | ERP 只读 |
| `erp:write` | admin/operator | ERP 写入 |
| `aigc:read` | admin/operator/viewer | AIGC 只读 |
| `aigc:review` | admin/operator | 审核建议 |
| `rpa:read` | admin/operator/viewer | RPA 只读 |
| `rpa:write` | admin/operator | RPA 写入 |
| `fde:read` | admin/operator/viewer | FDE 只读 |
| `fde:run` | admin/operator | 执行 ETL |
| `loop:read` | admin/operator/viewer | 闭环只读 |
| `loop:run` | admin/operator | 执行闭环 |
| `loop:rollback` | admin | 回滚闭环 |
| `audit:read` | admin | 审计日志 |

### 5.3 密码策略检查

**密码策略要求**（`services/password_policy.py`）：

- 最小长度 8 位
- 必须包含大小写字母 + 数字 + 特殊字符
- 不允许与用户名相同
- 首次登录强制改密

**检查命令**：

```bash
# 查询未改密的用户（应为空）
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT id, username, created_at FROM sys_user WHERE must_change_password=true;"

# 查询弱密码用户（需应用层检查，数据库仅存 hash）
# 建议通过审计日志检查最近密码修改记录
docker compose -f docker-compose.prod.yml exec db \
  psql -U ops -d ops_platform -c \
  "SELECT actor, created_at FROM audit_log
   WHERE action='change_password' ORDER BY id DESC LIMIT 10;"
```

### 5.4 依赖 CVE 扫描

**pip-audit 扫描**：

```bash
# 在 web 容器中执行
docker compose -f docker-compose.prod.yml exec web \
  pip-audit -r requirements.txt

# 或在本地执行
pip install pip-audit
pip-audit -r requirements.txt
```

**bandit 安全扫描**：

```bash
# CI 已集成，本地手动执行
docker compose -f docker-compose.prod.yml exec web \
  bandit -r . -x tests,.venv

# 或通过 Makefile
make security
```

**Docker 镜像扫描**（可选）：

```bash
# 使用 trivy 扫描镜像
trivy image ghcr.io/cgs123456/intelligent-ops-platform:<tag>
```

**扫描频率与处理**：

| 频率 | 工具 | 处理 |
|------|------|------|
| 每次提交 | bandit（CI） | 高危阻断合并 |
| 每周 | pip-audit | 高危/Critical 7 天内修复 |
| 每月 | trivy 镜像扫描 | 高危镜像重新构建 |
| 紧急 | CVE 通报 | 24 小时内评估并修复 |

**修复流程**：

1. 升级依赖版本（`requirements.txt`）
2. 运行测试确保兼容性：`pytest`
3. 重新构建镜像并部署
4. 复扫确认漏洞已消除

### 5.5 其他安全检查

- [ ] `SECRET_KEY` 已设置且未泄露（不在日志 / 代码 / 公开仓库中）
- [ ] `DB_PASSWORD` / `REDIS_PASSWORD` 为强密码
- [ ] `CORS_ORIGINS` 显式指定域名（非 `*`）
- [ ] `RBAC_ENABLED=1`
- [ ] TLS 证书有效且未过期
- [ ] Nginx 安全头已生效（`curl -I https://<host>/` 检查 HSTS / X-Frame-Options 等）
- [ ] `/metrics` 端点仅内网可访问
- [ ] 容器以非 root 用户运行
- [ ] 备份文件权限受限（仅运维可读）
- [ ] 审计日志定期归档且归档文件已加密或权限隔离

## 日志聚合与告警（P1-13）

本节说明基于 Loki + Promtail + Alertmanager 的日志聚合与告警体系如何启动、查询与处置。

### 组件与职责

| 组件 | 端口 | 职责 |
|------|------|------|
| Loki | 3100 | 日志聚合存储，提供 LogQL 查询接口 |
| Promtail | 9080（内部） | 采集 docker 容器 stdout/stderr，解析 JSON 后推送至 Loki |
| Alertmanager | 9093 | 接收 Prometheus 告警，按 severity 分组路由，通过 webhook 通知 |

> Prometheus 与 Grafana 已由 P0 监控栈提供，本节不重复描述。Grafana 中添加 Loki 数据源 `http://loki:3100` 即可查询日志。

### 启动与停止

**联合启动**（与生产 compose 一起，推荐生产用法）：

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.monitoring.yml up -d
```

**单独启动监控栈**（仅用于测试）：

```bash
docker compose -f docker-compose.monitoring.yml up -d
```

**查看监控栈状态**：

```bash
docker compose -f docker-compose.monitoring.yml ps
# 预期看到 ops-loki / ops-promtail / ops-alertmanager 三容器 Up (healthy)
```

**验证各组件就绪**：

```bash
# Loki 就绪
curl -sf http://localhost:3100/ready && echo OK
# Alertmanager 健康
curl -sf http://localhost:9093/-/healthy && echo OK
# Promtail 指标
curl -sf http://localhost:9080/metrics | grep -E 'logentry.*total' | head
```

**停止监控栈**（不影响业务服务）：

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.monitoring.yml stop loki promtail alertmanager
```

### 日志查询方式

#### 1. Grafana UI 查询（推荐）

Grafana → Explore → 数据源选择 Loki → 输入 LogQL：

```logql
# 查询 web 容器最近 1 小时所有日志
{container_name="intelligent-ops-platform-web-1"} |= ``

# 查询 ERROR 级别日志（promtail 已将 level 提取为标签）
{container_name=~"intelligent-ops-platform-.*", level="ERROR"}

# 按 request_id 追踪单次请求全链路日志
{request_id="abc-123-def"}

# 关键字过滤：包含 timeout 的日志
{container_name="intelligent-ops-platform-web-1"} |= "timeout"

# 排除健康检查日志（降噪）
{container_name="intelligent-ops-platform-web-1"} != "/health/live"

# 统计最近 5 分钟每分钟 ERROR 日志条数（用于趋势图）
sum by (container_name) (
  count_over_time({level="ERROR"}[5m])
)
```

#### 2. Loki HTTP API 查询

```bash
# 即时查询
curl -G -s "http://localhost:3100/loki/api/v1/query" \
  --data-urlencode 'query={container_name="intelligent-ops-platform-web-1"} |= "ERROR"' | jq

# 范围查询（最近 1 小时）
curl -G -s "http://localhost:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={level="ERROR"}' \
  --data-urlencode "start=$(date -d '1 hour ago' +%s)000000000" \
  --data-urlencode "end=$(date +%s)000000000" | jq
```

#### 3. 通过 label 维度过滤

promtail 提取的标签维度：

| 标签 | 来源 | 示例 |
|------|------|------|
| `container_name` | docker 容器名 | `intelligent-ops-platform-web-1` |
| `service` | compose service 名 | `web` / `nginx` / `db` |
| `level` | JSON 日志 level 字段 | `INFO` / `WARNING` / `ERROR` |
| `request_id` | JSON 日志 request_id 字段 | `uuid-xxx` |
| `stream` | stdout/stderr | `stdout` / `stderr` |

### 告警分级与处理流程

#### 告警级别定义

| 级别 | 含义 | 响应时效 | 通知方式 |
|------|------|----------|----------|
| `critical` | 服务不可用 / 数据丢失风险 | 立即响应（< 15 分钟） | 立即发送 webhook，1h 内不重复 |
| `warning` | 性能下降 / 容量预警 | 工作时间内响应（< 4 小时） | 5 分钟聚合后发送，6h 内不重复 |

#### 告警规则清单（monitoring/prometheus/alerts.yml）

| 告警名 | 触发条件 | 持续时间 | 级别 |
|--------|----------|----------|------|
| `High5xxErrorRate` | 5xx 错误率 > 1% | 2m | critical |
| `HighP95Latency` | P95 响应时间 > 500ms | 5m | warning |
| `DiskSpaceLow` | 磁盘剩余 < 10% | 1m | critical |
| `DBConnectionPoolExhaustion` | 连接池使用率 > 80% | 3m | warning |
| `ServiceDown` | `up == 0` | 1m | critical |
| `RedisDown` | `up{job="redis"} == 0` | 0m（立即） | critical |

#### 抑制规则

- 同服务同告警名有 `critical` 时，自动抑制对应 `warning`，避免重复告警
- `ServiceDown` 触发时，抑制该服务的所有 `warning` 告警（服务都挂了，其他告警无意义）

#### 处理流程

```
告警触发（Prometheus）
  → 推送至 Alertmanager
  → 按 severity 分组（critical 立即 / warning 5min 聚合）
  → 调用 webhook 通知
  → 值班人员响应
  → 按 runbook 排查
  → 恢复后 Alertmanager 自动发送 resolved 通知
```

**critical 告警处理 SOP**：

1. 收到 critical 告警通知后，15 分钟内开始响应
2. 通过 Alertmanager UI（`http://<host>:9093`）查看告警详情与 label
3. 根据告警 `runbook` annotation 跳转对应 runbook 章节
4. 在 Loki 中按 `container_name` / `request_id` 查询错误日志
5. 处置后确认告警状态变为 resolved

**告警静默**（处理已知问题期间避免打扰）：

```bash
# 通过 Alertmanager API 创建静默规则（静默 1 小时）
curl -X POST http://localhost:9093/api/v2/silences \
  -H "Content-Type: application/json" \
  -d '{
    "matchers": [{"name": "alertname", "value": "High5xxErrorRate", "isRegex": false}],
    "startsAt": "'$(date -Iseconds)'",
    "endsAt": "'$(date -d "+1 hour" -Iseconds)'",
    "createdBy": "ops",
    "comment": "已知问题排查中，临时静默"
  }'
```

### 通知渠道配置

当前 `monitoring/alertmanager/alertmanager.yml` 使用 webhook 占位地址（`http://127.0.0.1:5001/...`），生产环境需替换为真实通知渠道：

**钉钉机器人**：

```yaml
receivers:
  - name: 'critical-webhook'
    webhook_configs:
      - url: 'https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN'
        send_resolved: true
```

**企业微信机器人**：

```yaml
receivers:
  - name: 'critical-webhook'
    webhook_configs:
      - url: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY'
        send_resolved: true
```

**邮件**（取消注释 alertmanager.yml 中的 SMTP 配置）：

```yaml
receivers:
  - name: 'critical-webhook'
    email_configs:
      - to: 'ops-team@example.com'
        headers:
          Subject: '【Critical】{{ .CommonLabels.alertname }}'
```

修改配置后需重启 Alertmanager：

```bash
docker compose -f docker-compose.monitoring.yml restart alertmanager
```

### 常见问题

| 现象 | 排查 |
|------|------|
| Loki 中查不到日志 | 1) 检查 promtail 容器是否运行；2) 检查 promtail 能否访问 docker.sock；3) 查看 promtail 日志 `docker logs ops-promtail` |
| Promtail 报 `connection refused` | Loki 未就绪，promtail 会自动重试，等待 30s 后再检查 |
| 告警未发送 | 1) 检查 Prometheus 是否加载 alerts.yml；2) 检查 alertmanager 是否接收告警（UI: http://host:9093）；3) 检查 webhook 地址是否可达 |
| Loki 占用磁盘过大 | 调整 `loki-config.yml` 中 `retention_period`（默认 168h=7 天） |
| 告警风暴 | 检查 `repeat_interval` 与 `inhibit_rules`；必要时通过 Alertmanager API 创建静默规则 |
