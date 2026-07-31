# 语法说明：使用多阶段构建（BuildKit frontend-stage + python runtime）
# P0-11: 添加前端构建阶段，使用 terser/clean-css 压缩 JS/CSS

# ==================== Stage 1: 前端构建（JS/CSS 压缩）====================
FROM node:20-alpine AS frontend-build

WORKDIR /build

# 复制静态资源源文件
COPY static/ ./static/

# 安装压缩工具并执行压缩
RUN npm install -g terser clean-css-cli && \
    # 压缩 JS：移除注释、空白；保留语义（不破坏可选链等 ES2020 语法）
    terser static/js/app.js \
        --compress --mangle \
        --output static/js/app.min.js && \
    # 压缩 CSS
    cleancss -o static/css/style.min.css static/css/style.css && \
    # 用压缩版替换原文件（Flask url_for 仍引用原文件名）
    mv static/js/app.min.js static/js/app.js && \
    mv static/css/style.min.css static/css/style.css

# ==================== Stage 2: Python 运行时 ====================
FROM python:3.12-slim AS base

WORKDIR /app

# 安装系统依赖（PostgreSQL 客户端库等）+ 清理 apt 缓存减小镜像
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用 layer 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制业务代码
COPY . .

# 用 Stage 1 的压缩版静态文件覆盖原始文件
COPY --from=frontend-build /build/static/js/app.js /app/static/js/app.js
COPY --from=frontend-build /build/static/css/style.css /app/static/css/style.css

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
