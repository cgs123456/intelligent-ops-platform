"""Flask 扩展实例（避免循环依赖）

所有扩展在此实例化，app 初始化时调用 init_app 绑定。
"""

import os

from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
cache = Cache()
# P2-14: 全局默认限流 200/hour（测试环境可通过 config 覆盖）
limiter = Limiter(key_func=get_remote_address, default_limits=["200/hour"])

# Celery 实例（可选，未配置 broker 时保持 None，不影响主应用启动）
# P0-1 修复：broker 必须从环境变量读取。此前硬编码 memory://，而 celery -A tasks.celery_app
# 启动的 worker/beat 不会 import app.py（唯一覆盖 broker_url 的地方），且 Celery 官方
# 不自动读 CELERY_BROKER_URL 环境变量 —— 生产环境定时任务/异步任务曾全部静默失效。
try:
    from celery import Celery

    _celery_broker = os.getenv("CELERY_BROKER_URL", "memory://")
    _celery_backend = os.getenv("CELERY_RESULT_BACKEND", "cache+memory://")
    celery_app = Celery(
        "ops_platform",
        broker=_celery_broker,
        backend=_celery_backend,
    )
    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="Asia/Shanghai",
        enable_utc=False,
        task_acks_late=True,  # 任务执行完才 ack，崩溃时任务重投
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,  # 一次只预取一个任务，避免长任务阻塞
        # P0/M4: 任务超时上限 —— ETL 死循环/慢查询时不设限会永久占用 worker
        task_soft_time_limit=int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "1800")),
        task_time_limit=int(os.getenv("CELERY_TASK_TIME_LIMIT", "3600")),
        broker_connection_retry_on_startup=True,
    )
except ImportError:
    celery_app = None

# P0-1: 生产环境 broker 快速失败 —— ProdConfig 有校验，但 worker/beat 进程可能绕过
# create_app 启动，这里在扩展层再兜一道，避免 memory:// 静默吞掉所有定时任务。
if celery_app is not None and _celery_broker.startswith("memory://") and os.getenv("FLASK_ENV") == "production":
    raise RuntimeError("生产环境禁止使用 memory:// Celery broker：请设置 CELERY_BROKER_URL（如 redis://redis:6379/2）")
