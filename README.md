# 中型企业智能运营平台

> RPA / ERP / FDE / AIGC 四层闭环协同的可运行原型:AIGC 生成补货建议 → 人工审核 → RPA 自动下单 → ERP 记账入库 → FDE 数仓刷新指标。

[![CI](https://github.com/cgs123456/intelligent-ops-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/cgs123456/intelligent-ops-platform/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-informational.svg)](LICENSE)
[![Python 3.10-3.12](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](pyproject.toml)

```
┌────────────────────────────────────────────┐
│        前端 React SPA(frontend/ 源码)      │
└──────────────────┬─────────────────────────┘
                   │ HTTP / SSE
┌──────────────────▼─────────────────────────┐
│         Nginx(TLS + 静态资源 + 限流)        │
└──────────────────┬─────────────────────────┘
┌──────────────────▼─────────────────────────┐
│        Flask(Gunicorn 多 worker)           │
│  routes/ → services/ → models/             │
│  adapters/(LLM/RPA 抽象) + tasks/(Celery)  │
└───────┬──────────────┬─────────────┬───────┘
        ▼              ▼             ▼
   PostgreSQL        Redis        Celery(可选)
   主库+备份      缓存+限流+broker  异步任务+Beat
```

五步闭环数据流:AIGC 补货建议(Suggestion) → 人工审核(approve/reject + final_qty) → RPA 多 Agent 博弈下单(PurchaseOrder) → ERP 记账入库(StockMove + AccountMove,移动加权平均成本) → FDE 刷新 ADS 指标。每步带文件锁、超时控制、失败回滚补偿;FDE 异常检测(critical)可自动重触发闭环。

## 目录

- [快速开始](#快速开始)
- [功能特性](#功能特性)
- [技术栈](#技术栈)
- [配置](#配置)
- [API](#api)
- [工程化](#工程化)
- [部署](#部署)
- [项目结构](#项目结构)
- [文档](#文档)
- [贡献](#贡献)
- [许可证](#许可证)

## 快速开始

本地开发默认 SQLite + 内存缓存,零外部依赖:

```bash
git clone https://github.com/cgs123456/intelligent-ops-platform.git
cd intelligent-ops-platform
pip install -r requirements.txt
python app.py                      # http://127.0.0.1:5000
```

开发环境自动建表并写入种子数据(6 SKU / 3 供应商 / 7 天电商订单)。管理员账号 `admin`,初始随机密码打印在启动日志(stderr)中,首次登录强制改密。

Docker 生产版:

```bash
cp .env.example .env               # 必填项见下方[配置]
docker compose -f docker-compose.prod.yml up -d
```

启动 nginx(80/443) + web + PostgreSQL + Redis + db-backup(cron)。

前端为 React SPA,构建产物已入库(`static/dist/` + `templates/index.html`),开箱即用;修改前端源码后重新构建(需 Node ≥ 20 + pnpm 12):

```bash
cd frontend && pnpm install && pnpm run build:flask
```

## 功能特性

- **AIGC**:LLM 抽象层(GLM/Qwen/OpenAI 兼容)+ 规则引擎兜底;补货建议(置信度 + 高金额分级审批)、4 段式经营日报、自然语言数仓查询(Text2SQL 全链路 + 问答对 RAG 沉淀)
- **RPA**:供应商报价采集、电商订单同步;多 Agent 采购博弈(买方 + 双供应商 LLM 角色扮演,价格/交期/评级综合评分);mock/selenium 适配器切换
- **ERP**:采购/销售/退货/调拨/盘点全链路,移动加权平均成本;统计安全库存(SS=z×σ×√LT)、SKU ABC/XYZ 分级、会计期间锁定、库位管理、科目试算平衡
- **FDE**:ODS/DWD/DWS/ADS 四层数仓,幂等 upsert ETL;数据质量校验、数据血缘、时序异常检测(7 日 MA + 2σ/3σ,critical 自动触发闭环)
- **闭环编排**:五步状态机(节点函数单步执行 + 条件路由),文件锁防并发、超时装饰器、断点续跑、回滚补偿、SSE 实时进度
- **平台能力**:JWT + RBAC 三级角色(admin/operator/viewer)+ TOTP MFA;多租户隔离(共库共表 + tenant_id 自动过滤);Idempotency-Key 幂等写;审计日志 hash chain 防篡改;request_id 全链路追踪;钉钉/企微/邮件多渠道告警;Prometheus 指标;限流 + 安全头
- **MRP / 财务对账**:BOM 多层展开净需求计算;对账批次(精确/税容差/多对一组合匹配 + 人工调节项)

## 技术栈

- **后端**:Flask + SQLAlchemy 2.x + Alembic + Marshmallow;PyJWT;sqlglot(SQL AST 安全校验);Celery(可选,未安装时优雅降级);Gunicorn
- **前端**:React 18 + Vite + TypeScript + Tailwind v4 + shadcn/ui + TanStack Query + Zustand + Recharts
- **数据**:PostgreSQL(生产)/ SQLite(开发);Redis(缓存 + 限流 + broker,可选)
- **基础设施**:Docker Compose(开发/生产/监控三套);Nginx TLS;Prometheus + Grafana + Loki 监控栈(可选);GitHub Actions CI

## 配置

完整清单见 [.env.example](.env.example)。关键项:

| 变量 | 默认 | 说明 |
|------|------|------|
| FLASK_ENV | development | development / production |
| SECRET_KEY | dev-secret | **生产必填**(≥32 字符):`python -c "import secrets; print(secrets.token_hex(32))"` |
| FIELD_ENCRYPTION_KEY | | **生产必填** 字段级加密密钥:`python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"` |
| DATABASE_URL | sqlite:///ops_platform.db | 生产必须 PostgreSQL |
| CORS_ORIGINS | localhost | **生产必填**(不可为 `*`) |
| CELERY_BROKER_URL | memory:// | **生产必填**(不可 memory://,如 `redis://redis:6379/2`) |
| RATELIMIT_STORAGE_URI | memory:// | **生产必填**(不可 memory://) |
| RBAC_ENABLED | 0 | 0=dev 放行 / 1=强制权限校验 |
| LLM_PROVIDER / LLM_API_KEY | rule | rule / glm / qwen / openai;留空走规则引擎 |
| RPA_BACKEND | mock | mock / selenium |
| LOOP_TIMEOUT | 60 | 闭环单步超时秒数 |
| METRICS_ENABLED / API_DOCS_ENABLED | 0 | 1=启用 Prometheus /metrics、Swagger /docs/swagger |
| ALERT_DINGTALK_WEBHOOK | | 钉钉告警 webhook(可选) |

## API

所有接口统一前缀 `/api/v1/`,JWT 认证,响应统一 envelope。完整清单(90+ 端点、权限矩阵)见 [docs/API.md](docs/API.md)。

```bash
# 登录(token 走 HttpOnly Cookie)
curl -X POST http://127.0.0.1:5000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "<启动日志中的初始密码>"}'

# 带认证查询库存
curl http://127.0.0.1:5000/api/v1/erp/inventory -b cookies.txt
```

## 工程化

CI(push/PR 到 main 自动触发,7 个 job 全绿才可合并):

| Job | 内容 |
|-----|------|
| lint | ruff + black + mypy |
| test | Python 3.10 / 3.11 / 3.12 矩阵 pytest + coverage |
| security | bandit + pip-audit(CVE 阻断) |
| migrate | PostgreSQL 16 空库跑完整迁移链,断言 head |
| frontend | pnpm install + 前端构建 |
| build | Docker 镜像多阶段构建 + Trivy 扫描(HIGH/CRITICAL 阻断) |

本地开发:

```bash
pip install -r requirements-dev.txt
pytest                           # 581 用例全量回归
pre-commit run --all-files       # ruff + black + mypy 提交前检查
```

数据库迁移(链:b00000000001_baseline → …_business_enhancements → …_stock_location → …_chat_history_rag):

```bash
flask db upgrade                 # 应用迁移
flask db migrate -m "描述"        # 创建新迁移
```

## 部署

生产部署清单与运维手册见 [docs/deployment.md](docs/deployment.md) 与 [docs/ops-runbook.md](docs/ops-runbook.md)。要点:

```bash
# 1. 配置 .env(生产必填项见上方[配置]节)
# 2. 起服务
docker compose -f docker-compose.prod.yml up -d
# 3. 备份(容器内 cron 默认每天 02:00 自动执行)
docker compose -f docker-compose.prod.yml exec db-backup /backup_db.sh
# 4. 恢复(交互式确认 + 行数校验)
docker compose -f docker-compose.prod.yml exec db-backup /restore_db.sh /backups/<file>.sql.gz
```

Celery(可选,未启动时异步接口返回 503、同步接口仍可用):

```bash
celery -A tasks.celery_app worker --loglevel=info --concurrency=2
celery -A tasks.celery_app beat --loglevel=info    # ETL 02:00 / 日报 02:30 / RPA 每 30min / 异常检测 09:00-22:00 整点
```

监控栈(可选):`docker compose -f docker-compose.monitoring.yml up -d` 启动 Prometheus + Grafana(预置仪表盘)+ Loki + Alertmanager。

## 项目结构

<details>
<summary>展开目录树</summary>

```
intelligent-ops-platform/
├── app.py                  # Flask 工厂入口(优雅关闭)
├── config.py               # DevConfig / ProdConfig(生产安全校验)
├── extensions.py           # db / cache / limiter / celery + 多租户过滤
├── tasks.py                # Celery 任务(含 _NullTask 兜底 + 逐租户执行)
├── seed.py                 # 开发种子数据
├── routes/                 # Blueprint 路由(auth/erp/rpa/fde/aigc/loop/audit/finance/mrp/tenant)
├── services/               # 业务服务层(闭环/AIGC/ERP/RPA/FDE/对账/MRP/RBAC/...)
├── models/                 # SQLAlchemy 模型(按业务域分文件 + TenantScoped mixin)
├── adapters/               # LLM / RPA 外部系统抽象(mock 与生产实现解耦)
├── frontend/               # React SPA 源码(pnpm 构建)
├── static/dist/            # 前端构建产物(入库,部署链免 Node)
├── templates/index.html    # 由 post-build 从构建产物生成(勿手改)
├── migrations/versions/    # Alembic 迁移(b00000000001~b00000000004)
├── tests/                  # pytest 测试套件(581 用例)
├── nginx/                  # Nginx TLS + 限流 + 安全头配置
├── scripts/                # 备份/恢复脚本
├── monitoring/             # Prometheus / Grafana / Loki / Alertmanager 配置
├── deploy/helm/            # Helm chart(K8s 部署)
├── docker-compose.yml      # 开发版(web + db + redis)
├── docker-compose.prod.yml # 生产版(+ nginx + db-backup)
└── .github/workflows/      # CI(ci.yml) / CD(cd.yml,需配置 SSH secrets 后启用)
```

</details>

## 文档

- [docs/API.md](docs/API.md) — 全量 API 清单与权限矩阵
- [docs/architecture.md](docs/architecture.md) — 架构设计
- [docs/MULTI_TENANCY.md](docs/MULTI_TENANCY.md) — 多租户模型与隔离语义
- [docs/deployment.md](docs/deployment.md) — 部署手册
- [docs/ops-runbook.md](docs/ops-runbook.md) — 运维手册
- [docs/backup_recovery.md](docs/backup_recovery.md) — 备份恢复

## 贡献

欢迎 issue 与 PR;提交前请跑 `pytest` 与 `pre-commit run --all-files`。

## 许可证

[MIT](LICENSE)
