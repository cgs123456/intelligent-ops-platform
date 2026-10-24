"""Celery 异步任务模块测试

覆盖：
- _NullTask 占位行为（delay/apply_async 抛 RuntimeError）
- _task 装饰器：celery_app 为 None 时返回 _NullTask，非 None 时调用 celery_app.task
- _get_app 双重检查锁定（延迟初始化 app context）
- 任务对象在 celery 不可用时为 _NullTask 实例
"""

import threading

import pytest


class TestNullTask:
    """_NullTask 占位 task 测试"""

    def test_null_task_delay_raises(self):
        from tasks import _NullTask

        task = _NullTask("test_task")
        assert task.name == "test_task"
        with pytest.raises(RuntimeError, match="Celery 未启用"):
            task.delay(1, 2)

    def test_null_task_apply_async_raises(self):
        from tasks import _NullTask

        task = _NullTask("another_task")
        with pytest.raises(RuntimeError, match="Celery 未启用"):
            task.apply_async(args=(1,), kwargs={"k": "v"})

    def test_null_task_delay_no_args(self):
        from tasks import _NullTask

        task = _NullTask("noarg")
        with pytest.raises(RuntimeError):
            task.delay()


class TestTaskDecorator:
    """_task 装饰器测试：celery 可用/不可用两种场景"""

    def test_decorator_celery_none_returns_nulltask(self, monkeypatch):
        """celery_app 为 None 时，装饰器返回 _NullTask 实例"""
        import tasks

        monkeypatch.setattr(tasks, "celery_app", None, raising=False)

        @tasks._task(name="my.test.task")
        def my_func():
            return "ok"

        from tasks import _NullTask

        assert isinstance(my_func, _NullTask)
        assert my_func.name == "my_func"  # 用 f.__name__

    def test_decorator_celery_available_wraps_function(self, monkeypatch):
        """celery_app 非 None 时，装饰器调用 celery_app.task 包装函数"""
        import tasks

        class FakeCelery:
            def __init__(self):
                self.tasks = {}

            def task(self, name=None, **kwargs):
                bind = kwargs.get("bind", False)

                def decorator(f):
                    # 模拟 celery 的 task 包装：返回带 run 方法的对象
                    class FakeTask:
                        def __init__(self, func, task_name):
                            self._func = func
                            self.name = task_name

                        def run(self, *args, **kwargs):
                            if bind:
                                return self._func(self, *args, **kwargs)
                            return self._func(*args, **kwargs)

                        def delay(self, *args, **kwargs):
                            return self.run(*args, **kwargs)

                    return FakeTask(f, name or f.__name__)

                return decorator

        fake = FakeCelery()
        monkeypatch.setattr("tasks.celery_app", fake)

        @tasks._task(name="my.wrapped.task", bind=True)
        def my_func(self, x):
            return x * 2

        # 应返回 FakeTask 实例（非 _NullTask）
        from tasks import _NullTask

        assert not isinstance(my_func, _NullTask)
        assert my_func.name == "my.wrapped.task"
        # delay 可调用并返回原函数结果
        assert my_func.delay(21) == 42


class TestGetApp:
    """_get_app 双重检查锁定测试"""

    def test_get_app_returns_app_context(self, monkeypatch):
        """_get_app 返回 app context 实例"""
        import tasks

        # 重置全局状态（P0-3: _app_instance 共享单例 + _app_ctx_local 线程局部）
        monkeypatch.setattr(tasks, "_app_instance", None)
        if hasattr(tasks._app_ctx_local, "ctx"):
            del tasks._app_ctx_local.ctx

        # mock create_app 返回带 app_context 的假 app
        class FakeCtx:
            def push(self):
                pass

        class FakeApp:
            def app_context(self):
                return FakeCtx()

        # 捕获是否调用了 create_app（_get_app 内部 from app import create_app）
        create_calls = []
        monkeypatch.setattr("app.create_app", lambda cfg: (create_calls.append(1), FakeApp())[1])

        ctx = tasks._get_app()
        assert ctx is not None
        assert len(create_calls) == 1

    def test_get_app_singleton_double_checked(self, monkeypatch):
        """_get_app 第二次调用不重复创建 app（双重检查锁定）"""
        import tasks

        monkeypatch.setattr(tasks, "_app_instance", None)
        if hasattr(tasks._app_ctx_local, "ctx"):
            del tasks._app_ctx_local.ctx
        create_calls = []

        class FakeCtx:
            def push(self):
                pass

        class FakeApp:
            def app_context(self):
                return FakeCtx()

        monkeypatch.setattr("app.create_app", lambda cfg: (create_calls.append(1), FakeApp())[1])

        ctx1 = tasks._get_app()
        ctx2 = tasks._get_app()
        assert ctx1 is ctx2
        assert len(create_calls) == 1  # 只创建一次

    def test_get_app_thread_safe(self, monkeypatch):
        """多线程并发调用 _get_app 只创建一个 app context"""
        import tasks

        monkeypatch.setattr(tasks, "_app_instance", None)
        if hasattr(tasks._app_ctx_local, "ctx"):
            del tasks._app_ctx_local.ctx
        create_calls = []
        lock = threading.Lock()

        class FakeCtx:
            def push(self):
                pass

        class FakeApp:
            def app_context(self):
                return FakeCtx()

        def fake_create(cfg):
            with lock:
                create_calls.append(1)
            return FakeApp()

        monkeypatch.setattr("app.create_app", fake_create)

        results = []

        def worker():
            results.append(tasks._get_app())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有线程拿到同一个 context
        assert all(r is results[0] for r in results)
        assert len(create_calls) == 1


