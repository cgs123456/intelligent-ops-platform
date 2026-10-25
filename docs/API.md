# API 接口清单

所有接口统一前缀 `/api/v1/`,JWT 认证(access 2h + refresh 7d,HttpOnly Cookie 或 `Authorization: Bearer <token>`)。响应统一 envelope:`{"code": "OK" | "ERR_*", "message": "...", "data": ...}`。

标注 `@idempotent` 的写接口支持 `Idempotency-Key` 请求头幂等重试(24h 窗口)。

权限列说明:admin 通配 `*:*`;operator/viewer 角色权限见 `services/rbac.py` 初始化定义;平台管理接口要求 `tenant_id IS NULL` 的平台管理员账号。

## 认证(auth)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | /api/v1/auth/login | - | 登录,返回 access + refresh token;失败 5 次锁定 |
| POST | /api/v1/auth/refresh | - | 刷新 token(旋转 + 旧 token 拉黑) |
| POST | /api/v1/auth/logout | - | 登出,吊销 token |
| GET | /api/v1/auth/me | - | 当前用户信息(仅 JWT 认证) |
| POST | /api/v1/auth/change-password | - | 修改密码(走 password_policy 校验) |
| POST | /api/v1/auth/mfa/setup | auth:mfa:setup | 启用 TOTP MFA |
| POST | /api/v1/auth/mfa/disable | auth:mfa:disable | 禁用 MFA(需密码复核) |

## 租户管理(tenant,仅平台管理员)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/tenants | tenant:manage | 租户列表(含用户数) |
| POST | /api/v1/tenants | tenant:manage | 创建租户(code 唯一) |
| GET | /api/v1/tenants/{id}/users | tenant:manage | 租户用户列表 |
| POST | /api/v1/tenants/{id}/users | tenant:manage | 为租户开户(密码强度校验) |

多租户模型与隔离语义见 [MULTI_TENANCY.md](MULTI_TENANCY.md)。

## ERP(erp)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/erp/inventory | erp:read | 库存清单(含安全库存低于标记) |
| GET | /api/v1/erp/orders | erp:read | 近期采购单(limit 上限 100) |
| GET | /api/v1/erp/account | erp:read | 财务概览 |
| GET | /api/v1/erp/warehouses | erp:read | 仓库列表 |
| POST | /api/v1/erp/returns | return:write | 创建退货(@idempotent) |
| POST | /api/v1/erp/transfers | transfer:write | 创建调拨(可挂库位,@idempotent) |
| GET | /api/v1/erp/reorder-rules | erp:read | 补货预警规则列表(分页) |
| GET | /api/v1/erp/reorder-check | erp:read | 触发库存预警检查 |
| GET | /api/v1/erp/budgets | erp:read | 预算列表(分页) |
| POST | /api/v1/erp/budgets/<id>/refresh | erp:write | 刷新预算实际发生额(@idempotent) |
| GET | /api/v1/erp/cost-centers | erp:read | 成本中心列表 |
| POST | /api/v1/erp/purchase-orders | purchase:write | 创建采购单(@idempotent) |
| POST | /api/v1/erp/purchase-orders/<no>/confirm | purchase:write | 确认采购单(会计期间校验,@idempotent) |
| POST | /api/v1/erp/purchase-orders/<no>/receive | purchase:write | 到货入库(@idempotent) |
| POST | /api/v1/erp/purchase-orders/<no>/cancel | purchase:write | 取消采购单(@idempotent) |
| POST | /api/v1/erp/purchase-orders/<no>/return | purchase:write | 采购退货(@idempotent) |
| POST | /api/v1/erp/sale-orders | sale:write | 创建销售单(会计期间校验,@idempotent) |
| POST | /api/v1/erp/stock-takes | erp:write | 创建盘点单(@idempotent) |
| GET | /api/v1/erp/stock-takes | erp:read | 盘点单列表(支持 product_id/status 过滤) |
| POST | /api/v1/erp/stock-takes/<id>/confirm | erp:write | 确认盘点:调整库存+流水(@idempotent) |
| POST | /api/v1/erp/products/<id>/safety-stock-recalc | erp:write | 安全库存重算(SS=z×σ×√LT,支持 service_level/window_days/apply) |
| GET | /api/v1/erp/accounting-periods | erp:read | 会计期间列表 |
| POST | /api/v1/erp/accounting-periods | erp:write | 创建会计期间(YYYY-MM) |
| POST | /api/v1/erp/accounting-periods/<name>/close | erp:write | 关闭会计期间(关账月禁止过账) |
| GET | /api/v1/erp/trial-balance | erp:read | 科目余额表 + 凭证断链检查 |
| GET | /api/v1/erp/locations | erp:read | 库位清单(可按仓库过滤) |
| POST | /api/v1/erp/locations | erp:write | 创建库位(仓库内 code 唯一) |
| GET | /api/v1/erp/locations/<id>/stock | erp:read | 库位现存量(流水聚合) |

## RPA(rpa)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/rpa/quotes | rpa:read | 采集供应商报价 |
| POST | /api/v1/rpa/sync-orders | rpa:write | 同步电商订单 |
| GET | /api/v1/rpa/schedule/status | rpa:read | 调度状态 |
| POST | /api/v1/rpa/schedule/run | rpa:write | 立即执行调度任务 |

