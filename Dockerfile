# 语法说明：使用多阶段构建的 BuildKit 语法（向后兼容）
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
