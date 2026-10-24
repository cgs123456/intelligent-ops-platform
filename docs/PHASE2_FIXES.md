# Phase 2 修复记录（P1/P2 级）

对应《OPTIMIZATION_REPORT.md》Phase 2 九项。Phase 0/1 见《PHASE1_FIXES.md》。
验证基线：全量 pytest **529 passed / 2 skipped / 0 failed**（新增 26 个回归测试）。

## 一、业务正确性与幂等

### P2-1 采购单幂等三件套
- `models/erp.py`：`PurchaseOrder.suggestion_id / ext_order_no` 改 `unique=True, index=True`——一条补货建议最多一张采购单，外部单号一一映射。
- `services/closed_loop.py` step3：下单前按 `suggestion_id` 查重（存在则跳过并补推 `ordered` 状态），每单成功即 commit，把崩溃窗口内重跑的重复下单从"可能"变为"不可能"。
- 迁移说明：原增量迁移 `b00000000002` 已删除——baseline `b00000000001` 使用 `metadata.create_all`（含模型索引），二者叠加会在空库 upgrade 时报 `index already exists`。项目无存量库，模型即真相，迁移链保持单 baseline（SQLite 空库 upgrade 实测通过，幂等索引确认存在）。

### P2-4 查询分页与 ETL upsert
- `routes/erp.py` `/reorder-rules`、`/budgets`，`routes/mrp.py` `/boms`：全部 `paginate`（默认 1/50，per_page 上限 200），响应统一 `{items, page, per_page, total}`。
- `routes/finance.py` `batch_report`：未匹配明细加 `limit`（默认 200，上限 500）。
- `services/warehouse_service.py` `_etl_dws`：改 `INSERT ... ON CONFLICT DO UPDATE`（PG/SQLite 方言分支），删掉全量 delete 重写，`(product_id, dt)` 冲突键防翻倍。
- 评估后不改：`services/response.py` `paginate_response` 的 COUNT+SELECT 双查询是分页标准模式。

## 二、异步与资源

### P2-2 SSE 与告警线程
- `routes/loop.py` `generate()`：固定 `sleep(5)` 改自适应轮询——状态活跃期（20s 内有变化）1s 快轮询保证看板低延迟，静默期回落 5s，总时长上限 120s 不变，心跳 15s 不变。多 worker 下跨进程事件不可行（无 Redis pub/sub 依赖），保留 DB 轮询是正确取舍。
- `services/notifier.py`：每条告警新建 `threading.Thread` 改为共享单 worker `ThreadPoolExecutor`——告警风暴时线程数不再随告警数线性增长，且串行化发送顺带规避 webhook 限流；三渠道均有超时（钉钉/企微 5s、SMTP 10s），单 worker 最坏阻塞可控。

### P2-3 DataAgent 只读会话
- `services/data_agent.py` `_execute_sql`：PG 分支事务首语句 `SET TRANSACTION READ ONLY`（先 commit 清 pending）；SQLite 分支 `PRAGMA query_only`（fairy 连接上执行，finally 中先 PRAGMA OFF 再 rollback 归还连接，防止污染连接池）。

## 三、安全

### P2-5 认证加固
- `JWT_SECRET_KEY` 独立配置（回退 `SECRET_KEY` 向后兼容），`services/auth.py` 签发/校验全部切换。
- 登录失败计数增加 IP 维度（单 IP 20 次/窗口），Redis pipeline `INCR+EXPIRE NX`（窗口从首次失败起算）；账号/IP 双维度锁定文案统一为"登录失败次数过多"，防用户名枚举。
- refresh token 旋转竞态：`IntegrityError`（`TokenBlacklist.jti` 唯一约束兜底）→ 回滚并返回"已被使用，请重新登录"，并发重放双请求只有一个成功。

## 四、监控链（P2-6，断点②③④⑤修复；①经核实不成立）