_ALL_ASYNC_TASKS = [
    "run_loop_step_async",
    "run_etl_async",
    "run_rpa_scheduled_async",
    "generate_suggestions_async",
    "generate_daily_report_async",
    "detect_anomalies_async",
    "cleanup_token_blacklist_async",
]


@pytest.fixture()
def tasks_without_celery(monkeypatch):
    """以 celery_app=None 重新加载 tasks 模块，模拟 Celery 未安装/未配置。

    无论当前环境是否安装 celery，reload 后模块级任务都走 _NullTask 降级分支，
    测试因此环境密闭；teardown 先还原 extensions.celery_app 再 reload，
    保证共享会话里的后续测试拿到与真实环境一致的 tasks 模块。
    """
    import importlib

    import extensions
    import tasks

    monkeypatch.setattr(extensions, "celery_app", None)
    reloaded = importlib.reload(tasks)
    yield reloaded
    monkeypatch.undo()
    importlib.reload(tasks)


class TestModuleTasksCeleryUnavailable:
    """celery 不可用时（celery_app=None），所有模块任务降级为 _NullTask"""

    def test_all_tasks_are_nulltask(self, tasks_without_celery):
        for name in _ALL_ASYNC_TASKS:
            t = getattr(tasks_without_celery, name)
            assert isinstance(t, tasks_without_celery._NullTask), f"{name} 应为 _NullTask 实例"

    def test_run_loop_step_async_delay_raises(self, tasks_without_celery):
        with pytest.raises(RuntimeError, match="Celery 未启用"):
            tasks_without_celery.run_loop_step_async.delay(1, actor="test")

    def test_run_etl_async_delay_raises(self, tasks_without_celery):
        with pytest.raises(RuntimeError):
            tasks_without_celery.run_etl_async.delay()

    def test_cleanup_token_blacklist_async_apply_async_raises(self, tasks_without_celery):
        with pytest.raises(RuntimeError):
            tasks_without_celery.cleanup_token_blacklist_async.apply_async()

    def test_beat_schedule_block_skipped(self, tasks_without_celery):
        """celery_app=None 时 beat_schedule 初始化块被跳过，模块加载本身不抛异常"""
        assert hasattr(tasks_without_celery, "run_loop_step_async")


class TestModuleTasksCeleryAvailable:
    """celery 可用时（celery_app 非 None），模块任务绑定真实 Celery Task"""

    def test_all_tasks_bound_to_celery(self):
        import tasks
        from extensions import celery_app
        from tasks import _NullTask

        if celery_app is None:
            pytest.skip("当前环境未配置 Celery broker，降级路径由 TestModuleTasksCeleryUnavailable 覆盖")
        for name in _ALL_ASYNC_TASKS:
            t = getattr(tasks, name)
            assert not isinstance(t, _NullTask), f"{name} 不应为 _NullTask"
            assert hasattr(t, "run") and hasattr(t, "delay"), f"{name} 缺少 Celery Task 接口"

    def test_beat_schedule_all_crontab(self):
        """P0-7 回归：beat_schedule 必须全部是 crontab 对象。

        裸 cron 字符串（"0 2 * * *"）会让 celery beat 启动即崩：
        AttributeError: 'str' object has no attribute 'app'。
        """
        from celery.schedules import crontab

        from extensions import celery_app

        if celery_app is None:
            pytest.skip("当前环境未配置 Celery broker，降级路径由 TestModuleTasksCeleryUnavailable 覆盖")
        schedule = celery_app.conf.beat_schedule
        assert schedule, "beat_schedule 未配置"
        assert len(schedule) == 5, f"应有 5 个定时任务，实际 {len(schedule)}"
        for key, entry in schedule.items():
            sched = entry["schedule"]
            assert isinstance(sched, crontab), (
                f"beat 任务 {key} 的 schedule 必须是 crontab 对象（P0-7），当前: {type(sched)}"
            )
        assert celery_app.conf.timezone == "Asia/Shanghai"
