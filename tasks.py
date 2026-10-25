"""Celery 异步任务定义

将长耗时操作（闭环执行、ETL、RPA 采集）异步化，避免阻塞 Gunicorn。
任务通过 Celery worker 执行，broker 用 Redis（生产）或内存（dev）。

启动 worker：
    celery -A tasks.celery_app worker --loglevel=info --concurrency=2

注意：Celery 未安装时，本模块的 task 装饰器退化为 no-op，相关 import 仍可用，
    但 .delay() 调用会抛 RuntimeError。路由层已捕获并返回 503。
"""

import logging
import threading

from flask import g

from config import config as default_config
from extensions import celery_app

logger = logging.getLogger(__name__)


# 当 celery_app 为 None（未安装 celery）时的空 task 装饰器，
# 让本模块仍可 import，避免 app 启动失败
class _NullTask:
    """Celery 不可用时的占位 task，调用 .delay() 抛 RuntimeError"""

    def __init__(self, name):
        self.name = name

    def delay(self, *args, **kwargs):
        raise RuntimeError(f"Celery 未启用，task {self.name} 无法异步执行")

    def apply_async(self, *args, **kwargs):
        raise RuntimeError(f"Celery 未启用，task {self.name} 无法异步执行")


def _task(name=None, **kwargs):
    """兼容 Celery 可用 / 不可用两种场景的 task 装饰器"""

    def decorator(f):
        if celery_app is None:
            return _NullTask(getattr(f, "__name__", "task"))
        if _TASK_BASE is not None:
            kwargs.setdefault("base", _TASK_BASE)
        return celery_app.task(name=name or f.__name__, **kwargs)(f)

    return decorator


# P0-13: 任务失败统一告警基类 —— 此前任务失败只写日志，夜间 ETL/日报挂掉无人知晓。
# on_failure 在重试耗尽后触发，通过 Notifier 多渠道推送（钉钉/企微/邮件，未配置时降级为日志）。
_TASK_BASE = None
if celery_app is not None:

    class _AlertTask(celery_app.Task):
        def on_failure(self, exc, task_id, args, kwargs, einfo):
            super().on_failure(exc, task_id, args, kwargs, einfo)
            try:
                from services.notifier import notifier

                notifier.send_alert(
                    title=f"Celery 任务失败: {self.name}",
                    message=f"task_id={task_id} error={exc}",
                    level="error",
                    context={"task": self.name, "task_id": task_id},
                )
            except Exception:
                logger.warning("任务失败告警发送异常 task=%s", self.name)

    _TASK_BASE = _AlertTask


# Flask app context 工厂（Celery worker 需要在 app context 内执行）
# P0-3 fix: 使用 threading.local() 替代全局变量，每个线程独立 push app context
# 原因：Flask app context 基于 thread-local storage，第一个线程 push 的 context
# 在其他线程中不可见，导致 "Working outside of application context" 错误
_app_ctx_local = threading.local()
_app_lock = threading.Lock()
_app_instance = None  # 共享 app 实例（创建一次），但 context 每线程独立

_TENANT_UNSET = object()


def _iter_active_tenants():
    """多租户：枚举全部活跃租户 id（sys_tenant 为全局表，查询不受租户过滤影响）。

    定时任务必须逐租户执行——worker 的 app context 里 g.tenant_id 未设置时
    会落到默认租户，若不遍历则租户 2+ 的数据永远不进数仓/不生成建议。
    """
    from extensions import db
    from models.tenant import DEFAULT_TENANT_ID, Tenant

    ids = [row[0] for row in db.session.query(Tenant.id).filter(Tenant.is_active.is_(True)).all()]
    return ids or [DEFAULT_TENANT_ID]


def _run_for_each_tenant(fn, label):
    """对每个活跃租户执行 fn(tid)，返回 {tenant_id: result|{"error": ...}}。

    失败语义：
    - 逐租户 try/except，单租户失败不中断其他租户；
    - 部分失败不 raise —— 整任务重试会重跑已成功租户（补货建议生成对重复执行
      有放大风险），单租户失败留待下一轮 beat 自然重试，并记录 error 日志；
    - 全部租户失败视为环境级故障，raise 触发 Celery retry 与 on_failure 告警。
    """
    results = {}
    failed = []
    for tid in _iter_active_tenants():
        g.tenant_id = tid
        try:
            results[tid] = fn()
        except Exception as exc:
            logger.exception("[celery] %s failed for tenant %s", label, tid)
            results[tid] = {"error": str(exc)}
            failed.append(tid)
    if failed and len(failed) == len(results):
        raise RuntimeError(f"{label} 在全部 {len(failed)} 个租户上失败，疑似环境故障")
    if failed:
        logger.warning("[celery] %s 部分租户失败（等待下一轮调度重试）: %s", label, failed)
    return results


