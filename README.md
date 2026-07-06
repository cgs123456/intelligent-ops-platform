# 中型企业智能运营平台

RPA / ERP / FDE / AIGC 四层闭环协同的可运行原型系统。通过 AIGC 生成补货建议、人工审核、RPA 自动下单、ERP 记账入库、FDE 数仓刷新指标的五步闭环,验证供应链自动化协同。

## 目录

- [项目概述](#项目概述)
- [技术栈](#技术栈)
- [项目架构](#项目架构)
- [目录结构](#目录结构)
- [核心文件说明](#核心文件说明)
- [快速启动](#快速启动)
- [配置说明](#配置说明)
- [API 接口](#api-接口)
- [部署](#部署)

## 项目概述

平台落地一个涵盖 RPA、ERP、FDE、AIGC 四大领域的小型可运行系统,验证技术协同:

- **AIGC**:基于 LLM 抽象层 + 规则引擎兜底,生成补货建议、4 段式经营日报,支持自然语言查询(Text2SQL 全链路 + 语义检索)
- **RPA**:供应商报价采集(多 Agent 博弈)、电商订单同步,适配器模式支持 mock/selenium 切换
- **ERP**:产品/供应商/采购/销售/库存/财务科目管理,移动加权平均成本核算
- **FDE**:ODS/DWD/DWS/ADS 四层数仓,ETL 流水线 + 数据质量监控 + 数据血缘 + 时序异常检测
- **闭环**:五步状态机编排,支持跨进程文件锁、超时处理、断点续跑、业务侧回滚补偿、异常自动触发

主要特性:
- JWT 认证 + RBAC 三级权限(admin/operator/viewer)+ Token 旋转 + 黑名单
- SSE 流式输出(AIGC 问答 + 闭环实时进度)
- Celery 异步任务(可选,未安装时优雅降级)
- Idempotency-Key 幂等写接口
- request_id 全链路追踪 + 审计日志
- 多渠道告警通知(钉钉/企业微信/邮件)
- Nginx TLS 终止 + 静态资源缓存 + 安全头
- PostgreSQL 定时备份 + 完整性校验 + 保留策略
- 健康检查端点(live/ready/deep)
- Prometheus 指标 + Swagger API 文档(可选)

### 进阶能力(改进 7-10)

- **多 Agent 采购博弈**:买方 Agent + 双供应商 Agent LLM 角色扮演,综合评分(价格 50% + 交期 30% + 评级 20%),<2 供应商时自动生成竞争对手
- **Data Agent Text2SQL 全链路**:NL→SQL 生成 → AST 4 层安全校验(单语句/SELECT-WITH/禁 DDL-DML/表白名单/强制 LIMIT 100)→ 真实执行 → NL 回复
- **FDE 时序异常检测**:7 日移动平均 + 2σ/3σ 分级,critical 异常自动触发闭环补货,warning+ 多渠道告警,Celery Beat 09:00-22:00 整点调度
- **LLM 经营日报**:4 段式(昨日回顾 + 趋势分析 + 风险提示 + 建议行动),读取近 7 天 ADS 趋势,Notifier 多渠道推送

## 技术栈

### 后端
- Flask(Flask-SQLAlchemy / Flask-Migrate / Flask-Caching / Flask-Limiter / Flask-Cors)
- PyJWT(认证)、Marshmallow(输入校验)、flask-smorest(OpenAPI 文档)
- Gunicorn(WSGI 服务器)、Celery(异步任务,可选)
- SQLAlchemy ORM + Alembic 迁移
- APScheduler(定时任务)

### 前端
- 原生 HTML / CSS / JavaScript(无构建步骤)
- Server-Sent Events(SSE)流式输出
- fetch + ReadableStream(流式读取)

### 数据与缓存
- PostgreSQL(生产)/ SQLite(开发默认)
- Redis(缓存 + 限流 + Celery broker,可选)

### 基础设施
- Docker + docker-compose(开发版 + 生产版)
- Nginx(反向代理 + TLS + 静态资源)
- Prometheus 指标采集(可选)
- pg_dump + cron(定时备份)

### 外部系统适配器
- LLM:规则引擎(默认)/ GLM / Qwen / OpenAI 兼容
- RPA:Mock(默认)/ Selenium 占位

## 项目架构

### 整体架构

```
┌──────────────────────────────────────────────────────────┐
│                      前端(浏览器)                          │
│  templates/index.html + static/js/app.js + static/css     │
└──────────────────────────┬───────────────────────────────┘
                           │ HTTP / SSE
┌──────────────────────────▼───────────────────────────────┐
│                    Nginx 反向代理                          │
│  TLS 终止 + 静态资源 + 限流 + 安全头 + gzip                │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│              Flask 应用(Gunicorn 多 worker)               │
│  ┌──────────┬──────────┬──────────┬──────────┬─────────┐  │
│  │ routes/  │ services/│ models/  │ adapters/│   ext   │  │
│  │ Blueprint│ 业务服务 │ 数据模型 │ 外部系统 │ db/cache│  │
│  └──────────┴──────────┴──────────┴──────────┴─────────┘  │
│  middleware(request_id) + schemas(输入校验)               │
└──────┬───────────────┬───────────────┬──────────────────┘
       │               │               │
       ▼               ▼               ▼
  ┌─────────┐    ┌─────────┐    ┌──────────────┐
  │PostgreSQL│   │  Redis  │    │   Celery     │
  │  主库    │   │缓存+限流│    │ 异步任务(可选)│
  └─────────┘    └─────────┘    └──────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  db-backup 容器  │
              │ cron + pg_dump   │
              └─────────────────┘
```

### 模块关系

- **routes/**:Blueprint 路由层,只做参数解析与调用 service,不含业务逻辑
- **services/**:业务服务层,封装 AIGC/ERP/RPA/FDE/闭环/RBAC/认证等核心能力
- **models/**:SQLAlchemy 数据模型,按业务域分文件
- **adapters/**:外部系统抽象层,把 mock 实现与生产实现解耦
- **extensions.py**:统一管理 db / cache / migrate / limiter / celery_app 实例
- **middleware.py**:request_id 注入与日志过滤器
- **schemas.py**:Marshmallow 输入校验 schema
- **tasks.py**:Celery 异步任务定义(含 _NullTask 兜底)

### 数据流向(五步闭环)

```
1. AIGC 生成补货建议 → Suggestion(pending)
2. 人工审核(approve/reject + final_qty) → SuggestionFeedback 自学习
3. RPA 向供应商系统下单(多 Agent 博弈报价)→ ExtSupplierQuote + PurchaseOrder(draft)
4. ERP 记账入库 → StockMove(in) + AccountMove(payable) + 移动加权平均成本
5. FDE 刷新 ADS 层 → AdsReplenishmentSuggest + AdsDailyOpsReport
```

每步带文件锁防并发、超时装饰器、失败回滚补偿(取消采购单/记审计待手工冲销/删 ADS 数据)。

**自动触发**:FDE 时序异常检测(critical 级别)→ `check_auto_trigger_with_anomaly` → 重置闭环并自动执行 step1 生成补货建议 + 审计日志。Celery Beat 09:00-22:00 整点调度。

## 目录结构

```
intelligent-ops-platform/
├── app.py                       # Flask 入口(工厂模式 + 优雅关闭)
├── config.py                    # 配置管理(DevConfig / ProdConfig)
├── extensions.py                # db / cache / migrate / limiter / celery_app
├── middleware.py                # request_id 注入 + 日志过滤器
├── schemas.py                   # Marshmallow 输入校验 schema
├── tasks.py                     # Celery 异步任务(含 _NullTask 兜底)
├── seed.py                      # 开发环境种子数据
├── gunicorn.conf.py             # Gunicorn 配置
│
├── adapters/                    # 外部系统适配器抽象层
│   ├── __init__.py              # 导出 get_rpa_backend / get_llm_backend
│   ├── rpa_backend.py           # RPABackend ABC + Mock + Selenium 占位
│   └── llm_backend.py           # LLMBackend ABC + Rule + GLM/Qwen/OpenAI
│
├── models/                      # 数据模型(按业务域分文件)
│   ├── __init__.py
│   ├── erp.py                   # Product/Supplier/PurchaseOrder/StockMove 等
│   ├── external.py              # ExtSupplierQuote/ExtEcommerceOrder
│   ├── warehouse.py             # ODS/DWD/DWS/ADS 四层数仓模型
│   ├── aigc.py                  # Suggestion/DailyReport/ChatHistory/Feedback
│   └── system.py                # User/Role/AuditLog/LoopState/TokenBlacklist
│
├── routes/                      # Blueprint 路由
│   ├── __init__.py              # 注册 Blueprint + 旧路径 308 重定向
│   ├── auth.py                  # 登录/刷新/登出/me/改密
│   ├── erp.py                   # 库存/订单/财务/退货/调拨
│   ├── rpa.py                   # 报价采集/订单同步/调度
│   ├── fde.py                   # ETL/分层统计/ADS/血缘/数据质量
│   ├── aigc.py                  # 建议/审核/日报/查询/SSE 流式
│   ├── loop.py                  # 闭环状态/执行/回滚/异步任务/SSE
│   └── audit.py                 # 审计日志查询/统计
│
├── services/                    # 业务服务层
│   ├── auth.py                  # JWT 签发/验证/黑名单/锁定
│   ├── rbac.py                  # 权限装饰器/角色初始化
│   ├── aigc_service.py          # LLM 抽象/建议/日报/Text2SQL/语义检索
│   ├── erp_service.py           # 库存/订单/财务/移动加权平均
│   ├── rpa_service.py           # 采集/同步/调度(接入多 Agent 博弈)
│   ├── warehouse_service.py     # ETL 流水线/血缘/数据质量
│   ├── closed_loop.py           # 五步状态机/文件锁/超时/回滚/异常触发
│   ├── multiagent.py            # 改进7:多 Agent 采购博弈(MultiAgentNegotiator)
│   ├── data_agent.py            # 改进8:Data Agent Text2SQL 全链路(AST 校验+执行)
│   ├── anomaly_detector.py      # 改进9:时序异常检测(7日 MA + 2σ/3σ 分级)
│   ├── notifier.py              # 多渠道告警(钉钉/微信/邮件)
│   ├── idempotency.py           # Idempotency-Key 幂等装饰器
│   └── password_policy.py       # 密码强度校验
│
├── migrations/                  # Alembic 数据库迁移
│   ├── alembic.ini
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 4e06934ab2d0_initial_schema.py
│
├── static/                      # 前端静态资源
│   ├── css/style.css
│   └── js/app.js
│
├── templates/
│   └── index.html               # 单页应用主模板
│
├── nginx/                       # Nginx 配置
│   ├── conf.d/app.conf          # TLS + 静态资源 + 限流 + 安全头
│   └── gen-self-signed-cert.sh  # 自签证书生成(本地预览用)
│
├── scripts/                     # 运维脚本
│   ├── Dockerfile.backup        # 备份容器镜像
│   ├── backup-cron-entrypoint.sh
│   ├── backup_db.sh             # pg_dump + 校验 + 保留清理
│   └── restore_db.sh            # 交互式恢复 + 行数校验
│
├── Dockerfile                   # 多阶段构建 + 非 root 用户
├── docker-compose.yml           # 开发版(web + db + redis)
├── docker-compose.prod.yml      # 生产版(+ nginx + db-backup)
├── .dockerignore
├── .gitignore
├── .env.example                 # 环境变量示例
└── requirements.txt
```

## 核心文件说明

### 入口与配置

- **[app.py](app.py)**:Flask 工厂模式入口。完成 ProxyFix 还原真实 IP、日志配置(带 request_id)、扩展初始化、Celery 配置、CORS、Prometheus、Blueprint 注册、健康检查端点、统一错误处理(不暴露内部异常)、RBAC 初始化、开发环境种子数据、SIGTERM/SIGINT 优雅关闭
- **[config.py](config.py)**:配置类。DevConfig(DEBUG=True,SQLite)/ ProdConfig(强制 SECRET_KEY + PostgreSQL + HTTPS Cookie)。所有配置通过环境变量注入,生产环境缺失关键变量会启动报错
- **[extensions.py](extensions.py)**:集中管理 db / cache / migrate / limiter / celery_app。Celery 未安装时 celery_app=None,所有依赖 Celery 的代码会优雅降级

### 核心业务逻辑

- **[services/closed_loop.py](services/closed_loop.py)**:五步闭环状态机。文件锁(fcntl + threading 兜底)、with_timeout 装饰器(multiprocessing + threading 兜底)、回滚补偿(取消采购单/记审计待手工冲销/删 ADS 数据)、Notifier 失败告警、`check_auto_trigger_with_anomaly` 异常自动触发
- **[services/aigc_service.py](services/aigc_service.py)**:LLM 抽象层 + 规则引擎兜底。补货建议生成(置信度算法)、4 段式经营日报(昨日回顾+趋势+风险+建议,含 7 天趋势 + Notifier 推送)、Text2SQL(优先 DataAgent,回退规则)、关键词语义检索、自然语言查询、审核反馈自学习
- **[services/multiagent.py](services/multiagent.py)**:改进7 多 Agent 采购博弈。`MultiAgentNegotiator` 三 Agent LLM 角色扮演(买方 + 双供应商),`score_quotes` 综合评分(价格 50% + 交期 30% + 评级 20%),<2 供应商时自动生成竞争对手,LLM 不可用走规则兜底
- **[services/data_agent.py](services/data_agent.py)**:改进8 Data Agent Text2SQL 全链路。`DataAgent.query()` NL→SQL→校验→执行→NL 回复,`_validate_sql_ast` 4 层安全校验(单语句/SELECT-WITH/禁 DDL-DML/表白名单/强制 LIMIT 100),CTE 别名追踪,sqlparse 不可用回退正则
- **[services/anomaly_detector.py](services/anomaly_detector.py)**:改进9 时序异常检测。`AnomalyDetector` 7 日 MA + 2σ/3σ 分级(critical/warning/info),`detect_and_trigger` critical→触发闭环 step1 + 审计日志,warning+→Notifier 多渠道告警
- **[services/auth.py](services/auth.py)**:JWT 签发(access 2h + refresh 7d,带 jti)、Token 黑名单、refresh 旋转、登录失败 5 次锁定、首次登录随机密码 + 强制改密
- **[services/rbac.py](services/rbac.py)**:权限装饰器 @require_permission。支持 TESTING 跳过、dev 无 token 放行、有 token 验证 JWT、RBAC_ENABLED 时校验权限。权限 JSON 解析异常显式记日志
- **[services/idempotency.py](services/idempotency.py)**:@idempotent 装饰器。基于 Idempotency-Key Header + Redis 缓存 24h,缺少 Header 放行兼容旧客户端

### 数据模型

- **[models/system.py](models/system.py)**:User/Role(多对多)/AuditLog/LoopState/TokenBlacklist
- **[models/erp.py](models/erp.py)**:Product/Supplier/Warehouse/PurchaseOrder/SaleOrder/StockMove/AccountMove/ReturnOrder
- **[models/warehouse.py](models/warehouse.py)**:ODS(贴源)/ DWD(明细)/ DWS(汇总)/ ADS(应用)四层,含 EtlMeta/DataQualityLog/DataLineage
- **[models/aigc.py](models/aigc.py)**:Suggestion/DailyReport/ChatHistory/SuggestionFeedback

### 关键组件

- **[middleware.py](middleware.py)**:request_id 注入(优先用客户端 X-Request-Id,否则生成 UUID 短格式)、响应头回写、日志过滤器让所有 handler 自动带 request_id
- **[schemas.py](schemas.py)**:Marshmallow 校验(LoginSchema/ChangePasswordSchema/CreateReturnSchema/ReviewSuggestionSchema 等)
- **[tasks.py](tasks.py)**:Celery 任务(闭环异步执行/ETL/RPA 同步/日报生成/异常检测)。_NullTask 让 Celery 不可用时仍可 import。Celery Beat 定时调度(ETL 02:00 / 日报 02:30 默认推送 / RPA 每 30min / 异常检测 09:00-22:00 整点)
- **[adapters/](adapters/)**:外部系统抽象。RPABackend(Mock + Selenium 占位)/ LLMBackend(Rule + GLM/Qwen/OpenAI 兼容)。通过 RPA_BACKEND / LLM_PROVIDER 环境变量切换
- **[services/notifier.py](services/notifier.py)**:多渠道告警。异步发送、单渠道失败不影响其他、无配置时降级为仅记日志

## 快速启动

### 本地开发

```bash
pip install -r requirements.txt
python app.py
# 访问 http://127.0.0.1:5000
```

默认使用 SQLite + 内存缓存,无需 Redis/PostgreSQL。开发环境会自动加载种子数据。

### Docker(生产版)

```bash
cp .env.example .env
# 编辑 .env,必须设置:SECRET_KEY、DB_PASSWORD、CORS_ORIGINS

docker compose -f docker-compose.prod.yml up -d
# 访问 https://localhost(自签证书需浏览器放行)
```

启动的服务:nginx(80/443)、web(5000)、db(PostgreSQL)、redis、db-backup(cron)。

### 数据库迁移

```bash
# 创建新迁移
flask db migrate -m "描述"

# 应用迁移
flask db upgrade
```

## 配置说明

完整环境变量见 [.env.example](.env.example),关键项:

| 变量 | 默认 | 说明 |
|------|------|------|
| FLASK_ENV | development | development / production |
| SECRET_KEY | dev-secret | 生产必填,生成方式 `python -c "import secrets; print(secrets.token_hex(32))"` |
| DATABASE_URL | sqlite:///ops_platform.db | 生产必须用 PostgreSQL |
| CACHE_TYPE | simple | simple / redis |
| CACHE_REDIS_URL | | Redis 连接(启用 redis 模式时必填) |
| RBAC_ENABLED | 0 | 0=dev 放行 / 1=强制权限校验(生产强制 1) |
| CORS_ORIGINS | * | 生产必须显式指定,缺失则启动报错 |
| LLM_PROVIDER | rule | rule / glm / qwen / openai |
| LLM_API_KEY | | LLM 密钥(留空走规则引擎) |
| RPA_BACKEND | mock | mock / selenium |
| LOOP_TIMEOUT | 60 | 闭环单步超时秒数 |
| METRICS_ENABLED | 0 | 1=启用 Prometheus /metrics |
| API_DOCS_ENABLED | 0 | 1=启用 Swagger /docs/swagger |
| BACKUP_CRON | 0 2 * * * | 备份 cron(默认每天 02:00) |
| BACKUP_RETENTION_DAYS | 7 | 备份保留天数 |
| ALERT_DINGTALK_WEBHOOK | | 钉钉告警 webhook |
| CELERY_BROKER_URL | | Celery broker(留空则禁用异步任务) |

## API 接口

所有 API 统一前缀 `/api/v1/`,旧路径 `/api/xxx` 自动 308 重定向到 v1。

### 认证(auth)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | /api/v1/auth/login | - | 登录,返回 access + refresh token |
| POST | /api/v1/auth/refresh | - | 刷新 token(旋转 + 旧 token 拉黑) |
| POST | /api/v1/auth/logout | - | 登出,吊销 token |
| GET | /api/v1/auth/me | erp:read | 当前用户信息 |
| POST | /api/v1/auth/change-password | - | 修改密码(走 password_policy 校验) |

### ERP(erp)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/erp/inventory | erp:read | 库存清单 |
| GET | /api/v1/erp/orders | erp:read | 近期单据(limit 上限 100) |
| GET | /api/v1/erp/account | erp:read | 财务概览 |
| GET | /api/v1/erp/warehouses | erp:read | 仓库列表 |
| POST | /api/v1/erp/returns | erp:write | 创建退货(@idempotent) |
| POST | /api/v1/erp/transfers | erp:write | 创建调拨(@idempotent) |

### RPA(rpa)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/rpa/quotes | rpa:read | 采集供应商报价 |
| POST | /api/v1/rpa/sync-orders | rpa:write | 同步电商订单 |
| GET | /api/v1/rpa/schedule/status | rpa:read | 调度状态 |
| POST | /api/v1/rpa/schedule/run | rpa:write | 立即执行调度任务 |

### FDE(fde)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | /api/v1/fde/run | fde:run | 执行 ETL 流水线 |
| GET | /api/v1/fde/stats | fde:read | 分层统计 |
| GET | /api/v1/fde/ads | fde:read | ADS 应用层数据 |
| GET | /api/v1/fde/lineage | fde:read | 数据血缘 |
| GET | /api/v1/fde/data-quality | fde:read | 数据质量报告 |
| GET | /api/v1/fde/anomalies | fde:read | 改进9:时序异常检测(支持 date/trigger 参数,trigger=true 触发闭环+告警) |

### AIGC(aigc)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/aigc/suggestions | aigc:read | 待审建议列表 |
| POST | /api/v1/aigc/generate-suggestions | aigc:review | 生成补货建议 |
| POST | /api/v1/aigc/review | aigc:review | 审核建议(支持 final_qty) |
| POST | /api/v1/aigc/batch-review | aigc:review | 批量审核 |
| GET | /api/v1/aigc/report | aigc:read | 最新日报 |
| POST | /api/v1/aigc/generate-report | fde:run | 改进10:生成 4 段式 LLM 日报(支持 push/date 参数,push=true 多渠道推送) |
| POST | /api/v1/aigc/query | aigc:read | 同步智能问答 |
| POST | /api/v1/aigc/query-stream | aigc:read | SSE 流式智能问答 |
| POST | /api/v1/aigc/data-query | aigc:read | 改进8:Data Agent Text2SQL 全链路(NL→SQL→执行→NL) |
| POST | /api/v1/aigc/negotiate | aigc:review | 改进7:多 Agent 采购博弈(买方+双供应商 LLM 角色扮演) |
| GET | /api/v1/aigc/chat-history/<session_id> | aigc:read | 对话历史 |
| GET | /api/v1/aigc/feedback-stats | aigc:read | 反馈统计 |

### 闭环(loop)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/loop/status | loop:read | 闭环状态 |
| POST | /api/v1/loop/run-step | loop:run | 同步执行步骤 |
| POST | /api/v1/loop/run-step-async | loop:run | 异步执行(需 Celery) |
| GET | /api/v1/loop/task/<task_id> | loop:read | 查询异步任务状态 |
| POST | /api/v1/loop/rollback | loop:rollback | 回滚指定步骤 |
| GET | /api/v1/loop/stream | loop:read | SSE 实时进度推送 |

### 审计(audit)

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | /api/v1/audit/logs | audit:read | 分页查询审计日志(支持 actor/action/target_type/日期过滤) |
| GET | /api/v1/audit/stats | audit:read | 最近 30 天按 action 聚合 |

### 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /health | 深度健康检查(数据库 + Redis + 磁盘) |
| GET | /health/live | 存活探针(轻量) |
| GET | /health/ready | 就绪探针(依赖检查) |
| GET | /metrics | Prometheus 指标(METRICS_ENABLED=1 时) |
| GET | /docs/swagger | API 文档(API_DOCS_ENABLED=1 时) |

## 部署

### 生产部署清单

1. 复制 `.env.example` 为 `.env`,设置以下必填项:
   - `SECRET_KEY`:`python -c "import secrets; print(secrets.token_hex(32))"`
   - `DB_PASSWORD`:PostgreSQL 密码
   - `CORS_ORIGINS`:允许的前端域名(逗号分隔)
2. (可选)配置 LLM:`LLM_PROVIDER=glm` + `LLM_API_KEY=xxx` + `LLM_API_URL=xxx`
3. (可选)配置告警:`ALERT_DINGTALK_WEBHOOK` / `ALERT_MAIL_*`
4. (可选)配置 Celery:`CELERY_BROKER_URL=redis://redis:6379/2`
5. TLS 证书:生产用 Let's Encrypt,本地预览用 `nginx/gen-self-signed-cert.sh`

```bash
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps       # 查看健康状态
docker compose -f docker-compose.prod.yml logs -f  # 查看日志
```

### 数据库备份恢复

```bash
# 手动备份(容器内 cron 默认每天 02:00 自动执行)
docker compose -f docker-compose.prod.yml exec db-backup /backup_db.sh

# 恢复(交互式确认 + 行数校验)
docker compose -f docker-compose.prod.yml exec db-backup /restore_db.sh /backups/ops_platform_20260629.sql.gz
```

备份文件:`pg_dump` + `gzip`,带 `gunzip -t` 完整性校验,默认保留 7 天。

### Celery Worker(可选)

```bash
celery -A tasks.celery_app worker --loglevel=info --concurrency=2
celery -A tasks.celery_app beat --loglevel=info
```

未启动 worker 时,异步接口 `/loop/run-step-async` 返回 503,同步接口仍可用。

### 健康检查

```bash
curl http://localhost/health/live   # 存活
curl http://localhost/health/ready  # 就绪
curl http://localhost/health        # 深度(含磁盘)
```
