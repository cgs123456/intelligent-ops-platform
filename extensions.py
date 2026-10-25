"""Flask 扩展实例（避免循环依赖）

所有扩展在此实例化，app 初始化时调用 init_app 绑定。
"""

import os

from flask import g, has_app_context
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.orm import with_loader_criteria

db = SQLAlchemy()
migrate = Migrate()
cache = Cache()
# P2-14: 全局默认限流 200/hour（测试环境可通过 config 覆盖）
limiter = Limiter(key_func=get_remote_address, default_limits=["200/hour"])

# ==== 多租户：ORM 层强制隔离（models/tenant.py 定义 TenantScoped）====
_UNSET = object()


def _current_tenant_id():
    """当前租户：g.tenant_id 未注入时默认租户；None 表示平台管理员（跨租户，不过滤）。"""
    if has_app_context():
        tid = g.get("tenant_id", _UNSET)
        if tid is _UNSET:
            from models.tenant import DEFAULT_TENANT_ID

            return DEFAULT_TENANT_ID
        return tid
    # 无 app context（脚本/Celery 内部路径）：调用方应显式传入租户，
    # 这里兜底默认租户，保证单租户行为不变
    from models.tenant import DEFAULT_TENANT_ID

    return DEFAULT_TENANT_ID


# 公开别名：绕过 ORM 自动过滤的原生 SQL/Core 路径必须显式取当前租户
current_tenant_id = _current_tenant_id


@event.listens_for(db.Session, "do_orm_execute")
def _tenant_auto_filter(execute_state):
    """所有 ORM select/update/delete 自动追加 tenant_id 过滤。

    with_loader_criteria 同时覆盖 relationship 懒加载与别名查询；
    平台管理员（g.tenant_id is None）跳过。
    """
    if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
        return
    from models.tenant import TenantScoped

    tid = _current_tenant_id()
    if tid is None:
        return
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(TenantScoped, lambda cls: cls.tenant_id == tid, include_aliases=True)
    )


@event.listens_for(db.Session, "before_flush")
def _tenant_fill_on_insert(session, flush_context, instances):
    """未显式指定租户的新对象按当前租户落库（无上下文时默认租户）。"""
    from models.tenant import DEFAULT_TENANT_ID, TenantScoped

    tid = _current_tenant_id()
    fill = DEFAULT_TENANT_ID if tid is None else tid
    for obj in session.new:
        if isinstance(obj, TenantScoped) and obj.tenant_id is None:
            obj.tenant_id = fill


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
