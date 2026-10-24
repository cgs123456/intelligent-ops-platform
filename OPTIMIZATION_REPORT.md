# intelligent-ops-platform 全面审查与优化建议报告

> 审查日期：2026-10-24 · 基线：main @ ae7ae3d
> 方法：全量人工审查（主代理直读 core/routes/models/tasks/adapters/requirements + 2 路子代理深读 services 13800 行与 tests/migrations/CI/CD/部署/监控全栈）+ 本机实机验证（Python 3.11.9 venv 装依赖 + pytest 全量跑）+ 开源同类对比调研（OpsAny、Spug、DB-GPT、WrenAI、Vanna、OpenChatBI、dtstack 选型框架、BIRD 榜）。
> 验证标签：`[实机复现]`=本机跑出；`[测试已红]`=仓库自带测试失败；`[源码验证]`=对照官方文档/上游源码确认；`[静态审查]`=读码推断未运行。

---

## 0. 执行摘要

这是一个**治理意识明显高于平均原型水平、但离"可上生产"仍差一轮 P0 修复**的项目。

- **规模**：services 13800 行 / routes ~1730 / models ~1350 / tests 486 用例 / migrations 12 版本 / docs 6 篇 + 完整 CI/CD/Helm/监控栈。
- **实机验证**：`pytest` 全量 **485 通过 / 11 失败**（2 个安全断言失败 + 9 个 Celery 环境依赖失败，CI 按其依赖文件同样必红）；`pip install -r requirements.txt` 在干净环境**直接失败**（OTel 全系 prerelease）。
- **最要害的三类问题**：
  1. **业务正确性**：退货/调拨不校验正数（负数反向搬运库存）、人工审核改量从不生效、财务对账自锁（有税差必无法关账）、闭环超时后重复下单。
  2. **运行时崩溃**：celery beat 的 schedule 用裸 cron 字符串，**beat 启动即崩**（已对照 celery 源码验证）；迁移链空库 `upgrade` 必失败；CD 健康检查/回滚脚本恒 exit 1。
  3. **工程可信度**：文档描述的备份 S3/WAL/Grafana/读写分离/LangGraph 编排在代码中不存在或损坏——"文档愿景超前于实现"。
- **亮点**（真实存在，非客套）：sqlglot AST 白名单 + 强制 LIMIT 的 Text2SQL 防线、审计哈希链（含 PG advisory lock 串行化）、供应商联系方式字段级加密、ProdConfig 启动强校验（禁 `*` CORS/弱 SECRET_KEY/memory broker）、Dockerfile 多阶段+非 root、486 个测试 + P0-XX 追溯标记、batch_review 的 SAVEPOINT 批量审核模式。

---

## 1. P0：必须先修（按影响排序）

### 1.1 业务正确性（资金/库存出口失守）

| # | 问题 | 位置 | 说明 |
|---|------|------|------|
| P0-1 | 负数量退货反向搬运库存 + 负红字凭证 | `services/erp_service.py:588-636, 683-694` | qty 未过 `_validate_qty_price`；`if qty > max_returnable` 对负数恒通过；`product.stock_qty += qty` 负数即反向扣减；refund_amount 同样无正数校验 |
| P0-2 | 负/零数量调拨方向反转、凭空入库 | `services/erp_service.py:740-783` | `if source_stock < qty` 对负数恒通过；`out_move.qty=-qty` 负负得正=凭空入库；qty=0 也写两条流水；`:751` 行锁查询结果被丢弃 |
| P0-3 | 人工审核"改量"从不生效，RPA 按原始量下单 | `services/aigc_service.py:977-1037` vs `services/closed_loop.py:1156` | docstring 承诺 final_qty 覆盖，实际只写入 review_note；闭环 `int(sug.suggested_qty)` 照原量下单；final_qty=0 仍触发下单；review 无行锁，并发双审可同时通过 |
| P0-4 | 财务对账自锁：匹配放行税差、关账禁止税差 | `services/finance_recon_service.py:361-376 vs :526-527` | auto_match 按 tax_rate 容差放行，close_batch 却要求 discrepancy ≤0.01；有增值税的业务所有明细 matched 也永远无法 close，功能整体不可用 |
| P0-5 | 闭环超时后子线程继续写库 + 重复下单 | `services/closed_loop.py:154-204, 1132-1177` | with_timeout 超时后 daemon 线程继续持共享 scoped_session；step3 PO 已建（独立事务）而 Suggestion 状态在循环末才 commit → 超时回滚后下轮重跑重复下单；`:1174` 用 `product_name in r` 字符串包含去重，同名产品互扰 |
| P0-6 | MRP 释放产生 qty=0 采购单 | `services/mrp_service.py:344-349` | 净需求 0.0001~0.99 通过 `>0` 过滤后 `int()` 截断为 0；直接 new PurchaseOrder 绕过 `_validate_qty_price`；无历史供应商时取 id 最小的任意 active 供应商；成本价当采购价 |

