# 语法说明：使用多阶段构建（BuildKit frontend-stage + python runtime）
# 前端：React + Vite + Tailwind（frontend/），构建产物输出 static/dist + templates/index.html

# ==================== Stage 1: 前端构建（React/Vite）====================
FROM node:20-alpine AS frontend-build

WORKDIR /build

# 先装依赖（利用层缓存：lockfile 不变则跳过安装）
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile

# 源码 + 构建（Vite 产物自带内容 hash 文件名）
COPY frontend/ ./
RUN pnpm run build:flask

# ==================== Stage 2: Python 运行时 ====================
FROM python:3.12-slim AS base

WORKDIR /app

# 安装系统依赖（PostgreSQL 客户端库等）+ 清理 apt 缓存减小镜像
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 layer 缓存）
COPY requirements.txt .
# P0-fix: 升级 pip 避免旧版 resolver 误报冲突 + 打印 requirements 便于排错 + 关闭进度条
# setuptools>=78.1.1: CVE-2025-47273（PackageIndex 路径穿越），基础镜像自带 70.3.0 需显式升级
RUN python -m pip install --upgrade pip && \
    pip install --upgrade "setuptools>=78.1.1" wheel && \
    echo "===== requirements.txt =====" && cat requirements.txt && \
    pip install --no-cache-dir --progress-bar off -r requirements.txt

# 复制业务代码
COPY . .

# 用 Stage 1 的 Vite 构建产物覆盖（保证镜像内前端与源码一致）
COPY --from=frontend-build /build/static/dist/ /app/static/dist/
COPY --from=frontend-build /build/templates/index.html /app/templates/index.html

# 创建非 root 用户并切换
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser \
    && mkdir -p /app/instance/logs \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

ENV FLASK_ENV=production
ENV FLASK_DEBUG=0
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:5000/health/live || exit 1

# P0: 使用 gunicorn.conf.py 统一配置（修复 Dockerfile 命令行参数与配置文件不一致问题）
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