- ② `promtail-config.yml`：`filters` 同时放行 `ops-platform`（CD 部署目录 /opt/ops-platform 的 compose project 名）与 `intelligent-ops-platform`（仓库目录本地运行），修复生产日志零采集。
- ③ `request_id` 退出 Loki label（高基数标签会把流数量推向 `max_streams` 触发 429 丢日志），保留在日志行 JSON 里，查询用 `| json | request_id="…"`。
- ④ Grafana 补齐：`docker-compose.monitoring.yml` 新增 grafana 服务（10.4.5，仅本机端口，匿名访问关闭，告警统一走 Alertmanager）；`provisioning/datasources`（prometheus/loki 显式 uid）+ `provisioning/dashboards`；重建 `dashboards/ops-platform.json`（8 面板：QPS/P95/连接池/up/Celery 积压/Redis/磁盘/错误日志速率）。
- ⑤ Celery 队列监控：redis-exporter `REDIS_EXPORTER_CHECK_SINGLE_KEYS=db2=celery` 暴露 `redis_key_size`，`alerts.yml` 新增 `CeleryQueueBacklog`（>1000 持续 5m warning）。worker 停摆时队列持续增长由该告警间接反映，容器崩溃由 compose `restart: unless-stopped` 自愈。
- ① 复核结论：postgres-exporter / node-exporter 服务实际已在 compose 中部署（报告写作时点的旧状态），prometheus 抓取 job 与 DiskSpaceLow 告警保留。

## 五、部署（P2-7）

- Helm 补 `deployment-worker.yaml`（concurrency 可配、优雅终止 90s）与 `deployment-beat.yaml`（单例 Recreate，schedule 落 /tmp）；`values.yaml` `celery` 段。模板括号平衡与 values 解析已验证，真渲染待集群环境。
- `.env.example` 补 `FIELD_ENCRYPTION_KEY`（compose 强制校验项）与 `JWT_SECRET_KEY`。

## 六、测试（P2-8/P2-9）

- `test_p1_fixes.py` 9 用例：幂等键/只读会话/JWT 隔离/IP 锁定/refresh 重放。
- `test_routes_integration.py` 14→27：补 loop / aigc / finance / mrp 四域路由集成测试（只测校验分支与列表往返，不触发 LLM/Text2SQL 真实调用）。
- `test_aigc_service.py` / `test_finance_recon.py`：final_qty 覆盖、税容差关账回归。
- `test_tasks.py` / `test_coverage_boost.py`：Celery 密闭化重写（可用/不可用两个世界各自断言，beat schedule 全 crontab 类型检查）。
- ruff `F401/F811/F841` 清零（forecast.py / notifier.py / test_p1_fixes.py 三处）。

### P2-9 深挖：LangGraph 死路径删除（报告点名项）

报告 Phase 2 第 9 项点名的 "LangGraph 假路径二选一、`_build_graph`" 经核实为真死代码并删除（`_route_after_step1/_route_after_step3` 有测试且被单步路由引用，保留；"两套 `get_best_quote`" 复核后均有真实调用方，报告误判，不删；`FORBIDDEN_KEYWORDS` 在用，不删）：

- `services/closed_loop.py`：删 langgraph import 块（`StateGraph/END/Command/interrupt` + `_HAS_LANGGRAPH`）、`_graph/_graph_lock` 类属性、`_get_graph()`、`_build_graph()`（约 60 行图定义）、`_node_fan_out()`、`_run_step_legacy()`（if-elif 降级双路径约 150 行）；`run_step()` 直连节点函数分发；`ClosedLoopState` 从 langgraph 条件定义改为无条件 `TypedDict`；模块/类/节点 docstring 同步去 LangGraph 化。净删约 700 行。
- `requirements.txt`：删 `langgraph>=0.2,<1.0` 与无主依赖 `langchain-core>=0.3,<1.0`（代码零 import，属孤儿依赖）；干净解析复验通过（解析结果不再含 langchain/langsmith）。
- `tests/test_closed_loop.py`：删 `TestClosedLoopNodeFanOut`（死代码的测试随死代码走）。
- `README.md` / `docs/architecture.md`：LangGraph 编排描述改写为「节点函数单步执行模式」决策记录（含删除理由：interrupt 语义用不上、图定义与串行驱动模式冲突、降级双路径维护负担）。
- 回归：全量 528 passed / 2 skipped（-1 为删除的 fan_out 死测试）；ruff F 类零告警。

## 遗留已知边界

1. `flask db check` 对 audit_log 分区子表有 7 个 `remove_table` 固有误报（alembic autogenerate 不理解声明式分区），CI 用表数/分区抽查替代。
2. dev 语义（create_all 抢跑）下 audit_log 是普通表，仅生产语义（baseline 迁移）是分区表——由 baseline 内 `has_table` 守卫保证幂等。
3. Helm chart 仅静态校验（YAML 解析+括号平衡），未在真实集群渲染部署。