**统一修法**：退货/调拨/MRP 释放三处全部接入 `_validate_qty_price`（qty 正整数、金额>0）；P0-3 补一行 `sug.suggested_qty = final_qty` 并拒绝 0；P0-4 口径二选一（ERP 侧折含税 或 close 容差与 auto_match 一致）；P0-5 给 suggestion_id 加唯一约束 + PO 创建与状态推进同事务。

### 1.2 部署/运行时崩溃

| # | 问题 | 位置 | 验证 |
|---|------|------|------|
| P0-7 | **celery beat 启动即崩**：5 条 beat_schedule 全用裸 cron 字符串 `"0 2 * * *"`；celery `maybe_schedule` 对 str 走 `s.app = app` → `AttributeError`。官方文档明确只接受 int/float/timedelta/crontab | `tasks.py:247-273` | [源码验证] celery/schedules.py + beat.py；`tests/test_tasks.py:262` 只 assert hasattr 恒真测不出。修：`crontab(minute=0, hour=2)` |
| P0-8 | **干净环境装不上**：`opentelemetry-distro>=0.40,<1.0` 在 PyPI 全部版本是 prerelease（b0/b1 后缀），pip 默认拒绝 → `No matching distribution found`；另 Windows GBK locale 读含中文注释的 requirements 需 `PYTHONUTF8=1` | `requirements.txt` | [实机复现]。修：精确 pin（如 `==0.66b1`）或文档标注 `--pre` |
| P0-9 | 迁移链空库必挂：initial_schema 只 create_index/add_column 不建表，核心表全靠 `db.create_all()`；测试也走 create_all + 只静态校验链结构 → 缺陷对测试不可见 | `migrations/versions/4e06934ab2d0_initial_schema.py:21-37`、`g7h8i9j0k1l2:7-16` | K8s/Helm 路径 `flask db upgrade` 第一步即报错 |
| P0-10 | CD 健康检查/回滚恒失败：curl localhost:5000 但 web 只有 `expose` 无端口映射；`docker exec nginx`/`ops_nginx` 但 compose 未设 container_name | `.github/workflows/cd.yml:102,161`、`scripts/rollback.sh:10,17,67` | set -e 下 deploy 与 rollback 必 exit 1 |
| P0-11 | alertmanager `${VAR:default}` 语法错误（envsubst 只认 `${VAR:-default}`），无 envsubst 时 fallback 直接 cp 原文件 → 全部告警通知发送失败 | `monitoring/alertmanager/alertmanager.yml:62,69,79` + entrypoint.sh:19-27 | |
| P0-12 | **CI 红但照常部署**：cd.yml 由 push main 独立触发，无 needs/workflow_run/环境门禁引用 ci.yml 结论 → lint/test/security 全挂的提交照样上线 | `.github/workflows/cd.yml` | |

### 1.3 测试套件本身是红的 `[实机复现]`

`pytest`：**485 通过 / 11 失败**，CI 安装 requirements.txt（含 celery）后同样必红：

- **2 个安全断言失败**（真 bug）：`tests/test_data_agent.py::test_multi_statement_blocked`、`tests/test_security.py::test_sql_injection_multi_statement_drop`。根因：`services/data_agent.py:206-208` 注释称"parse_one 天然保证单语句"，但 sqlglot 25.x 的 `parse_one` 对 `"SELECT ...; DROP TABLE users"` **静默取第一条不抛错**。实际执行因从 AST 重新生成 SQL 而未中招（防线侥幸成立），但"多语句必须拒绝"的契约已破，且 `sqlglot>=23,<26` 区间内行为随版本漂移。修：显式 `len(sqlglot.parse(sql)) == 1` 检查。
- **9 个环境依赖失败**：`test_tasks.py`/`test_coverage_boost.py` 的 Celery 用例假设 celery **未安装**（期望 `_NullTask`），装了 celery 即失败——测试不密闭，且 requirements.txt 本身含 celery。

---

## 2. 安全审查

### 高优先级（P1）

