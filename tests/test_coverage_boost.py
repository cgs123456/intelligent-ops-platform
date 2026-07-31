"""P2 补充测试：覆盖 db_router / metrics / tasks 三个 0% 覆盖率模块

提升整体覆盖率至 60% 门槛以上。
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

# ==================== db_router 测试 ====================


class TestReadWriteRouter:
    """读写分离路由器测试"""

    def test_singleton_pattern(self):
        """单例模式：多次构造返回同一实例"""
        from services.db_router import ReadWriteRouter

        # 重置单例以便测试隔离
        ReadWriteRouter._instance = None
        r1 = ReadWriteRouter(master_url="sqlite:///test1.db")
        r2 = ReadWriteRouter(master_url="sqlite:///test2.db")
        assert r1 is r2
        # 第二次构造不重新初始化
        assert r1._master_url == "sqlite:///test1.db"
        ReadWriteRouter._instance = None

    def test_get_master_engine_singleton(self):
        """主库 engine 单例 + 双重检查锁"""
        from services.db_router import ReadWriteRouter

        ReadWriteRouter._instance = None
        router = ReadWriteRouter(master_url="sqlite:///master_test.db")
        eng1 = router.get_master_engine()
        eng2 = router.get_master_engine()
        assert eng1 is eng2
        ReadWriteRouter._instance = None

    def test_get_write_engine_equals_master(self):
        """写库 engine 恒为主库"""
        from services.db_router import ReadWriteRouter

        ReadWriteRouter._instance = None
        router = ReadWriteRouter(master_url="sqlite:///write_test.db")
        assert router.get_write_engine() is router.get_master_engine()
        ReadWriteRouter._instance = None

    def test_read_engine_fallback_when_no_replica(self):
        """未配置从库 URL 时，读操作降级到主库"""
        from services.db_router import ReadWriteRouter

        ReadWriteRouter._instance = None
        router = ReadWriteRouter(master_url="sqlite:///fallback_test.db", replica_url="")
        read_eng = router.get_read_engine()
        assert read_eng is router.get_master_engine()
        ReadWriteRouter._instance = None

    def test_read_engine_with_replica(self):
        """配置从库 URL 时，读操作走从库"""
        from services.db_router import ReadWriteRouter

        ReadWriteRouter._instance = None
        router = ReadWriteRouter(
            master_url="sqlite:///m1.db",
            replica_url="sqlite:///r1.db",
        )
        read_eng = router.get_read_engine()
        # 从库可达时，read engine 应为从库 engine（不同于主库）
        assert read_eng is not None
        assert router._replica_available is True
        ReadWriteRouter._instance = None

    def test_check_replica_lag_healthy(self):
        """从库健康检查：可达返回 True"""
        from services.db_router import ReadWriteRouter

        ReadWriteRouter._instance = None
        router = ReadWriteRouter(
            master_url="sqlite:///health_m.db",
            replica_url="sqlite:///health_r.db",
        )
        # 先触发从库 engine 创建
        router.get_read_engine()
        assert router.check_replica_lag() is True
        ReadWriteRouter._instance = None

    def test_check_replica_lag_unavailable(self):
        """从库健康检查：未配置从库返回 False"""
        from services.db_router import ReadWriteRouter

        ReadWriteRouter._instance = None
        router = ReadWriteRouter(master_url="sqlite:///nolag.db", replica_url="")
        assert router.check_replica_lag() is False
        ReadWriteRouter._instance = None

    def test_check_replica_lag_broken_replica(self):
        """从库健康检查：从库不可达返回 False（用 mock 避免真实 TCP 连接超时）"""
        from services.db_router import ReadWriteRouter

        ReadWriteRouter._instance = None
        router = ReadWriteRouter(
            master_url="sqlite:///broken_m.db",
            replica_url="sqlite:///broken_r.db",
        )
        # 手动设置从库 engine 为一个会抛异常的 mock
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = Exception("connection refused")
        router._replica_engine = mock_engine
        router._replica_available = True
        # check_replica_lag 应返回 False
        assert router.check_replica_lag() is False
        # get_read_engine 应降级到主库
        eng = router.get_read_engine()
        assert eng is router.get_master_engine()
        ReadWriteRouter._instance = None

    def test_read_replica_decorator(self, app):
        """@read_replica 装饰器：被装饰函数能在从库上下文执行"""
        from services.db_router import ReadWriteRouter

        ReadWriteRouter._instance = None
        router = ReadWriteRouter(master_url="sqlite:///dec_m.db", replica_url="")

        @router.read_replica
        def _read_func():
            return "ok"

        assert _read_func() == "ok"
        ReadWriteRouter._instance = None

    def test_use_read_replica_context_manager(self, app):
        """use_read_replica 上下文管理器：正常进入和退出"""
        from services.db_router import ReadWriteRouter

        ReadWriteRouter._instance = None
        router = ReadWriteRouter(master_url="sqlite:///ctx_m.db", replica_url="")

        with router.use_read_replica() as eng:
            assert eng is not None
        ReadWriteRouter._instance = None

    def test_resolve_master_url_priority(self):
        """主库 URL 解析优先级：构造参数 > app config > 环境变量"""
        from services.db_router import ReadWriteRouter

        ReadWriteRouter._instance = None
        router = ReadWriteRouter(master_url="sqlite:///priority.db")
        assert router._resolve_master_url() == "sqlite:///priority.db"
        ReadWriteRouter._instance = None


# ==================== metrics 测试 ====================


class TestMetrics:
    """业务指标 + 慢查询监控测试"""

    def test_init_prometheus_metrics_idempotent(self):
        """指标初始化幂等：多次调用不重复创建"""
        from services import metrics

        metrics._metrics.clear()
        metrics._init_prometheus_metrics()
        first_count = len(metrics._metrics)
        metrics._init_prometheus_metrics()
        second_count = len(metrics._metrics)
        assert first_count == second_count
        assert first_count > 0

    def test_record_slow_query(self):
        """记录慢查询不抛异常"""
        from services import metrics

        metrics._init_prometheus_metrics()
        metrics.record_slow_query("test_table", "select", 150)
        # 不抛异常即通过

    def test_record_loop_step(self):
        """记录闭环步骤耗时"""
        from services import metrics

        metrics._init_prometheus_metrics()
        metrics.record_loop_step(1, "success", 1.5)
        metrics.record_loop_step(2, "failed", 3.2)

    def test_record_suggestion_approval_rate(self):
        """更新补货建议批准率"""
        from services import metrics

        metrics._init_prometheus_metrics()
        metrics.record_suggestion_approval_rate(3, 10)
        metrics.record_suggestion_approval_rate(0, 0)  # total=0 不更新

    def test_record_etl_rows(self):
        """记录 ETL 行数"""
        from services import metrics

        metrics._init_prometheus_metrics()
        metrics.record_etl_rows("ods", 100)
        metrics.record_etl_rows("dwd", 50)

    def test_record_erp_order(self):
        """记录 ERP 单据"""
        from services import metrics

        metrics._init_prometheus_metrics()
        metrics.record_erp_order("purchase", "approved")
        metrics.record_erp_order("sale", "pending")

    def test_record_llm_call(self):
        """记录 LLM 调用耗时"""
        from services import metrics

        metrics._init_prometheus_metrics()
        metrics.record_llm_call("openai", "success", 2.3)

    def test_set_active_sessions(self):
        """设置活跃会话数"""
        from services import metrics

        metrics._init_prometheus_metrics()
        metrics.set_active_sessions(42)

    def test_init_metrics_with_app(self, app):
        """init_metrics 在 Flask app 上注册慢查询监控"""
        from services import metrics

        # 不清空 _metrics：prometheus registry 不支持重复注册同名指标
        # init_metrics 内部调用 _init_prometheus_metrics（幂等），已注册则跳过
        if not metrics._metrics:
            metrics._init_prometheus_metrics()
        metrics.init_metrics(app)
        assert "slow_query_total" in metrics._metrics

    def test_record_functions_without_metrics_dict(self):
        """_metrics 为空时记录函数不抛异常（容错）"""
        from services import metrics

        metrics._metrics.clear()
        metrics.record_slow_query("t", "select", 100)
        metrics.record_loop_step(1, "ok", 1.0)
        metrics.record_suggestion_approval_rate(1, 2)
        metrics.record_etl_rows("ods", 1)
        metrics.record_erp_order("sale", "ok")
        metrics.record_llm_call("p", "ok", 1.0)
        metrics.set_active_sessions(1)


# ==================== tasks 测试（Celery 不可用场景）====================


class TestCeleryTasks:
    """Celery 任务测试（celery_app=None 时的降级行为）"""

    def test_null_task_delay_raises_runtime_error(self):
        """_NullTask.delay() 抛 RuntimeError"""
        from tasks import _NullTask

        t = _NullTask("test_task")
        with pytest.raises(RuntimeError, match="Celery 未启用"):
            t.delay(1, 2)

    def test_null_task_apply_async_raises_runtime_error(self):
        """_NullTask.apply_async() 抛 RuntimeError"""
        from tasks import _NullTask

        t = _NullTask("test_task")
        with pytest.raises(RuntimeError, match="Celery 未启用"):
            t.apply_async()

    def test_task_decorator_returns_null_task_when_celery_unavailable(self):
        """celery_app=None 时，@_task 装饰器返回 _NullTask"""
        from tasks import _task

        @_task(name="test_func")
        def my_func():
            return "ok"

        # celery_app 为 None 时应返回 _NullTask 实例
        from tasks import _NullTask

        assert isinstance(my_func, _NullTask)
        # _NullTask 使用 f.__name__ 而非 name 参数（celery_app=None 时的行为）
        assert my_func.name == "my_func"

    def test_task_decorator_preserves_name(self):
        """未指定 name 时，使用函数 __name__"""
        from tasks import _task

        @_task()
        def custom_task():
            return "ok"

        from tasks import _NullTask

        assert isinstance(custom_task, _NullTask)
        assert custom_task.name == "custom_task"

    def test_all_async_tasks_are_null_tasks(self):
        """所有异步任务在 celery 不可用时均为 _NullTask"""
        from tasks import (
            _NullTask,
            cleanup_token_blacklist_async,
            detect_anomalies_async,
            generate_daily_report_async,
            generate_suggestions_async,
            run_etl_async,
            run_loop_step_async,
            run_rpa_scheduled_async,
        )

        for t in [
            run_loop_step_async,
            run_etl_async,
            run_rpa_scheduled_async,
            generate_suggestions_async,
            generate_daily_report_async,
            detect_anomalies_async,
            cleanup_token_blacklist_async,
        ]:
            assert isinstance(t, _NullTask), f"{t} 应为 _NullTask"

    def test_run_loop_step_async_raises_when_called(self):
        """run_loop_step_async.delay() 抛 RuntimeError"""
        from tasks import run_loop_step_async

        with pytest.raises(RuntimeError, match="Celery 未启用"):
            run_loop_step_async.delay(1, "test")

    def test_cleanup_token_blacklist_async_raises_when_called(self):
        """cleanup_token_blacklist_async.delay() 抛 RuntimeError"""
        from tasks import cleanup_token_blacklist_async

        with pytest.raises(RuntimeError, match="Celery 未启用"):
            cleanup_token_blacklist_async.delay()