## FDE(fde)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | /api/v1/fde/run | fde:run | 执行 ETL 流水线 |
| GET | /api/v1/fde/stats | fde:read | 分层统计 |
| GET | /api/v1/fde/ads | fde:read | ADS 应用层数据 |
| GET | /api/v1/fde/lineage | fde:read | 数据血缘 |
| GET | /api/v1/fde/data-quality | fde:read | 数据质量报告 |
| GET | /api/v1/fde/anomalies | fde:read | 时序异常检测(支持 date/trigger 参数,trigger=true 触发闭环+告警) |
| POST | /api/v1/fde/run-dq | fde:run | 执行数据质量校验 |
| POST | /api/v1/fde/backfill | fde:run | 重跑 ETL 指定日期范围[DWS+ADS] |
| GET | /api/v1/fde/sku-classification | fde:read | SKU ABC/XYZ 分级(支持 dt/abc/xyz 过滤) |

## AIGC(aigc)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/aigc/suggestions | aigc:read | 待审建议列表 |
| POST | /api/v1/aigc/generate-suggestions | aigc:review | 生成补货建议 |
| POST | /api/v1/aigc/review | aigc:review | 审核建议(支持 final_qty;金额≥SUGGESTION_HIGH_VALUE_AMOUNT 需 aigc:approve_high) |
| POST | /api/v1/aigc/batch-review | aigc:review | 批量审核 |
| GET | /api/v1/aigc/report | aigc:read | 最新日报 |
| POST | /api/v1/aigc/generate-report | fde:run | 生成 4 段式 LLM 日报(支持 push/date 参数,push=true 多渠道推送) |
| POST | /api/v1/aigc/query | aigc:read | 同步智能问答 |
| POST | /api/v1/aigc/query-stream | aigc:read | SSE 流式智能问答 |
| POST | /api/v1/aigc/data-query | aigc:read | Data Agent Text2SQL 全链路(NL→SQL→执行→NL) |
| POST | /api/v1/aigc/negotiate | aigc:review | 多 Agent 采购博弈(买方+双供应商 LLM 角色扮演) |
| GET | /api/v1/aigc/chat-history/<session_id> | aigc:read | 对话历史 |
| GET | /api/v1/aigc/feedback-stats | aigc:read | 反馈统计 |

## MRP(mrp)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | /api/v1/mrp/boms | mrp:write | 创建 BOM(物料清单)(@idempotent) |
| GET | /api/v1/mrp/boms | mrp:read | 查询 BOM 列表(分页) |
| POST | /api/v1/mrp/plans | mrp:write | 创建 MRP 计划(@idempotent) |
| POST | /api/v1/mrp/plans/<id>/calculate | mrp:write | 执行净需求计算 |
| POST | /api/v1/mrp/plans/<id>/release | mrp:write | 下达计划:转为草稿采购单(@idempotent) |

## 财务对账(finance)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | /api/v1/finance/reconciliation/batches | recon:create | 创建对账批次(@idempotent) |
| GET | /api/v1/finance/reconciliation/batches | finance:read | 对账批次列表(支持 period/status 过滤) |
| POST | /api/v1/finance/reconciliation/batches/<id>/import | recon:import | 导入外部流水明细(@idempotent) |
| POST | /api/v1/finance/reconciliation/batches/<id>/auto-match | recon:match | 自动匹配(exact + 税容差 + 多对一组合) |
| GET | /api/v1/finance/reconciliation/batches/<id>/report | finance:read | 对账报告(含未匹配明细) |
| POST | /api/v1/finance/reconciliation/batches/<id>/close | recon:close | 关闭对账批次(要求无未匹配) |
| POST | /api/v1/finance/reconciliation/batches/<id>/adjustments | recon:create | 人工调节项(配平差异后可关账) |

## 闭环(loop)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/loop/status | loop:read | 闭环状态 |
| POST | /api/v1/loop/run-step | loop:run | 同步执行步骤 |
| POST | /api/v1/loop/run-step-async | loop:run | 异步执行(需 Celery) |
| GET | /api/v1/loop/task/<task_id> | loop:read | 查询异步任务状态 |
| POST | /api/v1/loop/rollback | loop:rollback | 回滚指定步骤 |
| POST | /api/v1/loop/reset | loop:reset | 重置闭环状态(仅 admin) |
| GET | /api/v1/loop/auto-trigger-check | loop:read | 触发自动检测闭环状态 |
| GET | /api/v1/loop/stream | loop:read | SSE 实时进度推送 |

## 审计(audit)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/audit/logs | audit:read | 分页查询审计日志(支持 actor/action/target_type/日期过滤;租户内自动隔离) |
| GET | /api/v1/audit/stats | audit:read | 最近 30 天按 action 聚合 |
| GET | /api/v1/audit/verify-chain | audit:read | 校验审计日志 hash chain 完整性(平台级) |

## 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /health | 深度健康检查(数据库 + Redis + 磁盘) |
| GET | /health/live | 存活探针(轻量) |
| GET | /health/ready | 就绪探针(依赖检查) |
| GET | /api/dashboard | 仪表盘汇总(TOP KPI,erp:read) |
| GET | /metrics | Prometheus 指标(METRICS_ENABLED=1 时) |
| GET | /docs/swagger | API 文档(API_DOCS_ENABLED=1 时) |