1. **MFA 生命周期缺陷**（`routes/auth.py:82-141`）：`mfa_setup` 未验证任何 TOTP 即置 `mfa_enabled=True`（标准是 verify-then-enable，用户可能被锁死，secret 先落库）；`mfa_disable` 仅需权限不要密码/TOTP 复核——**被盗 access token 即可拆掉 MFA**。
2. **JWT 密钥复用**（`services/auth.py:101,371`）：HS256 直接用 Flask SECRET_KEY 签发，会话签名密钥与框架内部密钥同源。应独立 JWT_SECRET 或迁 RS256。
3. **LLM SQL 对生产库执行**（`services/data_agent.py:285-322`、`services/evaluator.py:243-246`）：AST 白名单/禁 DDL/表白名单/强制 LIMIT 较完善，但**无语句超时、无只读会话强制**（`SELECT count(*)` 全表聚合可拖死库）；`:208/:252` dialect 硬编码 sqlite 而后端可能是 PG。
4. **用户名枚举 + 锁定无 IP 维度**（`services/auth.py:191-207`）：锁定/未锁定响应文案不同可枚举账号；失败计数仅按 username，分布式口令喷洒每号 4 次不受限；`_incr_login_fail_count` Redis get+set 非原子（应 INCR）。
5. **refresh token 并发重放**（`services/auth.py:268-320, 106-145`）：旋转无并发防护，revoke 是 check-then-insert 无唯一约束。
6. **token 进 URL**（`services/rbac.py:133`）：SSE 支持 `?token=` query，JWT 落访问日志/代理日志/Referer。
7. **依赖 CVE**：Flask-Cors 4.0.1（CVE-2024-6839/6844）、Werkzeug 3.0.6（CVE-2024-34069）、gunicorn 23（CVE-2024-1135）——requirements 全部 pin 在有洞版本。
8. **默认密钥**：`config.py:39` SECRET_KEY 默认 `"dev-secret-key-change-in-production"`（ProdConfig 会拦，但中间态环境裸奔）。

### 低优先级（P2）

`rbac.py:269` admin 初始密码写文件失败回退 stderr 明文；`mfa.py:129-132` 历史明文备用码永久有效未强制迁移哈希；`crypto.py:28-30` 用已废弃的 `FLASK_ENV` 判生产（Flask≥2.3 只认 ENV）；`crypto.py:90-95` decrypt 失败返回 None 调用方不判会扩散字段损坏；`idempotency.py:47-55` 匿名命名空间信 X-Forwarded-For；`data_agent.py:175` LLM SQL 明文入日志。正面确认：未发现 SQL 拼接注入（ORM 参数绑定为主）、未发现硬编码密钥。

### 测试环境失真放大安全盲区

`tests/conftest.py:18` RBAC_ENABLED=0 默认关闭（多数 API 测试跑在无鉴权路径）；`:140-141` 登录失败静默返回空 headers（授权用例可能空转通过）。

---

## 3. 性能优化

1. **闭环建议生成串行 LLM 必超时**（`aigc_service.py:176-256`）：N 条建议 N 次串行 LLM（每条 timeout 30s）vs LOOP_TIMEOUT=60s——建议数 >2 几乎必超时，再触发 P0-5。修：并发批调用或改 Celery 异步。
2. **告警每条起新线程无背压**（`notifier.py:132-137`）：告警风暴线程爆炸。修：队列 + 线程池/Celery。
3. **请求线程内长阻塞**：`closed_loop.py:1289-1339` anomaly 定时器线程内同步跑完整 step1（ETL+N 次 LLM）；`rpa_service.py:426` 请求线程内 `time.sleep` 退避重试。
4. **N+1 查询群**：`rpa_service.py:218-219, 307-352`（两套 get_best_quote 并存，批量版已优化）；`mrp_service.py:253-258, 166,174`（深 BOM 递归放大）；`multiagent.py:181-187`。
5. **全量加载无分页**：`erp_service.py:806-825`（inventory_summary）、`:959-979`（reorder rules）；`aigc_service.py:950-975`；`rpa_service.py:437-477`；`warehouse_service.py:338/375/413`；路由层 `routes/erp.py:97-129, 143-181`、`routes/mrp.py` list 均 `query.all()`。
6. **ETL 重写式全量**（`warehouse_service.py:550-583`）：DWS 全量 delete + 逐行 ORM add；backfill 逐天全产品循环；一次 pipeline 十几次小事务（_record_lineage/_update_etl_meta 各自 commit）。修：upsert（PG `ON CONFLICT`）+ 批量执行 + 事务合并。
7. **假读写分离多一跳**（`db_router.py:148,156-193`）：占位实现但每次读前 SELECT 1 探活——零收益纯开销。
8. **SSE 阻塞 gthread**（`routes/loop.py:114-148`）：`time.sleep(5)` 每连接占一个线程 120s。修：队列/Redis pubsub。
9. **分页双查询**（`response.py:129-131`）：count 全集扫描每页两次查询。
10. **审计链写入串行化**（`models/system.py`）：`pg_advisory_xact_lock` 全局串行化 hash 链计算——写入吞吐天花板。可接受为一致性代价，但需知晓。

