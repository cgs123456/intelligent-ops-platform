"""Flask 扩展实例（避免循环依赖）

所有扩展在此实例化，app 初始化时调用 init_app 绑定。
"""
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
migrate = Migrate()
cache = Cache()
limiter = Limiter(key_func=get_remote_address, default_limits=[])

# Celery 实例（可选，未配置 broker 时保持 None，不影响主应用启动）
try:
    from celery import Celery
    celery_app = Celery(
        'ops_platform',
        broker='memory://',  # 默认内存（dev），生产用 redis://redis:6379/2
        backend='cache+memory://',
    )
    celery_app.conf.update(
        task_serializer='json',
        result_serializer='json',
        accept_content=['json'],
        timezone='Asia/Shanghai',
        enable_utc=False,
        task_acks_late=True,  # 任务执行完才 ack，崩溃时任务重投
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,  # 一次只预取一个任务，避免长任务阻塞
    )
except ImportError:
    celery_app = None