def _run_as_tenant(tid, fn):
    """以指定租户执行 fn 并返回其结果；tid 为 None 时按默认租户。

    g.tenant_id 必须显式赋值（不能依赖默认）：worker 线程的 app context
    跨任务复用，上一个任务设置的租户会残留到下一个任务。
    执行后恢复原值，保证同 context 内多次调用互不泄漏。
    """
    from flask import g

    from models.tenant import DEFAULT_TENANT_ID

    saved = g.get("tenant_id", _TENANT_UNSET)
    g.tenant_id = DEFAULT_TENANT_ID if tid is None else tid
    try:
        return fn()
    finally:
        if saved is _TENANT_UNSET:
            g.pop("tenant_id", None)
        else:
            g.tenant_id = saved


def _get_app():
    """获取 Flask app context（线程局部，每个线程独立 push）

    P0-3 fix: Flask app context 基于 thread-local storage，无法跨线程共享。
    使用 threading.local() 确保每个 Celery worker 线程都有自己独立的 app context。
    app 实例本身只创建一次（双重检查锁定），但 context 每线程 push 一次。
    """
    global _app_instance
    ctx = getattr(_app_ctx_local, "ctx", None)
    if ctx is None:
        with _app_lock:
            if _app_instance is None:  # 双重检查：app 只创建一次
                from app import create_app

                _app_instance = create_app(default_config)
            # 每个线程独立 push context
            ctx = _app_instance.app_context()
            ctx.push()
            _app_ctx_local.ctx = ctx
    return _app_instance


# ==================== 闭环任务 ====================


@_task(name="tasks.run_loop_step_async", bind=True, max_retries=2, default_retry_delay=5)
def run_loop_step_async(self, step_no, actor="system", tenant_id=None):
    """异步执行闭环步骤（tenant_id=触发者租户，缺失时默认租户——见 _run_as_tenant）"""
    try:
        _get_app()
        from services.closed_loop import ClosedLoop

        result = _run_as_tenant(tenant_id, lambda: ClosedLoop.run_step(step_no, actor=actor))
        logger.info("[celery] loop step %s done: %s", step_no, result)
        return result
    except Exception as exc:
        logger.exception("[celery] loop step %s failed", step_no)
        if hasattr(self, "retry"):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise


@_task(name="tasks.run_etl_async", bind=True, max_retries=2, default_retry_delay=10)
def run_etl_async(self):
    """异步执行 FDE ETL 全量流水线（逐租户执行，租户间互不影响）"""
    try:
        _get_app()
        from services.warehouse_service import WarehouseService

        def _etl_one():
            # P0-13: raise_on_error=True —— 让异常上抛触发逐租户错误收集，
            # 全部租户失败时 _run_for_each_tenant raise 触发 self.retry 与 on_failure 告警
            return WarehouseService().run_full_pipeline(raise_on_error=True)

        results = _run_for_each_tenant(lambda tid: _etl_one(), "ETL")
        logger.info(
            "[celery] ETL done (per tenant): %s",
            {k: "ok" if not (isinstance(v, dict) and "error" in v) else "error" for k, v in results.items()},
        )
        return results
    except Exception as exc:
        logger.exception("[celery] ETL failed")
        if hasattr(self, "retry"):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise


@_task(name="tasks.run_rpa_scheduled_async", bind=True, max_retries=1)
def run_rpa_scheduled_async(self):
    """异步执行 RPA 定时任务（报价采集 + 订单同步，逐租户执行）"""
    try:
        _get_app()
        from services.rpa_service import RPAService

        results = _run_for_each_tenant(lambda tid: RPAService().run_scheduled_tasks(), "RPA scheduled")
        logger.info("[celery] RPA scheduled done (per tenant): %s", results)
        return results
    except Exception as exc:
        logger.exception("[celery] RPA scheduled failed")
        if hasattr(self, "retry"):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise


@_task(name="tasks.generate_suggestions_async", bind=True)
def generate_suggestions_async(self):
    """异步生成 AIGC 补货建议（逐租户执行）"""
    try:
        _get_app()
        from services.aigc_service import AIGCService

        def _gen_one():
            suggestions = AIGCService().generate_suggestions()
            return {"count": len(suggestions)}

        results = _run_for_each_tenant(_gen_one, "generate suggestions")
        total = sum(v.get("count", 0) for v in results.values() if isinstance(v, dict))
        logger.info("[celery] generated %s suggestions across %s tenants", total, len(results))
        return results
    except Exception as exc:
        logger.exception("[celery] generate suggestions failed")
        if hasattr(self, "retry"):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise


