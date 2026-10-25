# 多租户架构（Multi-Tenancy）

> 状态：已实施（共库共表 + `tenant_id` 列 + ORM 自动过滤）。
> 相关决策记录见本文「设计取舍」；测试见 `tests/test_multi_tenant.py`。

## 1. 模型

| 层 | 机制 |
| --- | --- |
| 存储 | 共库共表，所有业务表带 `tenant_id` 外键（`sys_tenant.id`） |
| 读隔离 | SQLAlchemy `do_orm_execute` 事件 + `with_loader_criteria(TenantScoped, ...)`，对全部 SELECT/UPDATE/DELETE 自动追加 `tenant_id = :current` |
| 写归属 | `before_flush` 事件：`tenant_id is None` 的新对象自动填当前租户（无请求上下文时落默认租户） |
| 租户上下文 | Flask `g.tenant_id`，由 JWT `tid` 声明回填（`login_required` / `require_permission` / `verify_sse_auth` 三处） |
| 身份 | `User.tenant_id` 可空：`NULL` = 平台管理员（跨租户），非空 = 租户用户 |
| 唯一性 | 业务唯一键全部改为租户内唯一复合约束（`ux_product_tenant_sku` 等 12 项 + 既有复合键前置 `tenant_id`），跨租户可重名 |

### 表分类

- **租户隔离表（43 张）**：ERP / AIGC / 数仓 ODS-DWD-DWS-ADS / 对账 / BOM-MRP / 闭环状态等全部业务表，挂 `TenantScoped` mixin。
- **仅记录维度（1 张）**：`audit_log` 带 `tenant_id` 但**不自动过滤**——哈希链是全局单链，防篡改属平台级完整性；查询过滤在路由层手动（`/api/v1/audit/logs`、`/stats` 按租户过滤，`/verify-chain` 刻意全局）。
- **全局表**：`sys_tenant`、`sys_user`（`tenant_id` 列区分归属）、`sys_role`、`sys_token_blacklist`、`user_roles`。角色/用户名全局唯一是共库共表的标准取舍（登录无租户前缀）。

## 2. 隔离机制的边界（重要）

自动过滤只覆盖 **ORM 路径**。以下路径为显式处理，升级代码时必须保持：

| 路径 | 处理方式 |
| --- | --- |
| `warehouse_service._upsert`（Core insert） | 行内 `setdefault("tenant_id", tid)` + `ON CONFLICT` 键含 `tenant_id`；原生聚合 SQL 手写 `WHERE tenant_id=:tid` |
| `data_agent`（用户自由 SQL） | LLM 生成 SQL 后经 sqlglot AST 改写：每个真实表（跳过 CTE）追加 `table.tenant_id = :tid`（`_tenant_rewrite`），改写失败拒绝执行 |
| 三处 `_audit` | `AuditLog.tenant_id` 列 default 调用 `current_tenant_id()`，平台管理员落默认租户 |

## 3. 租户管理 API（`routes/tenant.py`）

全部要求 `tenant:manage` 权限 **且** 平台管理员身份（`g.tenant_id is None`），租户管理员（即使 `*:*`）访问返回 403 `ERR_SYS_003`：

- `GET /api/v1/tenants` — 租户列表（含用户数）
- `POST /api/v1/tenants` — 创建（`code` 租户内唯一，409）
- `GET /api/v1/tenants/<id>/users` — 租户用户列表
- `POST /api/v1/tenants/<id>/users` — 开户（`validate_password` 强度校验；`role ∈ {admin, operator, viewer}` 引用全局角色表）

默认租户：`id=1`（`DEFAULT_TENANT_ID`），迁移与 seed 均幂等预置；未携带租户上下文的写入（历史数据、内部任务）落默认租户，保证存量行为不变。

## 4. 设计取舍

- **共库共表 vs 独库/独立 schema**：当前规模（单组织起步、多租户预留）下共库共表运维成本最低；若未来单租户数据量或合规要求升级，按 `tenant_id` 分库导出的迁移路径是开放的（所有数据已带租户标记）。
- **平台管理员 = `tenant_id IS NULL`**：不引入独立 admin 表；语义为「不属于任何租户即不受过滤」，与 `with_loader_criteria` 的跳过逻辑天然一致。
- **审计哈希链全局单链**：租户间链交织使单租户内部人无法重写历史而不破坏全局链；租户维度的审计查询在路由层过滤。
- **JWT 携带 `tid`**：令牌签发时固化租户，用户被转移租户后旧令牌自动失效于过滤层（`tid` 不匹配）。

## 5. 已知限制

- 平台管理员操作业务表会看到全租户数据——这是设计语义，不是泄漏；审计会记录 `tenant_id=1`。
- `data_agent` 的 SQL 改写覆盖单层/嵌套 CTE 与 JOIN 的真实表；若未来引入子查询外的动态 SQL 形态（如存储过程），需同步扩展 `_tenant_rewrite`。

## 6. 异步执行链路的租户传递

定时/异步任务是租户泄漏的高发点（worker 的 app context 里 `g.tenant_id` 未设置会静默落默认租户）。当前链路的保证与测试锚点：

- **用户触发的任务**（`run_loop_step_async`）：路由提交时把 `g.tenant_id` 作为 `tenant_id` 参数传入，worker 内 `_run_as_tenant` 显式设置并在结束后恢复（防 worker 线程残留上一个任务的租户）。
- **定时任务**（ETL/RPA 采集/建议生成/日报/异常检测）：`_run_for_each_tenant` 枚举全部活跃租户逐个执行，返回 `{tenant_id: result|{"error": ...}}`。失败语义：单租户失败记录明细不中断其他租户（整任务重试会重跑已成功租户，对补货建议生成有放大风险）；全部租户失败视为环境故障 raise 触发 Celery retry 与 on_failure 告警。
- **步骤超时子线程**（`closed_loop.with_timeout`）：子线程 push 的是全新 app context，`g` 不随线程迁移——装饰器显式捕获并回放外层租户（`None` 平台管理员语义原样传递）。
- 回归测试：`tests/test_multi_tenant.py::TestTenantContextPropagation`（7 用例：_run_as_tenant 设置/恢复/None 语义、遍历执行、部分失败不 raise、全部失败 raise、with_timeout 租户/平台管理员传递）。
