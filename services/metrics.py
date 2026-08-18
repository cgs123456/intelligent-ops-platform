"""业务指标 + 慢查询监控（P1-10）

提供：
1. SQLAlchemy 慢查询监控：通过 before_cursor_execute/after_cursor_execute 事件，
   记录执行时间 >100ms 的 SQL，打 WARN 日志 + Prometheus counter。
2. 业务 KPI 指标：闭环步骤耗时、建议批准率、ETL 行数、活跃会话数等。
3. Prometheus 自定义指标注册。

接入方式（在 app.py 的 create_app 中）：
    from services.metrics import init_metrics
    init_metrics(app)
"""

import logging
import time

logger = logging.getLogger(__name__)

# 慢查询阈值（毫秒）
SLOW_QUERY_THRESHOLD_MS = 100

# ==================== Prometheus 指标定义 ====================

# 延迟到 metrics 初始化时创建，避免无 prometheus_client 时报错
_metrics: dict = {}


def _init_prometheus_metrics():
    """初始化 Prometheus 自定义指标（幂等）"""
    if _metrics:
        return
    try:
        from prometheus_client import Counter, Gauge, Histogram

        _metrics["slow_query_total"] = Counter(
            "slow_query_total",
            "慢查询总数（执行时间 >100ms）",
            ["table", "operation"],
        )
        _metrics["loop_step_duration_seconds"] = Histogram(
            "loop_step_duration_seconds",
            "闭环步骤执行耗时（秒）",
            ["step", "status"],
            buckets=(0.5, 1, 2, 5, 10, 30, 60, 120),
        )
        _metrics["suggestion_approval_rate"] = Gauge(
            "suggestion_approval_rate",
            "补货建议批准率（0-1）",
        )
        _metrics["etl_layer_rows"] = Counter(
            "etl_layer_rows_total",
            "ETL 各层处理行数",
            ["layer"],
        )
        _metrics["active_sessions"] = Gauge(
            "active_sessions",
            "当前活跃会话数",
        )
        _metrics["erp_order_total"] = Counter(
            "erp_order_total",
            "ERP 单据总数",
            ["order_type", "status"],
        )
        _metrics["llm_call_duration_seconds"] = Histogram(
            "llm_call_duration_seconds",
            "LLM 调用耗时（秒）",
            ["provider", "status"],
            buckets=(0.5, 1, 2, 5, 10, 30),
        )
        # P1-6: SQLAlchemy 连接池指标（对应 alerts.yml: DBConnectionPoolExhaustion）
        # 告警规则使用 sqlalchemy_pool_checkedout / sqlalchemy_pool_size
        _metrics["sqlalchemy_pool_checkedout"] = Gauge(
            "sqlalchemy_pool_checkedout",
            "SQLAlchemy 连接池当前已检出（in-use）连接数",
        )
        _metrics["sqlalchemy_pool_size"] = Gauge(
            "sqlalchemy_pool_size",
            "SQLAlchemy 连接池总大小",
        )
        logger.info("Prometheus 业务指标已注册")
    except ImportError:
        logger.warning("prometheus_client 未安装，业务指标不可用")
    except Exception as e:
        logger.warning("Prometheus 指标注册失败：%s", e)


def record_slow_query(table, operation, duration_ms):
    """记录慢查询"""
    if "slow_query_total" in _metrics:
        _metrics["slow_query_total"].labels(table=table, operation=operation).inc()
    logger.warning("慢查询 table=%s op=%s duration=%dms", table, operation, duration_ms)


def record_loop_step(step, status, duration_seconds):
    """记录闭环步骤耗时"""
    if "loop_step_duration_seconds" in _metrics:
        _metrics["loop_step_duration_seconds"].labels(step=str(step), status=status).observe(duration_seconds)


def record_suggestion_approval_rate(approved, total):
    """更新补货建议批准率"""
    if "suggestion_approval_rate" in _metrics and total > 0:
        _metrics["suggestion_approval_rate"].set(approved / total)


def record_etl_rows(layer, count):
    """记录 ETL 各层处理行数"""
    if "etl_layer_rows" in _metrics:
        _metrics["etl_layer_rows"].labels(layer=layer).inc(count)


def record_erp_order(order_type, status):
    """记录 ERP 单据"""
    if "erp_order_total" in _metrics:
        _metrics["erp_order_total"].labels(order_type=order_type, status=status).inc()


def record_llm_call(provider, status, duration_seconds):
    """记录 LLM 调用耗时"""
    if "llm_call_duration_seconds" in _metrics:
        _metrics["llm_call_duration_seconds"].labels(provider=provider, status=status).observe(duration_seconds)


def set_active_sessions(count):
    """设置当前活跃会话数"""
    if "active_sessions" in _metrics:
        _metrics["active_sessions"].set(count)


def init_metrics(app):
    """初始化指标监控（慢查询 + 业务 KPI）

    在 create_app 中调用，注册 SQLAlchemy 事件监听器。
    """
    _init_prometheus_metrics()

    # 注册慢查询监控
    try:
        from sqlalchemy import event

        from extensions import db

        # 用字典存储每个连接的查询开始时间
        _query_timings = {}

        @event.listens_for(db.engine, "before_cursor_execute")
        def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            _query_timings[id(cursor)] = time.time()

        @event.listens_for(db.engine, "after_cursor_execute")
        def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            start = _query_timings.pop(id(cursor), None)
            if start is None:
                return
            duration_ms = (time.time() - start) * 1000
            if duration_ms > SLOW_QUERY_THRESHOLD_MS:
                # 提取表名和操作类型
                stmt_lower = statement.lower().strip()
                operation = "unknown"
                if stmt_lower.startswith("select"):
                    operation = "select"
                elif stmt_lower.startswith("insert"):
                    operation = "insert"
                elif stmt_lower.startswith("update"):
                    operation = "update"
                elif stmt_lower.startswith("delete"):
                    operation = "delete"

                # 提取表名（简单解析）
                table = "unknown"
                for kw in ["from", "into", "update", "table"]:
                    if kw in stmt_lower:
                        parts = stmt_lower.split(kw)
                        if len(parts) > 1:
                            table = parts[1].strip().split()[0].strip('"').strip("'").strip("`")
                            break

                record_slow_query(table, operation, int(duration_ms))

        logger.info("慢查询监控已启用，阈值=%dms", SLOW_QUERY_THRESHOLD_MS)
    except Exception as e:
        logger.warning("慢查询监控初始化失败：%s", e)

    # P1-6: 周期性采集 SQLAlchemy 连接池指标（每 15s 一次，与 Prometheus scrape_interval 对齐）
    try:
        from extensions import db

        def _update_pool_metrics():
            """采集连接池 checkedout / size 并更新 Gauge"""
            try:
                engine = db.engine
                pool = engine.pool
                if hasattr(pool, "checkedout"):
                    _metrics["sqlalchemy_pool_checkedout"].set(pool.checkedout())
                if hasattr(pool, "size"):
                    _metrics["sqlalchemy_pool_size"].set(pool.size())
            except Exception as e:
                logger.debug("连接池指标采集失败：%s", e)

        # 使用 background 线程周期性更新
        import threading

        _pool_metrics_stop = threading.Event()

        def _pool_metrics_loop():
            while not _pool_metrics_stop.wait(15.0):
                _update_pool_metrics()

        t = threading.Thread(target=_pool_metrics_loop, daemon=True, name="pool-metrics")
        t.start()
        logger.info("SQLAlchemy 连接池指标监控已启用（15s 间隔）")
    except Exception as e:
        logger.warning("连接池指标监控初始化失败：%s", e)