---

## 4. 架构与代码结构

1. **LangGraph 引入但从未运行**（`closed_loop.py:300-399`）：`_build_graph` 构建的图（条件路由/fan_out/interrupt）从未 invoke，`_run_step_via_graph` 实为 getattr 直调——为依赖付出成本却零编排收益（超时/重试/中断全手写）。要么真用（拿 interrupt 做人工 gate），要么删依赖。
2. **闭环两套执行路径 ~150 行复制**（`closed_loop.py:438-568 vs 778-928`）：锁/LoopState/超时/审计/告警全套两份，改一处漏一处。
3. **ERPService 上帝类**（1344 行）：采购/销售/退货/调拨/凭证/预算/盘点/汇总一锅烩；四个 Service 各自重复 `_to_decimal`/`_gen_*_no`/`self.db = db_session or db.session`——应上提公共基座 + 按聚合拆分。
4. **并发模型不可靠的拼接**：SQLite 下 `with_for_update` 全部失效（6 处注释自认）+ 跨进程文件锁 + 失败退 threading 仅进程内 + session 跨线程共享（P0-5）。生产必须 PG，且应以 DB 约束（唯一索引/检查约束）兜底而非仅依赖应用层锁。
5. **外键语义混用**（`mrp_service.py:357`）：`PurchaseOrder.suggestion_id` 挂 MRPRequirement.id——同一字段两种实体，报表 join 必错。
6. **models/__init__.py 导出不完整**：缺 TokenBlacklist、BOM 全家、Reconciliation 全家（靠 routes/services 直接 import 模块才没炸）。
7. **schema 层迁移烂尾**（`schemas.py`）：仅 8 个 schema，docstring 自认 flask-smorest 迁移未完成；routes 普遍 `request.get_json(force=True)` 手工校验（force=True 连 Content-Type 都不校验）。
8. **单文件超限**：CLAUDE.md 规则 500 行/文件，erp_service 1344、closed_loop 1339、aigc_service 1053 均超标 2 倍+。
9. **魔法数字群**：补货公式 `max(threshold*2, (safety_stock or 10)*5)`、lead_days=5、multiagent 权重 0.5/0.3/0.2、`suppliers[:2]` 静默截断、tax_rate 推导失败默认 0.13——业务假设无配置化。
10. **datetime.now() 全项目 naive**：单号/审计/对账期间全依赖服务器本地时区，与 Celery `Asia/Shanghai + enable_utc=False` 的组合跨机部署即错位。

---

## 5. 业务功能缺口（对比成熟 ERP/MRP/WMS/财务/AIOps）

- **ERP**：无审批流（PO/销售单无多级审批与金额阈值）；凭证无借贷双分录平衡校验；无会计期间锁定/期末结账（可随意改历史）；无批次/序列号全链追踪（StockLot 有 FEFO 但与 stock_qty 解耦）；无 AR/AP 账龄与信用控制（Customer.credit_limit 字段存在但下单不校验）；无价格清单/促销；成本方法固定移动平均（退货冲减用当前 avg_cost，成本账不平 P1-1）；无期末汇兑重估。
- **MRP**：无 MPS 独立需求源；无 time-phased 时段净额（一次性总量）；无计划订单（POR）实体——calculate→release 直接生成 PO，无法模拟/重排；不扣安全库存与在途（P1-10 超额释放）；无批量法则（FOQ/MOQ/包装倍数）；无低层码/CRP/替代料；BOM 无生效日期与 ECN。
- **WMS**：无库位（仅仓库级）；无质检状态/IQC 隔离；无循环盘点（只有全量）；上架/拣货策略缺失。
- **财务对账**：无银行对账单格式适配（MT940/CAMT.053/网银 Excel），import_entries 要手工构造；无手工调节项（手续费/利息差额无法解释只能挂账）；严格 1:1 first-fit 无多对一/部分匹配；期间按 created_at 而非业务日期。
- **AIOps/闭环**：告警无升级/收敛/根因关联（每 product_id 独立告警 + 仅 300s 抑制）；基线仅同星期均值+σ，无再训练/季节性退化检测；无 SLO 跟踪；**闭环缺强制人工 gate**（review 非必须，anomaly 可直接 auto-trigger 下单——资金出口自动化无闸门）；回滚仅 _compensate 且 step4 只记"需人工冲红"；forecast 无促销/节假日变量。
- **平台**：单租户；无 SSO（OIDC/SAML）与服务间认证；审计分散（AuditLog 表与 logger 混用无集中检索）；无请求级分布式追踪（仅 request_id 透传）。