@_task(name="tasks.generate_daily_report_async", bind=True)
def generate_daily_report_async(self, dt_str=None, push=True):
    """改进10：异步生成 LLM 经营日报并多渠道推送。

    - 从 ADS 读取当日指标 + 近 7 天趋势
    - LLM 生成 4 段式日报（昨日回顾/趋势/风险/建议）
    - 通过 Notifier 推送钉钉/企微/邮件（push=True，定时任务默认开启）
    """
    try:
        _get_app()
        from datetime import datetime

        from services.aigc_service import AIGCService

        dt = datetime.strptime(dt_str, "%Y-%m-%d").date() if dt_str else None

        def _report_one():
            result = AIGCService().generate_daily_report(dt, push=push)
            return {"ok": result is not None, "pushed": push}

        results = _run_for_each_tenant(_report_one, "daily report")
        logger.info("[celery] daily report generated (per tenant): %s", results)
        return results
    except Exception as exc:
        logger.exception("[celery] generate daily report failed")
        if hasattr(self, "retry"):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise


@_task(name="tasks.detect_anomalies_async", bind=True, max_retries=1)
def detect_anomalies_async(self):
    """改进9：定时执行销售时序异常检测 + 触发闭环 + 发送告警。

    - 检测近 30 天 dws_sales_sku_daily 数据
    - critical 异常自动触发闭环补货
    - warning+ 异常发送多渠道告警（钉钉/企业微信/邮件）
    """
    try:
        _get_app()
        from services.anomaly_detector import AnomalyDetector

        def _detect_one():
            detector = AnomalyDetector()
            return detector.detect_and_trigger()

        results = _run_for_each_tenant(_detect_one, "anomaly detection")
        total = sum(r.get("summary", {}).get("total", 0) for r in results.values() if isinstance(r, dict))
        logger.info("[celery] anomaly detection done across %s tenants, anomalies=%d", len(results), total)
        return results
    except Exception as exc:
        logger.exception("[celery] anomaly detection failed")
        if hasattr(self, "retry"):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise


@_task(name="tasks.cleanup_token_blacklist_async", bind=True, max_retries=1)
def cleanup_token_blacklist_async(self):
    """P1-10: 清理已过期的 Token 黑名单记录，避免表无限增长。"""
    try:
        _get_app()
        from models.system import TokenBlacklist

        count = TokenBlacklist.cleanup_expired()
        logger.info("[celery] token blacklist cleanup done: removed=%d", count)
        return {"removed": count}
    except Exception:
        logger.exception("[celery] token blacklist cleanup failed")
        raise


# ==================== 定时任务（Celery Beat）====================
if celery_app is not None:
    # P0-7: beat_schedule 只接受 int/float/timedelta/crontab 对象，裸 cron 字符串
    # 会让 celery beat 启动即崩（AttributeError: 'str' object has no attribute 'app'）
    from celery.schedules import crontab

    celery_app.conf.beat_schedule = {
        # 每天凌晨 02:00 执行 ETL 全量刷新
        "daily-etl-full": {
            "task": "tasks.run_etl_async",
            "schedule": crontab(minute=0, hour=2),
        },
        # 每天凌晨 02:30 生成经营日报（ETL 完成后）
        "daily-report": {
            "task": "tasks.generate_daily_report_async",
            "schedule": crontab(minute=30, hour=2),
        },
        # 每 30 分钟同步电商订单（与 RPA_SYNC_CRON 默认值对齐）
        "rpa-sync-every-30min": {
            "task": "tasks.run_rpa_scheduled_async",
            "schedule": crontab(minute="*/30"),
        },
        # 改进9：每小时整点执行销售时序异常检测（09:00-22:00 业务时段）
        "hourly-anomaly-detect": {
            "task": "tasks.detect_anomalies_async",
            "schedule": crontab(minute=0, hour="9-22"),
        },
        # P1-10: 每天凌晨 03:00 清理过期的 Token 黑名单记录
        "daily-token-blacklist-cleanup": {
            "task": "tasks.cleanup_token_blacklist_async",
            "schedule": crontab(minute=0, hour=3),
        },
    }
    celery_app.conf.timezone = "Asia/Shanghai"
