"""Celery 异步任务定义

将长耗时操作（闭环执行、ETL、RPA 采集）异步化，避免阻塞 Gunicorn。
任务通过 Celery worker 执行，broker 用 Redis（生产）或内存（dev）。

启动 worker：
    celery -A tasks.celery_app worker --loglevel=info --concurrency=2

注意：Celery 未安装时，本模块的 task 装饰器退化为 no-op，相关 import 仍可用，
    但 .delay() 调用会抛 RuntimeError。路由层已捕获并返回 503。
"""

import logging
import os

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
        return celery_app.task(name=name or f.__name__, **kwargs)(f)

    return decorator


# Flask app context 工厂（Celery worker 需要在 app context 内执行）
_app_ctx = None


def _get_app():
    """获取 Flask app 实例（延迟初始化，仅在 worker 中首次调用时创建）"""
    global _app_ctx
    if _app_ctx is None:
        from app import create_app

        app = create_app(default_config)
        _app_ctx = app.app_context()
        _app_ctx.push()
    return _app_ctx


# ==================== 闭环任务 ====================


@_task(name="tasks.run_loop_step_async", bind=True, max_retries=2, default_retry_delay=5)
def run_loop_step_async(self, step_no, actor="system"):
    """异步执行闭环步骤"""
    try:
        _get_app()
        from services.closed_loop import ClosedLoop

        result = ClosedLoop.run_step(step_no, actor=actor)
        logger.info("[celery] loop step %s done: %s", step_no, result)
        return result
    except Exception as exc:
        logger.exception("[celery] loop step %s failed", step_no)
        if hasattr(self, "retry"):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise


@_task(name="tasks.run_etl_async", bind=True, max_retries=2, default_retry_delay=10)
def run_etl_async(self):
    """异步执行 FDE ETL 全量流水线"""
    try:
        _get_app()
        from services.warehouse_service import WarehouseService

        result = WarehouseService().run_full_pipeline()
        logger.info("[celery] ETL done: %s", result)
        return result
    except Exception as exc:
        logger.exception("[celery] ETL failed")
        if hasattr(self, "retry"):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise


@_task(name="tasks.run_rpa_scheduled_async", bind=True, max_retries=1)
def run_rpa_scheduled_async(self):
    """异步执行 RPA 定时任务（报价采集 + 订单同步）"""
    try:
        _get_app()
        from services.rpa_service import RPAService

        result = RPAService().run_scheduled_tasks()
        logger.info("[celery] RPA scheduled done: %s", result)
        return result
    except Exception as exc:
        logger.exception("[celery] RPA scheduled failed")
        if hasattr(self, "retry"):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise


@_task(name="tasks.generate_suggestions_async", bind=True)
def generate_suggestions_async(self):
    """异步生成 AIGC 补货建议"""
    try:
        _get_app()
        from services.aigc_service import AIGCService

        suggestions = AIGCService().generate_suggestions()
        logger.info("[celery] generated %s suggestions", len(suggestions))
        return {"count": len(suggestions)}
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
        result = AIGCService().generate_daily_report(dt, push=push)
        logger.info("[celery] daily report generated, pushed=%s", push)
        return {"ok": result is not None, "pushed": push}
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

        detector = AnomalyDetector()
        result = detector.detect_and_trigger()
        logger.info(
            "[celery] anomaly detection done: checked=%d anomalies=%d triggered=%d",
            result.get("checked", 0),
            result.get("summary", {}).get("total", 0),
            len(result.get("triggered_actions", [])),
        )
        return result
    except Exception as exc:
        logger.exception("[celery] anomaly detection failed")
        if hasattr(self, "retry"):
            raise self.retry(exc=exc, countdown=2**self.request.retries)
        raise


# ==================== 定时任务（Celery Beat）====================
if celery_app is not None:
    celery_app.conf.beat_schedule = {
        # 每天凌晨 02:00 执行 ETL 全量刷新
        "daily-etl-full": {
            "task": "tasks.run_etl_async",
            "schedule": "0 2 * * *",
        },
        # 每天凌晨 02:30 生成经营日报（ETL 完成后）
        "daily-report": {
            "task": "tasks.generate_daily_report_async",
            "schedule": "30 2 * * *",
        },
        # 每 30 分钟同步电商订单（与 RPA_SYNC_CRON 默认值对齐）
        "rpa-sync-every-30min": {
            "task": "tasks.run_rpa_scheduled_async",
            "schedule": "*/30 * * * *",
        },
        # 改进9：每小时整点执行销售时序异常检测（09:00-22:00 业务时段）
        "hourly-anomaly-detect": {
            "task": "tasks.detect_anomalies_async",
            "schedule": "0 9-22 * * *",
        },
    }
    celery_app.conf.timezone = "Asia/Shanghai"
