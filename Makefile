# 中型企业智能运营平台 - Makefile
# 使用方式：make <target>

.PHONY: help install dev-install test test-cov lint format type-check security clean run docker-build docker-up docker-down migrate migrate-new

# Python 虚拟环境
PYTHON ?= python
VENV ?= .venv
PIP := $(VENV)/Scripts/pip.exe
PY := $(VENV)/Scripts/python.exe

# 默认目标
help: ## 显示所有可用命令
	@echo "可用命令："
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ==================== 依赖管理 ====================

install: ## 安装生产依赖
	$(PIP) install -r requirements.txt

dev-install: ## 安装开发依赖（含测试/格式化/类型检查）
	$(PIP) install -r requirements.txt
	$(PIP) install -r requirements-dev.txt
	pre-commit install

# ==================== 测试 ====================

test: ## 运行测试
	$(PY) -m pytest tests/ -v

test-cov: ## 运行测试并生成覆盖率报告
	$(PY) -m pytest tests/ --cov=. --cov-report=term-missing --cov-report=html --cov-report=xml -v

test-fast: ## 快速运行测试（跳过慢测试）
	$(PY) -m pytest tests/ -v -m "not slow"

# ==================== 代码质量 ====================

lint: ## Ruff 代码检查
	$(PY) -m ruff check .

lint-fix: ## Ruff 自动修复
	$(PY) -m ruff check . --fix

format: ## Black 格式化
	$(PY) -m black .

format-check: ## Black 格式检查（不修改）
	$(PY) -m black --check .

type-check: ## Mypy 类型检查
	$(PY) -m mypy --ignore-missing-imports --no-strict-optional app.py config.py extensions.py

security: ## Bandit 安全扫描
	$(PY) -m bandit -r . -x tests,.venv,migrations -ll

pre-commit: ## 运行 pre-commit 钩子
	pre-commit run --all-files

# ==================== 运行 ====================

run: ## 启动开发服务器
	$(PY) app.py

run-prod: ## 启动生产 Gunicorn（Linux）
	gunicorn -c gunicorn.conf.py wsgi:app

# ==================== Docker ====================

docker-build: ## 构建 Docker 镜像
	docker build -t intelligent-ops-platform:latest .

docker-up: ## 启动开发版 Docker Compose
	docker compose up -d

docker-up-prod: ## 启动生产版 Docker Compose
	docker compose -f docker-compose.prod.yml up -d

docker-down: ## 停止 Docker Compose
	docker compose down
	docker compose -f docker-compose.prod.yml down

docker-logs: ## 查看 Docker 日志
	docker compose logs -f

# ==================== 数据库 ====================

migrate: ## 应用数据库迁移
	$(PY) -m flask db upgrade

migrate-new: ## 创建新迁移（用法：make migrate-new m="描述"）
	$(PY) -m flask db migrate -m "$(m)"

migrate-rollback: ## 回滚上一个迁移
	$(PY) -m flask db downgrade

# ==================== 清理 ====================

clean: ## 清理生成文件
	@echo "清理生成文件..."
	-rm -rf .pytest_cache
	-rm -rf .coverage
	-rm -rf htmlcov
	-rm -rf coverage.xml
	-rm -rf .mypy_cache
	-rm -rf .ruff_cache
	-find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	-find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "清理完成"

clean-db: ## 清理开发数据库（慎用）
	-rm -f ops_platform.db
	-rm -f instance/*.db
	@echo "开发数据库已清理"

# ==================== Celery ====================

celery-worker: ## 启动 Celery Worker
	celery -A tasks.celery_app worker --loglevel=info --concurrency=2

celery-beat: ## 启动 Celery Beat 调度器
	celery -A tasks.celery_app beat --loglevel=info
