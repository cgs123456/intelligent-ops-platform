# 备份与恢复指南

> 目标：明确智能运营平台的备份策略、恢复步骤与容灾目标，确保数据可恢复、业务可快速续接。
>
> **容灾目标**
> - **RPO（Recovery Point Objective，恢复点目标）= 24h**：最多丢失 24 小时数据。
> - **RTO（Recovery Time Objective，恢复恢复时间目标）= 4h**：故障发生后 4 小时内恢复服务。

---

## 1. 容灾目标定义

| 指标 | 目标 | 说明 |
| --- | --- | --- |
| RPO | 24h | 每日全量备份 + 持续归档日志，最坏情况丢失不超过一个备份周期（24h）。 |
| RTO | 4h | 从触发恢复到服务恢复对外可用，含数据恢复 + 应用重启 + 健康检查通过。 |
| 备份保留 | 7 天 | 每日全量备份保留 7 天，超过自动清理。 |
| 恢复演练 | 每季度 1 次 | 每季度执行一次完整恢复演练，验证 RPO/RTO 达标。 |

> 注：如业务对数据丢失容忍度更低（如财务模块要求 RPO≤1h），应启用 PostgreSQL 流复制 + 增量 WAL 归档（见下文「进阶：缩短 RPO」）。

---

## 2. 备份范围

| 组件 | 备份对象 | 备份方式 | 频率 |
| --- | --- | --- | --- |
| PostgreSQL | 业务数据（用户、订单、审计日志、闭环状态等） | `pg_dump` 全量 + WAL 归档 | 每日 02:00 全量；WAL 持续归档 |
| Redis | 缓存 + 限流计数 + 登录失败计数 + token 黑名单 | RDB 快照 + AOF | RDB 每 1h；AOF 实时（Redis 仅缓存可重建，备份优先级低） |
| 应用配置 | Helm values / .env / Secret | 版本库 + Secret 备份 | 每次变更即提交 |
| 审计日志 | AuditLog（含 hash chain） | 随 PostgreSQL 备份 + 独立归档 | 随 PG 全量 + 每月独立归档至对象存储 |
| 对象存储 | 用户上传附件（如有） | 存储层跨区域复制 | 实时 |

> 关键约束：审计日志（AuditLog）含 hash chain 防篡改链，恢复时必须整表恢复，禁止部分插入，否则链断裂会被 `AuditLog.verify_chain()` 检测出篡改。

---

## 3. 备份策略

### 3.1 PostgreSQL 全量备份（每日 02:00）

由定时任务（K8s CronJob / 主机 crontab）执行：

```bash
# 环境变量（由部署注入）
# PG_HOST / PG_PORT / PG_USER / PG_PASSWORD / PG_DB
# BACKUP_RETENTION_DAYS=7

set -euo pipefail
TS=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backups/postgres"
mkdir -p "$BACKUP_DIR"
FILE="$BACKUP_DIR/ops_platform_${TS}.sql.gz"

# 全量逻辑备份（自定义格式 + 压缩），含 schema + 数据
PGPASSWORD="$PG_PASSWORD" pg_dump \
  -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
  --format=custom --compress=9 \
  -f "${FILE%.gz}"

gzip "$FILE"

# 上传到对象存储（S3 / OSS / MinIO）
# aws s3 cp "$FILE.gz" "s3://ops-backup/postgres/$(basename $FILE.gz)"

# 清理超过保留期的本地备份
find "$BACKUP_DIR" -name "ops_platform_*.sql.gz" -mtime +${BACKUP_RETENTION_DAYS:-7} -delete

echo "[$(date)] PostgreSQL 备份完成: $FILE.gz"
```

定时表达式（与 `.env.example` 的 `BACKUP_CRON=0 2 * * *` 一致）：每日 02:00。

### 3.2 PostgreSQL WAL 归档（缩短 RPO）

启用 WAL 归档后，可在全量备份基础上做时间点恢复（PITR），将 RPO 缩短到分钟级。

`postgresql.conf` 关键配置：
```
wal_level = replica
archive_mode = on
archive_command = 'test ! -f /archive/%f && cp %p /archive/%f'
archive_timeout = 300s
```

### 3.3 Redis 备份

Redis 在本平台仅作缓存/限流/计数用途（数据可重建），备份优先级低。生产环境启用 RDB 快照：

```
# redis.conf
save 3600 1       # 1h 内至少 1 个 key 变化则快照
appendonly yes    # 开启 AOF 持久化
appendfsync everysec
```

> 注意：token 黑名单（TokenBlacklist）持久化在 PostgreSQL，不依赖 Redis。Redis 不可用时 `AuthService.is_token_revoked` 会降级为内存 set + DB 查询（见 `services/auth.py` P2-6）。

### 3.4 配置与密钥备份

- **Helm values**：提交到 Git 版本库（`deploy/helm/intelligent-ops-platform/values.yaml`）。
- **Secret**：禁止明文入库。使用 External Secrets / Sealed Secrets / 云 KMS 托管，由 KMS 负责其备份与轮换。
- **.env**：仅本地开发用，不入库（已在 `.gitignore`）。

---

## 4. 恢复步骤

### 4.1 PostgreSQL 恢复（RTO 主要耗时项）

#### 场景 A：从全量备份恢复（RPO ≤ 24h）