---

## 6. 开源同类对比与可借鉴模式

**定位辨析**：本项目实为 **GenBI + 补货闭环运营系统**，而非通用运维平台。最贴近的开源对标是 OpenChatBI（LangGraph+NL2SQL+异常检测+根因分析），而非 OpsAny/Spug 那类主机运维台。

| 项目 | 规模 | 对本项目的启示 |
|------|------|----------------|
| [DB-GPT](https://github.com/eosphoros-ai/DB-GPT) | 20.2k★ | agentic 数据助理的完整产品化路径（多 agent 编排、AWEL 流） |
| [WrenAI](https://github.com/Canner/WrenAI) | 17.9k★ | **语义层/开放上下文层是 governed Text2SQL 的关键**——本项目 whitelist+sqlglot 是"防"，缺"懂业务口径"的语义描述层 |
| [Vanna](https://github.com/vanna-ai/vanna) | RAG 范式 | **schema 上下文 + 问答对沉淀进向量库**、lifecycle hooks 做缓存与成本追踪、会话存储——直接可抄的 Text2SQL 进化路线（本项目 DDL+3-shot prompt 无任何沉淀机制） |
| [OpenChatBI](https://github.com/OpenBMB/OpenChatBI) | 669★ | 技术栈最接近（LangGraph+NL2SQL+异常+根因），可对标指标设计 |
| [Spug](https://github.com/spug-git/spug) | 11k★ | 中型企业运维台的功能切片参考（批量执行/发布/监控告警的产品化粒度） |
| OpsAny | 企业级 | 9 大 SaaS 的权限/工作台拆分方式 |
| BIRD 榜事实 | — | 最佳 Text2SQL 准确率仅 **67.86%**——本项目"规则引擎兜底 + sqlglot 校验"方向正确，必须保持 LLM-only 路径永不直连资金操作 |
| GitHub 补货 agent 生态 | 多为 0★ 玩具 | 可借鉴模式：**"LLM explains but never computes"**（LLM 只解释不计算）、蒙特卡洛模拟库存风险、per-item 季节性预测 |
| dtstack AIOps 选型框架 | — | 四维评估：看得见（全栈可观测）/能分析（降噪+根因+预测）/能合规/能闭环——本项目"能闭环"是差异化亮点，但"看得见"（Grafana 损坏/promtail 零采集/告警链断）恰是最弱腿 |

**核心战略建议**：不要扩成大而全运维平台。差异化价值在 **"检测→建议→人工审核→自动执行→记账→复盘"的资金级闭环**，竞品都只做了前半段。把闭环做 trustworthy（幂等/审计/人工 gate/对账正确），比补 WMS 库位更有价值。

---

## 7. 可观测性 / 部署 / 文档失真清单

（子代理 6ddb3f63 全量核对，摘要）

**监控栈四断**：① prometheus 抓 postgres/node-exporter 但 compose 只有 redis-exporter（两 job 永久 down + critical 告警常燃）；② promtail 过滤 `project=intelligent-ops-platform` 而 CD 部署目录是 /opt/ops-platform（project 名 ops-platform）→ 业务日志零采集；③ request_id 提为 Loki label 高基数 + max_streams 10000 → 429 丢日志；④ Grafana dashboard JSON 文件损坏（2 行残片）且无 provisioning。加：无 Celery 队列积压/worker 存活告警（异步层零监控）。

**部署**：Helm 只有 web Deployment 无 worker/beat（K8s 缺全部异步能力）；celery-beat schedule 文件无持久卷；nginx healthcheck 疑因 301→自签证书永久 unhealthy；`.env.example` 缺 FIELD_ENCRYPTION_KEY 且 DATABASE_URL 密码为空（照抄即连不上库）；dev compose 跑 FLASK_ENV=production + 硬编码默认密码。

**文档失真**（节选，全部有行号核对）：backup_recovery.md 宣称 S3 上传/WAL 归档/`--clean` 恢复——代码全是 plain+gzip 本地 volume，restore_db.sh 根本没 COPY 进镜像（文档恢复命令必失败）；备份时间三处不一致（02:00/03:30/02:00）；deployment.md 的 gunicorn 配置（sync+preload=True）与实际（gthread+False）相反；README 完全未提 cd.yml 存在自动部署。**建议**：文档瘦身到与代码一致，或补齐实现；每篇文档头部加"对应代码版本"。

---

## 8. 修复路线图

### Phase 1 — 止血（≤2 周，全是小改动）
1. 退货/调拨/MRP 释放接入 `_validate_qty_price`（P0-1/2/6）。
2. `sug.suggested_qty = final_qty` 一行修复 + 拒绝 0（P0-3）。
3. 对账口径统一（P0-4）。
4. beat_schedule 改 `crontab()` 对象（P0-7）。
5. requirements 精确 pin OTel + 升级 Flask-Cors/Werkzeug/gunicorn 补 CVE（P0-8）。
6. `mfa_disable` 加密码/TOTP 复核（安全#1，半行权限判断改动）。
7. data_agent 显式多语句拒绝（`len(sqlglot.parse(sql))==1`），修红测试。
8. Celery 测试改密闭（monkeypatch 安装态而非假设未装）。
9. cd.yml 加 `workflow_run`/环境门禁；健康检查改 `docker compose exec web`；alertmanager 语法 + `amtool check-config` 进 CI。
10. initial schema 补全量建表迁移（或 squash 重建基线）+ CI 加"空 PG 容器 upgrade head"测试。

### Phase 2 — 加固（1~2 月）
1. 幂等键三件套：suggestion_id 唯一约束、ext_order_id↔order_no 唯一索引、step3 状态推进与 PO 同事务（P0-5）。
2. 告警/闭环长任务全量迁 Celery；SSE 改队列；消灭请求线程 sleep 与每告警一线程。
3. LLM 建议生成并发化 + 语句超时 + 只读会话（data_agent set `default_read_only` / PG role）。
4. 分页与 N+1 系统性清理；ETL 改 upsert。
5. MFA verify-then-enable；JWT 独立密钥；登录失败 INCR + IP 维度；refresh 旋转加唯一约束。
6. 监控链修复：promtail project 名/request_id 出 label/Grafana 重建/Celery 队列告警。
7. Helm 补 worker/beat 模板；.env.example 对齐 compose 必填项。
8. 补 routes 层集成测试（aigc/finance/loop/mrp/rpa 五个域当前零路由测试）+ testcontainers 真 Redis/PG。
9. 删死代码：LangGraph 假路径二选一（真用或删依赖）、_build_graph、两套 get_best_quote、FORBIDDEN_KEYWORDS 等。

### Phase 3 — 演进（季度）
按第 5 节缺口排优先级：**审批流（金额阈值+角色）> 会计期间锁定 > 双分录平衡 > MRP time-phased+POR > 对账调节项+多对一 > 库位**；平台侧多租户/SSO/集中审计检索；Text2SQL 引入 Vanna 式 RAG 沉淀 + WrenAI 式语义层。

---

## 9. 总评

| 维度 | 评分（5 分制） | 一句话 |
|------|------|--------|
| 业务设计 | 3.5 | 闭环愿景先进（竞品无此完整度），但资金出口的输入校验系统性缺失 |
| 正确性 | 2 | 6 个 P0 业务 bug，其中对账自锁=功能不可用 |
| 安全 | 3 | 无注入/无硬编码密钥/有加密有 RBAC 有哈希链，但 MFA 生命周期与密钥复用是硬伤 |
| 性能 | 2.5 | 原型量级够用，N+1/全量/串行 LLM/每告警一线程在全量数据下连环引爆 |
| 架构 | 3 | 分层清晰有治理痕迹；LangGraph/读写分离是"简历驱动"空壳，上帝类待拆 |
| 工程化 | 3.5 | 486 测试/CI/CD/Helm/监控全都有——但 CI 是红的、CD 不看 CI、监控四断 |
| 文档 | 2.5 | 数量充足质量不低，但与实现大面积失真（备份/恢复照做必失败） |

**一句话结论**：先花两周做完 Phase 1 的 10 项止血（多数是一行到十行级改动），这个项目就能从"演示可用"变成"可小规模试点"；它的长期价值在资金级闭环的可信度，不在功能广度。