```bash
set -euo pipefail
# 1. 选定待恢复的备份文件（取最近一次成功的全量备份）
BACKUP_FILE="/backups/postgres/ops_platform_YYYYMMDD_020000.sql.gz"

# 2. 停止应用（避免恢复期间有写入）
# kubectl scale deployment intelligent-ops-platform --replicas=0

# 3. 恢复前先备份当前（损坏的）数据库，防止恢复操作失误覆盖
PGPASSWORD="$PG_PASSWORD" pg_dump -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" \
  --format=custom -f "/backups/postgres/pre_restore_$(date +%s).sql.gz" || true

# 4. 恢复：先清库再导入（--clean 自动 DROP 再 CREATE）
gunzip -c "$BACKUP_FILE" | \
PGPASSWORD="$PG_PASSWORD" pg_restore \
  -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" \
  --clean --if-exists --no-owner --no-privileges

# 5. 校验 hash chain 完整性（检测备份/恢复过程中是否被篡改）
# 进入应用 shell 执行：
#   python -c "from app import app; from models.system import AuditLog; \
#     print('篡改记录 id =', AuditLog.verify_chain())"
# 输出 None 表示链完整。

# 6. 重启应用
# kubectl scale deployment intelligent-ops-platform --replicas=<原副本数>

# 7. 健康检查
curl -s http://<app-host>/health/ready | jq .
```

#### 场景 B：时间点恢复（PITR，RPO ≤ 分钟级）

适用于启用了 WAL 归档的场景：

```bash
# 1. 准备新数据目录
mkdir -p /var/lib/postgresql/recovery
chown postgres:postgres /var/lib/postgresql/recovery

# 2. 恢复基础全量备份（物理备份）
pg_basebackup -h "$PG_HOST" -U repl -D /var/lib/postgresql/recovery -P -R

# 3. 配置恢复目标时间点（例如恢复到故障前 10 分钟）
cat >> /var/lib/postgresql/recovery/postgresql.auto.conf <<EOF
restore_command = 'cp /archive/%f %p'
recovery_target_time = '2026-07-30 01:50:00+08'
recovery_target_action = 'promote'
EOF
touch /var/lib/postgresql/recovery/recovery.signal

# 4. 启动 PostgreSQL，自动进入恢复模式并回放 WAL 至目标时间点
pg_ctl -D /var/lib/postgresql/recovery start

# 5. 验证数据后切换应用连接到新实例
```

### 4.2 Redis 恢复

由于 Redis 数据可重建（缓存/限流/计数），默认 **不恢复 Redis**，重启后由应用自动重建。

如确需恢复 RDB 快照：

```bash
# 1. 停止 Redis
# 2. 用最近一次 RDB 快照替换 dump.rdb
cp /backups/redis/dump_YYYYMMDD.rdb /data/dump.rdb
# 3. 启动 Redis（自动加载 dump.rdb）
# 4. 应用重启后自动从 DB 回填缓存
```

### 4.3 应用层恢复

```bash
# 1. 确认配置（Helm values / Secret）正确
helm get values intelligent-ops-platform -n <namespace>

# 2. 滚动重启，确保所有 worker 连接新数据库
kubectl rollout restart deployment/intelligent-ops-platform -n <namespace>

# 3. 等待滚动更新完成
kubectl rollout status deployment/intelligent-ops-platform -n <namespace> --timeout=300s

# 4. 健康检查（就绪探针通过即对外可用）
kubectl get pods -n <namespace> -l app.kubernetes.io/name=intelligent-ops-platform
curl -s https://<app-host>/health/ready | jq .
```

---

## 5. RTO 分解（4 小时预算）

| 阶段 | 预估耗时 | 责任方 |
| --- | --- | --- |
| 故障发现与告警 | 5 min | Alertmanager + 值班 SRE |
| 故障定位与决策（是否走备份恢复） | 30 min | SRE + DBA |
| 拉取并恢复 PostgreSQL 全量备份 | 60 min | DBA |
| hash chain 完整性校验 | 10 min | SRE |
| 应用滚动重启 + 就绪检查 | 15 min | SRE |
| 业务回归验证（关键链路冒烟） | 30 min | QA + 业务 |
| 缓冲（异常处理 / 数据核对） | 30 min | — |
| **合计** | **≤ 4h** | — |

> 若使用 PITR（场景 B），WAL 回放额外耗时约 30 min，仍在 4h 预算内。

---

## 6. 恢复演练

- **频率**：每季度 1 次（建议季末非业务高峰）。
- **流程**：
  1. 在隔离环境（独立命名空间 / 独立集群）从最新备份恢复。
  2. 执行 `AuditLog.verify_chain()` 校验审计链完整性。
  3. 跑核心业务冒烟用例（登录 / 下单 / 闭环触发 / AIGC 查询）。
  4. 记录实际 RPO/RTO，与本文件目标比对，不达标则整改。
- **产出**：演练报告归档至 `docs/ops/`，并在 ADR 中记录改进项。

---

## 7. 备份监控与告警

- 备份任务失败需触发告警（Alertmanager critical 级别）：
  - CronJob `status.failed >= 1`（K8s Job 失败）。
  - 备份文件最近修改时间 > 26h（备份未按时生成）。
- 每日检查项：
  - 最近一次备份文件大小 > 0。
  - 备份目录磁盘占用 < 80%。
  - WAL 归档目录持续增长（归档未中断）。

---

## 8. 相关文件

- 备份脚本目录：`scripts/`（部署时由 CronJob 挂载执行）。
- 备份保留期环境变量：`.env.example` 中 `BACKUP_CRON` / `BACKUP_RETENTION_DAYS`。
- 审计日志 hash chain 校验：`models/system.py` 的 `AuditLog.verify_chain()`。
- token 黑名单 Redis 降级：`services/auth.py` 的 `AuthService.is_token_revoked()`。
- 健康检查端点：`/health/ready`（就绪探针，恢复后用于验证）。
