"""闭环编排器
五步闭环：AIGC建议 → 人审核 → RPA下单 → ERP记账 → FDE刷新
支持：跨进程文件锁、超时处理、回滚机制、自动触发、SSE进度
基于 LangGraph StateGraph 编排（不可用时降级为 if-elif 硬编码）
"""

import json
import logging
import os
import threading
from datetime import datetime
from functools import wraps
from typing import TypedDict

from extensions import db
from models.system import AuditLog, LoopState

logger = logging.getLogger(__name__)

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # Windows 无 fcntl

# Windows 跨进程锁兜底：msvcrt.locking
try:
    import msvcrt

    _HAS_MSVCRT = True
except ImportError:
    _HAS_MSVCRT = False

# LangGraph 可选依赖（不可用时降级为 if-elif 硬编码状态机）
try:
    from langgraph.graph import END, StateGraph
    from langgraph.types import Command, interrupt

    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False
    END = None
    StateGraph = None
    Command = None
    interrupt = None


def _file_lock(run_id, step):
    """跨进程文件锁（兼容 gunicorn 多 worker）。
    Linux/Mac 用 fcntl.flock；Windows 用 msvcrt.locking 实现跨进程锁。
    两者都不可用时返回 None，由 _acquire_lock 退回 threading（仅进程内有效）。
    """
    if not _HAS_FCNTL and not _HAS_MSVCRT:
        return None, None
    # 锁文件目录：Windows 默认 instance/，Linux 默认 /tmp
    if os.name == "nt":
        default_dir = os.path.join(os.getcwd(), "instance")
    else:
        default_dir = "/tmp"
    lock_dir = os.getenv("LOOP_LOCK_DIR", default_dir)
    try:
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, f"loop_{run_id}_{step}.lock")
        # P0-3: 用 os.open + O_NOFOLLOW 防 symlink 劫持，权限 0o600 防其他用户读写
        # O_EXCL 确保是我们创建的文件而非符号链接
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            # 文件已存在（上次未清理），重新以 O_WRONLY 打开（不跟随符号链接）
            fd = os.open(lock_path, os.O_WRONLY | os.O_NOFOLLOW)
        f = os.fdopen(fd, "w")
        # msvcrt.locking 要求文件至少有 1 字节内容
        if _HAS_MSVCRT and not _HAS_FCNTL:
            f.write("L")
            f.flush()
        return f, lock_path
    except Exception:
        return None, None


def _try_acquire_file_lock(f):
    """尝试获取文件排他锁，非阻塞。成功返回 True。
    Linux/Mac 用 fcntl.flock；Windows 用 msvcrt.locking。
    """
    if f is None:
        return False
    if _HAS_FCNTL:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if _HAS_MSVCRT:
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    return False


# Windows 兜底：进程内 threading 锁
_thread_locks = {}
_thread_locks_mutex = threading.Lock()


def _get_thread_lock(run_id, step):
    key = (run_id, step)
    with _thread_locks_mutex:
        if key not in _thread_locks:
            _thread_locks[key] = threading.Lock()
        return _thread_locks[key]


def _acquire_lock(run_id, step):
    """获取锁（文件锁优先，threading 兜底）。返回 (locked, release_fn)"""
    # 尝试文件锁
    f, path = _file_lock(run_id, step)
    if f is not None and _try_acquire_file_lock(f):

        def release():
            try:
                if _HAS_FCNTL:
                    fcntl.flock(f, fcntl.LOCK_UN)
                elif _HAS_MSVCRT:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
                    except OSError:
                        pass
            except Exception:
                pass
            f.close()
            try:
                os.unlink(path)
            except Exception:
                pass

        return True, release
    # 文件锁不可用（fcntl/msvcrt 都缺失），退回 threading 并记录 warning
    if not _HAS_FCNTL and not _HAS_MSVCRT:
        logger.warning("跨进程文件锁不可用（fcntl 与 msvcrt 均缺失），仅使用进程内 threading 锁")
    lock = _get_thread_lock(run_id, step)
    if lock.acquire(blocking=False):
        return True, lambda: lock.release()
    return False, lambda: None


def with_timeout(seconds):
    """P2-5/P1-1: 步骤超时装饰器。
    使用 threading 模式（daemon 线程 + Event.wait），避免 multiprocessing 破坏 DB 事务。
    线程无法被强制 kill，超时后主线程抛 TimeoutError 并通过 cancel_event 通知子线程取消。
    会自动把 Flask app context 传递到子线程。

    P0-运维2: 增加 cooperative cancellation 机制：
    - 创建 cancel_event，尝试注入被装饰函数 kwargs，子线程可在关键操作前检查并提前返回不 commit
    - 主线程超时后 set cancel_event 通知子线程退出
    - 被装饰函数若不接受 cancel_event 参数（TypeError），降级为不检查
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            from flask import current_app

            app = current_app._get_current_object()  # type: ignore[attr-defined]

            cancel_event = threading.Event()
            result_box: list = [None]
            error_box: list = [None]
            done_event = threading.Event()

            def target_thread():
                try:
                    with app.app_context():
                        try:
                            # 尝试将 cancel_event 注入 kwargs，让被装饰函数可以检查
                            result_box[0] = func(*args, cancel_event=cancel_event, **kwargs)
                        except TypeError:
                            # 被装饰函数不接受 cancel_event 参数，降级为不检查
                            result_box[0] = func(*args, **kwargs)
                except Exception as e:
                    error_box[0] = e
                finally:
                    done_event.set()

            t = threading.Thread(target=target_thread, daemon=True)
            t.start()
            # 主线程等待 done_event 或超时
            if not done_event.wait(timeout=seconds):
                cancel_event.set()  # 通知子线程取消
                raise TimeoutError(f"步骤执行超时（{seconds}秒），已发送取消信号，后台线程可能仍在运行")
            if error_box[0]:
                raise error_box[0]
            return result_box[0]

        return wrapper

    return decorator


# ==================== LangGraph 状态 Schema ====================

if _HAS_LANGGRAPH:

    class ClosedLoopState(TypedDict, total=False):
        """LangGraph 闭环状态 schema

        字段说明：
        - run_id: 闭环运行轮次 ID
        - current_step: 当前执行步骤号 (1-5)
        - steps_status: 各步骤状态 {step_no: "pending"/"running"/"done"/"failed"}
        - suggestions: 补货建议列表（step1 产出）
        - orders: 采购单列表（step3 产出）
        - errors: 错误信息列表
        - actor: 操作人
        - detail: 当前步骤详情
        - retry_count: step3 重试次数
        - need_manual: step2 是否需要人工审核
        """

        run_id: int
        current_step: int
        steps_status: dict
        suggestions: list
        orders: list
        errors: list
        actor: str
        detail: str
        retry_count: int
        need_manual: bool

else:
    # langgraph 不可用时占位
    ClosedLoopState = None  # type: ignore[misc]


class ClosedLoop:
    """五步闭环状态机（LangGraph StateGraph 编排）

    编排流程：
        step1(AIGC建议) --无建议--> step5(FDE刷新) --> END
                         --有建议--> step2(人工审核, interrupt)
        step2 --> step3(RPA下单)
        step3 --失败重试--> step3
             --继续--> step4(ERP入库) + step5(FDE刷新) 并行 --> END

    LangGraph 不可用时降级为原有 if-elif 硬编码逻辑。
    """

    STEPS = [
        (1, "AIGC生成建议", "AIGC"),
        (2, "人工审核", "人"),
        (3, "RPA自动下单", "RPA"),
        (4, "ERP记账入库", "ERP"),
        (5, "FDE刷新指标", "FDE"),
    ]

    # 懒加载的 LangGraph 编译图
    _graph = None
    _graph_lock = threading.Lock()

    @staticmethod
    def _services():
        from services.aigc_service import AIGCService
        from services.erp_service import ERPService
        from services.rpa_service import RPAService
        from services.warehouse_service import WarehouseService

        return AIGCService, RPAService, ERPService, WarehouseService

    @staticmethod
    def _get_timeout():
        """从配置读取超时秒数"""
        try:
            from flask import current_app

            return current_app.config.get("LOOP_TIMEOUT", 60)
        except Exception:
            return 60

    # ==================== LangGraph 图定义 ====================

    @staticmethod
    def _get_graph():
        """懒加载编译好的 LangGraph StateGraph（线程安全双重检查锁定）"""
        if not _HAS_LANGGRAPH:
            return None
        if ClosedLoop._graph is None:
            with ClosedLoop._graph_lock:
                if ClosedLoop._graph is None:
                    ClosedLoop._graph = ClosedLoop._build_graph()
        return ClosedLoop._graph

    # P0-fix 注意：本方法构建的 LangGraph 图当前未被 graph.invoke() 调用。
    # 实际执行路径为 _run_step_via_graph() 中的 getattr 直接调用节点函数。
    # 图的条件路由（_route_after_step1/step3）和并行执行（fan_out）暂未生效。
    # 保留代码供未来启用完整图编排，但当前应视为未使用的死代码。
    # 如需启用，需在 run_step 中改为 graph.invoke({"step_no": step_no, ...})。
    @staticmethod
    def _build_graph():
        """构建五步闭环 StateGraph

        当前未在运行时调用，保留用于未来完整编排模式（一次性 graph.invoke() 走完 5 步）。
        运行时单步执行由 _run_step_via_graph 直接调用节点函数实现。

        节点：step1 ~ step5 对应 5 个步骤，fan_out 为并行分发节点
        边：
        - step1 → 条件分支（无补货建议跳到 step5，否则 step2 人工审核）
        - step2 → step3（人工审核 interrupt 完成后继续）
        - step3 → 条件分支（下单失败重试 step3，否则到 fan_out）
        - fan_out → step4 + step5（并行执行 ERP入库 和 FDE刷新）
        - step4, step5 → END（并行汇聚）
        """
        if not _HAS_LANGGRAPH:
            return None

        graph = StateGraph(ClosedLoopState)

        # 注册 5 个步骤节点 + 1 个并行分发节点
        graph.add_node("step1", ClosedLoop._node_step1)
        graph.add_node("step2", ClosedLoop._node_step2)
        graph.add_node("step3", ClosedLoop._node_step3)
        graph.add_node("step4", ClosedLoop._node_step4)
        graph.add_node("step5", ClosedLoop._node_step5)
        graph.add_node("fan_out", ClosedLoop._node_fan_out)

        # 入口：step1
        graph.set_entry_point("step1")

        # step1 → 条件路由：无补货建议则跳过 step2/3 直接到 step5
        graph.add_conditional_edges(
            "step1",
            ClosedLoop._route_after_step1,
            {"review": "step2", "skip": "step5"},
        )

        # step2 → step3（人工审核完成后继续下单）
        graph.add_edge("step2", "step3")

        # step3 → 条件路由：下单失败重试（最多 2 次），否则到 fan_out 并行执行
        graph.add_conditional_edges(
            "step3",
            ClosedLoop._route_after_step3,
            {"retry": "step3", "continue": "fan_out"},
        )

        # fan_out → step4 + step5（并行执行 ERP入库 和 FDE刷新）
        graph.add_edge("fan_out", "step4")
        graph.add_edge("fan_out", "step5")

        # step4, step5 → END（并行汇聚）
        graph.add_edge("step4", END)
        graph.add_edge("step5", END)

        return graph.compile()

    @staticmethod
    def _node_fan_out(state):
        """并行分发节点：触发 step4（ERP入库）和 step5（FDE刷新）并行执行

        该节点本身不执行业务逻辑，仅作为 fan-out 分发点。
        """
        return state

    # ==================== 条件路由函数 ====================

    @staticmethod
    def _route_after_step1(state):
        """step1 后路由：无补货建议则跳过 step2/3 直接到 step5

        :param state: LangGraph 状态
        :return: "review"（有建议→人工审核）或 "skip"（无建议→直接刷新指标）
        """
        suggestions = state.get("suggestions", [])
        detail = state.get("detail", "")
        # 无建议（detail 含"无需补货"或 suggestions 为空）则跳过审核和下单
        if not suggestions or (detail and "无需补货" in detail):
            return "skip"
        return "review"

    @staticmethod
    def _route_after_step3(state):
        """step3 后路由：下单失败可重试（最多 2 次），否则并行执行 step4 + step5

        :param state: LangGraph 状态
        :return: "retry"（重试下单）或 "continue"（并行入库 + 刷新）
        """
        errors = state.get("errors", [])
        retry_count = state.get("retry_count", 0)
        # 有错误且重试次数未超限则重试
        if errors and retry_count < 2:
            return "retry"
        return "continue"

    # ==================== LangGraph 节点函数 ====================

    @staticmethod
    def _prepare_state(step_no, actor):
        """准备 LangGraph 状态（从 DB 读取已有步骤状态）

        :param step_no: 要执行的步骤号
        :param actor: 操作人
        :return: ClosedLoopState 字典
        """
        run_id = ClosedLoop.get_current_run_id()
        records = LoopState.query.filter_by(run_id=run_id).all()
        steps_status = {r.step: r.status for r in records}

        # 查询当前补货建议（用于路由判断）
        suggestions = []
        try:
            from models.aigc import Suggestion

            suggestions = Suggestion.query.filter(Suggestion.status.in_(["pending", "approved"])).all()
        except Exception:
            pass

        return {
            "run_id": run_id,
            "current_step": step_no,
            "steps_status": steps_status,
            "suggestions": suggestions,
            "orders": [],
            "errors": [],
            "actor": actor,
            "detail": "",
            "retry_count": 0,
            "need_manual": False,
        }

    @staticmethod
    def _execute_node(state, step_no, step_name, step_func):
        """通用图节点执行器：处理跨进程锁、LoopState 记录、超时、审计日志、告警

        :param state: LangGraph 状态
        :param step_no: 步骤号
        :param step_name: 步骤名称
        :param step_func: 步骤执行函数 (actor) -> detail_str
        :return: API 响应 dict（同时包含状态更新字段供 LangGraph 使用）
        """
        run_id = state["run_id"]
        actor = state["actor"]

        # 获取跨进程锁
        locked, release = _acquire_lock(run_id, step_no)
        if not locked:
            return {"error": f"步骤{step_no}正在执行中（其他进程持有锁），请勿重复触发", "step": step_no}

        try:
            rec = LoopState.query.filter_by(run_id=run_id, step=step_no).first()
            if rec and rec.status == "done":
                return {"error": f"步骤{step_no}已完成，请从下一步继续", "step": step_no}

            if not rec:
                rec = LoopState(run_id=run_id, step=step_no, step_name=step_name, status="running")
                rec.started_at = datetime.now()
                db.session.add(rec)
            else:
                rec.status = "running"
                rec.started_at = datetime.now()
            db.session.commit()

            try:
                timeout = ClosedLoop._get_timeout()

                @with_timeout(timeout)
                def _execute(cancel_event=None):
                    # P0-2 fix: check cancel_event before and after step execution
                    if cancel_event is not None and cancel_event.is_set():
                        raise TimeoutError("Step cancelled before execution")
                    detail = step_func(actor)
                    # P0-2 fix: if cancelled during execution, rollback instead of commit
                    if cancel_event is not None and cancel_event.is_set():
                        db.session.rollback()
                        raise TimeoutError("Step cancelled during execution")
                    return detail

                detail = _execute()
                rec.status = "done"
                rec.detail = detail
                rec.finished_at = datetime.now()
                db.session.commit()
                db.session.add(
                    AuditLog(
                        actor=actor,
                        action="loop_step_done",
                        target_type="loop",
                        target_id=f"{run_id}-{step_no}",
                        detail=detail[:500] if detail else "",
                    )
                )
                db.session.commit()
                return {"step": step_no, "status": "done", "detail": detail}

            except TimeoutError as e:
                db.session.rollback()
                rec = LoopState.query.filter_by(run_id=run_id, step=step_no).first()
                if rec:
                    rec.status = "failed"
                    rec.detail = f"超时：{str(e)}"
                    rec.finished_at = datetime.now()
                    db.session.commit()
                db.session.add(
                    AuditLog(
                        actor=actor,
                        action="loop_step_timeout",
                        target_type="loop",
                        target_id=f"{run_id}-{step_no}",
                        detail=str(e)[:500],
                    )
                )
                db.session.commit()
                # P1-2: 失败时触发告警通知
                try:
                    from services.notifier import notifier

                    notifier.send_alert(
                        title=f"闭环步骤{step_no}超时",
                        message=f"Run #{run_id} step {step_no} 超时：{str(e)[:200]}",
                        level="error",
                        context={"run_id": run_id, "step": step_no},
                    )
                except Exception:
                    pass
                # P0-2 fix: release session after timeout to prevent daemon thread
                # from using the shared db.session (SQLAlchemy sessions are not thread-safe)
                db.session.remove()
                return {"step": step_no, "error": f"步骤超时：{e}"}
            except Exception as e:
                logger.exception("步骤%s执行失败", step_no)
                db.session.rollback()
                rec = LoopState.query.filter_by(run_id=run_id, step=step_no).first()
                if rec:
                    rec.status = "failed"
                    rec.detail = str(e)[:500]
                    rec.finished_at = datetime.now()
                    db.session.commit()
                db.session.add(
                    AuditLog(
                        actor=actor,
                        action="loop_step_failed",
                        target_type="loop",
                        target_id=f"{run_id}-{step_no}",
                        detail=str(e)[:500],
                    )
                )
                db.session.commit()
                # P1-2: 失败时触发告警通知
                try:
                    from services.notifier import notifier

                    notifier.send_alert(
                        title=f"闭环步骤{step_no}执行失败",
                        message=f"Run #{run_id} step {step_no} 异常：{str(e)[:200]}",
                        level="error",
                        context={"run_id": run_id, "step": step_no, "error": str(e)[:100]},
                    )
                except Exception:
                    pass
                return {"step": step_no, "error": str(e)}
        finally:
            release()

    @staticmethod
    def _node_step1(state):
        """LangGraph 节点：步骤1 AIGC生成补货建议

        执行后根据建议数量更新状态，供条件路由 _route_after_step1 判断。
        """
        result = ClosedLoop._execute_node(
            state, 1, "AIGC生成建议", lambda actor: ClosedLoop._step1_generate_suggestions()
        )
        # 补充状态字段供 LangGraph 路由使用
        if result.get("status") == "done":
            detail = result.get("detail", "")
            has_suggestions = "无需补货" not in detail if detail else False
            # 查询 DB 中的建议数量
            try:
                from models.aigc import Suggestion

                count = Suggestion.query.filter(Suggestion.status.in_(["pending", "approved"])).count()
            except Exception:
                count = 0
            result["suggestions"] = count if has_suggestions else 0
            result["current_step"] = 1
        return result

    @staticmethod
    def _node_step2(state):
        """LangGraph 节点：步骤2 人工审核

        当前为单步执行模式，interrupt 仅在完整 graph.invoke() 模式下生效。
        本节点直接返回 need_manual 状态，人工审核通过 API 调用 run_step(3) 推进。
        """
        run_id = state["run_id"]
        actor = state["actor"]
        step_no = 2

        # 获取跨进程锁
        locked, release = _acquire_lock(run_id, step_no)
        if not locked:
            return {"error": f"步骤{step_no}正在执行中（其他进程持有锁），请勿重复触发", "step": step_no}

        try:
            rec = LoopState.query.filter_by(run_id=run_id, step=step_no).first()
            if rec and rec.status == "done":
                return {"error": f"步骤{step_no}已完成，请从下一步继续", "step": step_no}

            if not rec:
                rec = LoopState(run_id=run_id, step=step_no, step_name="人工审核", status="running")
                rec.started_at = datetime.now()
                db.session.add(rec)
            else:
                rec.status = "running"
                rec.started_at = datetime.now()
            db.session.commit()

            # 设置为等待人工审核状态
            rec.status = "pending"
            rec.detail = "等待人工审核建议"
            rec.finished_at = datetime.now()
            db.session.commit()
            db.session.add(
                AuditLog(
                    actor=actor,
                    action="loop_step_wait",
                    target_type="loop",
                    target_id=str(run_id),
                    detail=f"步骤{step_no}等待人工审核",
                )
            )
            db.session.commit()

            return {
                "step": 2,
                "message": "请前往审核看板处理待审建议",
                "need_manual": True,
                "current_step": 2,
                "need_manual_state": True,
            }
        finally:
            release()

    @staticmethod
    def _node_step3(state):
        """LangGraph 节点：步骤3 RPA自动下单

        执行后根据结果更新 errors 和 retry_count，供条件路由 _route_after_step3 判断。
        """
        result = ClosedLoop._execute_node(state, 3, "RPA自动下单", lambda actor: ClosedLoop._step3_rpa_order(actor))
        # 补充状态字段供 LangGraph 路由使用
        if result.get("error"):
            result["errors"] = [result["error"]]
            result["retry_count"] = state.get("retry_count", 0) + 1
        else:
            result["errors"] = []
        result["current_step"] = 3
        return result

    @staticmethod
    def _node_step4(state):
        """LangGraph 节点：步骤4 ERP记账入库（与 step5 可并行）"""
        result = ClosedLoop._execute_node(state, 4, "ERP记账入库", lambda actor: ClosedLoop._step4_erp_receive(actor))
        result["current_step"] = 4
        return result

    @staticmethod
    def _node_step5(state):
        """LangGraph 节点：步骤5 FDE刷新指标（与 step4 可并行）"""
        result = ClosedLoop._execute_node(state, 5, "FDE刷新指标", lambda actor: ClosedLoop._step5_fde_refresh(actor))
        result["current_step"] = 5
        return result

    # ==================== 状态查询 ====================

    @staticmethod
    def get_current_run_id():
        # 跳过 archived 记录，找最新的未归档 run
        last = LoopState.query.filter(LoopState.status != "archived").order_by(LoopState.run_id.desc()).first()
        if not last:
            # 全部归档或无记录，开启新一轮
            last_any = LoopState.query.order_by(LoopState.run_id.desc()).first()
            return (last_any.run_id + 1) if last_any else 1
        last_round = LoopState.query.filter_by(run_id=last.run_id).all()
        if all(s.status == "done" for s in last_round) and len(last_round) == 5:
            return last.run_id + 1
        return last.run_id

    @staticmethod
    def get_status():
        run_id = ClosedLoop.get_current_run_id()
        records = LoopState.query.filter_by(run_id=run_id).order_by(LoopState.step).all()
        steps = []
        for step_no, step_name, owner in ClosedLoop.STEPS:
            rec = next((r for r in records if r.step == step_no), None)
            steps.append(
                {
                    "step": step_no,
                    "name": step_name,
                    "owner": owner,
                    "status": rec.status if rec else "pending",
                    "detail": rec.detail if rec else "",
                    "time": rec.created_at.strftime("%H:%M:%S") if rec else "-",
                }
            )
        current_step = 1
        for s in steps:
            if s["status"] == "done":
                current_step = int(s["step"]) + 1
            elif s["status"] in ("running", "failed"):
                current_step = int(s["step"])
                break
        if current_step > 5:
            current_step = 5
        return {"run_id": run_id, "current_step": current_step, "steps": steps}

    # ==================== 执行步骤（公共 API） ====================

    @staticmethod
    def run_step(step_no, actor="system"):
        """执行指定步骤（带跨进程锁 + 超时）

        LangGraph 可用时通过图节点分发，否则降级为 if-elif 硬编码逻辑。
        公共 API 签名保持兼容。
        """
        if _HAS_LANGGRAPH and ClosedLoop._get_graph() is not None:
            return ClosedLoop._run_step_via_graph(step_no, actor)
        return ClosedLoop._run_step_legacy(step_no, actor)

    @staticmethod
    def _run_step_via_graph(step_no, actor="system"):
        """单步执行模式：通过 LangGraph 图节点函数执行单步。

        当前实际执行方式（P0-fix 标注）：
            本方法 **不调用 graph.invoke()**，而是通过 ``getattr(ClosedLoop, node_map[step_no])``
            直接获取并调用对应的节点函数（``_node_step1`` ~ ``_node_step5``）。节点函数
            内部自行处理跨进程锁、LoopState、超时、审计日志、告警通知等逻辑。

            因此 ``_build_graph()`` 中定义的条件路由（``_route_after_step1`` /
            ``_route_after_step3``）和并行 fan_out（``_node_fan_out``）在当前模式下
            **不会生效**，步骤间的推进由外部调用方（API/定时任务）通过 ``run_step(step_no)``
            串行驱动。

            图定义（``_build_graph``）保留用于未来完整运行模式（一次性走完 5 步），届时
            需在 ``run_step`` 中改为 ``graph.invoke({"step_no": step_no, ...})``，节点内
            的 ``interrupt`` 才会生效。
        """
        state = ClosedLoop._prepare_state(step_no, actor)
        node_map = {1: "_node_step1", 2: "_node_step2", 3: "_node_step3", 4: "_node_step4", 5: "_node_step5"}
        node_fn = getattr(ClosedLoop, node_map[step_no])
        return node_fn(state)

    @staticmethod
    def _run_step_legacy(step_no, actor="system"):
        """降级执行：原有 if-elif 硬编码逻辑（langgraph 不可用时使用）"""
        run_id = ClosedLoop.get_current_run_id()
        step_name = ClosedLoop.STEPS[step_no - 1][1]

        # 获取跨进程锁
        locked, release = _acquire_lock(run_id, step_no)
        if not locked:
            return {"error": f"步骤{step_no}正在执行中（其他进程持有锁），请勿重复触发", "step": step_no}

        try:
            rec = LoopState.query.filter_by(run_id=run_id, step=step_no).first()
            if rec and rec.status == "done":
                return {"error": f"步骤{step_no}已完成，请从下一步继续", "step": step_no}

            if not rec:
                rec = LoopState(run_id=run_id, step=step_no, step_name=step_name, status="running")
                rec.started_at = datetime.now()
                db.session.add(rec)
            else:
                rec.status = "running"
                rec.started_at = datetime.now()
            db.session.commit()

            try:
                timeout = ClosedLoop._get_timeout()

                @with_timeout(timeout)
                def _execute(cancel_event=None):
                    # P0-2 fix: check cancel_event before step execution
                    if cancel_event is not None and cancel_event.is_set():
                        raise TimeoutError("Step cancelled before execution")
                    if step_no == 1:
                        detail = ClosedLoop._step1_generate_suggestions()
                    elif step_no == 3:
                        detail = ClosedLoop._step3_rpa_order(actor)
                    elif step_no == 4:
                        detail = ClosedLoop._step4_erp_receive(actor)
                    elif step_no == 5:
                        detail = ClosedLoop._step5_fde_refresh(actor)
                    else:
                        detail = None
                    # P0-2 fix: if cancelled during execution, rollback instead of commit
                    if cancel_event is not None and cancel_event.is_set():
                        db.session.rollback()
                        raise TimeoutError("Step cancelled during execution")
                    return detail

                # 步骤2特殊处理
                if step_no == 2:
                    rec.status = "pending"
                    rec.detail = "等待人工审核建议"
                    rec.finished_at = datetime.now()
                    db.session.commit()
                    db.session.add(
                        AuditLog(
                            actor=actor,
                            action="loop_step_wait",
                            target_type="loop",
                            target_id=str(run_id),
                            detail=f"步骤{step_no}等待人工审核",
                        )
                    )
                    db.session.commit()
                    return {"step": 2, "message": "请前往审核看板处理待审建议", "need_manual": True}

                detail = _execute()
                rec.status = "done"
                rec.detail = detail
                rec.finished_at = datetime.now()
                db.session.commit()
                db.session.add(
                    AuditLog(
                        actor=actor,
                        action="loop_step_done",
                        target_type="loop",
                        target_id=f"{run_id}-{step_no}",
                        detail=detail[:500] if detail else "",
                    )
                )
                db.session.commit()
                return {"step": step_no, "status": "done", "detail": detail}

            except TimeoutError as e:
                db.session.rollback()
                rec = LoopState.query.filter_by(run_id=run_id, step=step_no).first()
                if rec:
                    rec.status = "failed"
                    rec.detail = f"超时：{str(e)}"
                    rec.finished_at = datetime.now()
                    db.session.commit()
                db.session.add(
                    AuditLog(
                        actor=actor,
                        action="loop_step_timeout",
                        target_type="loop",
                        target_id=f"{run_id}-{step_no}",
                        detail=str(e)[:500],
                    )
                )
                db.session.commit()
                # P1-2: 失败时触发告警通知
                try:
                    from services.notifier import notifier

                    notifier.send_alert(
                        title=f"闭环步骤{step_no}超时",
                        message=f"Run #{run_id} step {step_no} 超时：{str(e)[:200]}",
                        level="error",
                        context={"run_id": run_id, "step": step_no},
                    )
                except Exception:
                    pass
                # P0-2 fix: release session after timeout to prevent daemon thread
                # from using the shared db.session (SQLAlchemy sessions are not thread-safe)
                db.session.remove()
                return {"step": step_no, "error": f"步骤超时：{e}"}
            except Exception as e:
                logger.exception("步骤%s执行失败", step_no)
                db.session.rollback()
                rec = LoopState.query.filter_by(run_id=run_id, step=step_no).first()
                if rec:
                    rec.status = "failed"
                    rec.detail = str(e)[:500]
                    rec.finished_at = datetime.now()
                    db.session.commit()
                db.session.add(
                    AuditLog(
                        actor=actor,
                        action="loop_step_failed",
                        target_type="loop",
                        target_id=f"{run_id}-{step_no}",
                        detail=str(e)[:500],
                    )
                )
                db.session.commit()
                # P1-2: 失败时触发告警通知
                try:
                    from services.notifier import notifier

                    notifier.send_alert(
                        title=f"闭环步骤{step_no}执行失败",
                        message=f"Run #{run_id} step {step_no} 异常：{str(e)[:200]}",
                        level="error",
                        context={"run_id": run_id, "step": step_no, "error": str(e)[:100]},
                    )
                except Exception:
                    pass
                return {"step": step_no, "error": str(e)}
        finally:
            release()

    # ==================== 回滚（带业务副作用补偿） ====================

    @staticmethod
    def rollback_step(step_no, actor="system"):
        """回滚指定步骤，并补偿已产生的业务副作用。

        P0-6: 入口加跨进程锁，防止与步骤执行并发冲突。获取锁失败说明步骤正在执行。
        补偿策略：
        - 步骤3 RPA下单：取消 draft/confirmed 状态的采购单（received 不可逆，仅记审计）
        - 步骤4 ERP入库：已入库的库存无法直接回退（会破坏移动加权平均成本），
                         仅标记状态 + 审计，需人工冲红
        - 步骤5 FDE刷新：删除本次生成的 ADS 数据（按审计日志记录的 ID 精确关联）
        """
        run_id = ClosedLoop.get_current_run_id()
        # P0-6: 回滚前获取跨进程锁，避免与步骤执行并发
        locked, release = _acquire_lock(run_id, step_no)
        if not locked:
            return {"error": f"步骤{step_no}正在执行中（其他进程持有锁），无法回滚", "step": step_no}

        try:
            rec = LoopState.query.filter_by(run_id=run_id, step=step_no).first()
            if not rec:
                return {"error": "步骤无记录"}
            if rec.status == "rolled_back":
                return {"error": "步骤已回滚，请勿重复操作"}
            if rec.status == "running":
                return {"error": "步骤正在执行中，无法回滚"}

            compensations = []
            try:
                if step_no == 3:
                    compensations = ClosedLoop._compensate_step3(run_id, actor)
                elif step_no == 4:
                    compensations = ClosedLoop._compensate_step4(run_id, actor)
                elif step_no == 5:
                    compensations = ClosedLoop._compensate_step5(run_id, actor)

                old_status = rec.status
                rec.status = "rolled_back"
                rec.detail = f"已回滚（原状态：{old_status}）；补偿：{compensations}"
                rec.finished_at = datetime.now()
                db.session.commit()

                db.session.add(
                    AuditLog(
                        actor=actor,
                        action="loop_step_rollback",
                        target_type="loop",
                        target_id=f"{run_id}-{step_no}",
                        detail=f"回滚步骤{step_no}，原状态{old_status}，补偿 {len(compensations)} 项",
                    )
                )
                db.session.commit()
                return {
                    "step": step_no,
                    "status": "rolled_back",
                    "compensations": compensations,
                }
            except Exception as e:
                db.session.rollback()
                logger.exception("步骤%s回滚失败", step_no)
                db.session.add(
                    AuditLog(
                        actor=actor,
                        action="loop_rollback_failed",
                        target_type="loop",
                        target_id=f"{run_id}-{step_no}",
                        detail=f"回滚失败：{e}",
                    )
                )
                db.session.commit()
                return {"step": step_no, "error": f"回滚失败：{e}"}
        finally:
            release()

    @staticmethod
    def _compensate_step3(run_id, actor):
        """补偿步骤3（RPA下单）：取消本轮产生的 draft/confirmed 采购单。
        received 状态不可逆，仅记审计。
        """
        from models.erp import PurchaseOrder
        from services.erp_service import ERPService

        erp = ERPService()
        compensations = []
        # 通过 audit_log 反查本轮 step3 创建的采购单 ID
        step3_logs = AuditLog.query.filter_by(
            action="loop_step_done",
            target_type="loop",
            target_id=f"{run_id}-3",
        ).all()
        # 直接查本轮新增的采购单（按 run_id 关联的 suggestion_id 反查）
        sug_ids = []
        _step3_detail = step3_logs[0].detail if step3_logs else ""  # noqa: F841
        # 从 detail 中提取 PO ID（detail 形如 '下单 产品A→供应商(...); ...'）
        # 更可靠的方式：通过 suggestion_id 关联
        from models.aigc import Suggestion

        ordered_sugs = Suggestion.query.filter_by(status="ordered", run_id=run_id).all()
        sug_ids = [s.id for s in ordered_sugs]

        pos_to_cancel = (
            PurchaseOrder.query.filter(
                PurchaseOrder.suggestion_id.in_(sug_ids),
                PurchaseOrder.status.in_(["draft", "confirmed"]),
            ).all()
            if sug_ids
            else []
        )

        for po in pos_to_cancel:
            try:
                erp.cancel_purchase_order(po.id, actor=actor)
                compensations.append(f"取消采购单 {po.order_no}")
            except ValueError as e:
                compensations.append(f"采购单 {po.order_no} 无法取消：{e}")
        return compensations

    @staticmethod
    def _compensate_step4(run_id, actor):
        """补偿步骤4（ERP入库）：已入库库存不可直接回退（破坏移动加权平均成本）。
        仅记审计 + 提示人工冲红。
        """
        # 通过 audit_log 查本轮 step4 入库的采购单
        _step4_logs = AuditLog.query.filter_by(  # noqa: F841
            action="loop_step_done",
            target_type="loop",
            target_id=f"{run_id}-4",
        ).all()
        return ["已入库库存不可自动回退（会破坏移动加权平均成本），请人工冲红处理"]

    @staticmethod
    def _compensate_step5(run_id, actor):
        """P0-5: 补偿步骤5（FDE刷新）：按本轮 step5 记录的 ADS 记录 ID 精确删除。

        通过审计日志 action=loop_step5_artifacts 中记录的 ID 列表删除，
        避免按 date.today() 误删当日历史数据。
        """
        from models.warehouse import AdsDailyOpsReport, AdsReplenishmentSuggest

        compensations = []
        # 查找本轮 step5 记录的 ADS 记录 ID（由 _step5_fde_refresh 写入）
        artifacts_log = (
            AuditLog.query.filter_by(
                action="loop_step5_artifacts",
                target_type="loop",
                target_id=f"{run_id}-5",
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        if not artifacts_log or not artifacts_log.detail:
            # 兼容历史数据：未记录 ID 时仅记审计，不按 date 删除（避免误删）
            return ["未找到本轮 step5 artifacts 记录，跳过自动删除（请人工核查 ADS 数据）"]

        try:
            ids = json.loads(artifacts_log.detail)
        except (ValueError, TypeError):
            return ["step5 artifacts 记录解析失败，跳过自动删除"]

        rep_ids = ids.get("ads_replenishment_ids", [])
        rpt_ids = ids.get("ads_daily_ops_ids", [])

        rep_n = 0
        rpt_n = 0
        if rep_ids:
            rep_n = AdsReplenishmentSuggest.query.filter(AdsReplenishmentSuggest.id.in_(rep_ids)).delete(
                synchronize_session=False
            )
        if rpt_ids:
            rpt_n = AdsDailyOpsReport.query.filter(AdsDailyOpsReport.id.in_(rpt_ids)).delete(synchronize_session=False)
        db.session.commit()
        if rep_n:
            compensations.append(f"删除本轮 ADS 补货建议 {rep_n} 条（按 ID 精确删除）")
        if rpt_n:
            compensations.append(f"删除本轮 ADS 经营日报 {rpt_n} 条（按 ID 精确删除）")
        if not compensations:
            compensations.append("本轮 step5 无新增 ADS 数据需要清理")
        return compensations

    # ==================== 各步骤实现 ====================

    @staticmethod
    def _step1_generate_suggestions():
        AIGCService, _, _, WarehouseService = ClosedLoop._services()
        WarehouseService().run_full_pipeline()
        results = AIGCService().generate_suggestions()
        if results:
            parts = []
            for s in results:
                pname = getattr(s, "product_name", "?")
                qty = getattr(s, "suggested_qty", 0)
                parts.append(f"{pname}{qty}件")
            return f"生成 {len(results)} 条补货建议：" + "；".join(parts)
        return "当前无需补货（所有SKU库存充足）"

    @staticmethod
    def _step3_rpa_order(actor="system"):
        from models.aigc import Suggestion

        _, RPAService, _, _ = ClosedLoop._services()
        approved = Suggestion.query.filter_by(status="approved").all()
        if not approved:
            return "无已审核建议，跳过下单（请先在步骤2审核建议）"
        import time as _time

        run_id = ClosedLoop.get_current_run_id()
        rpa = RPAService()
        results = []
        for sug in approved:
            success = False
            # P1-4: 每条下单最多重试 2 次（共 3 次尝试），间隔 1 秒
            for attempt in range(3):
                try:
                    result = rpa.place_supplier_order(
                        {"id": sug.id, "product_id": sug.product_id, "suggested_qty": sug.suggested_qty}
                    )
                    if result["status"] == "ok":
                        sug.status = "ordered"
                        sug.run_id = run_id
                        results.append(f'{sug.product_name}→{result["supplier"]}({result["ext_order_no"]})')
                        success = True
                        break
                    elif attempt < 2:
                        _time.sleep(1)
                        continue
                    else:
                        results.append(f'{sug.product_name}下单失败：{result["reason"]}')
                except Exception as e:
                    if attempt < 2:
                        _time.sleep(1)
                        continue
                    results.append(f"{sug.product_name}异常：{e}")
            if not success and not any(sug.product_name in r for r in results):
                results.append(f"{sug.product_name}下单失败：重试 3 次均未成功")
        db.session.commit()
        return "下单 " + "；".join(results)

    @staticmethod
    def _step4_erp_receive(actor="system"):
        from extensions import db as _db
        from models.erp import Product, PurchaseOrder

        _, _, ERPService, _ = ClosedLoop._services()
        confirmed_pos = PurchaseOrder.query.filter_by(status="confirmed").all()
        if not confirmed_pos:
            return "无待收货采购单，跳过入库"
        erp = ERPService()
        results = []
        for po in confirmed_pos:
            erp.receive_purchase_order(po.id)
            prod = _db.session.get(Product, po.product_id)
            results.append(f'{prod.name if prod else "未知"}入库{po.qty}件')
        return "入库 " + "；".join(results)

    @staticmethod
    def _step5_fde_refresh(actor="system"):
        """P0-5: 执行 FDE 刷新，并记录本次写入的 ADS 记录 ID 到审计日志，供回滚按 ID 精确删除。"""
        from models.warehouse import AdsDailyOpsReport, AdsReplenishmentSuggest

        AIGCService, _, _, WarehouseService = ClosedLoop._services()
        # 记录 step5 执行前的 ADS 最大 ID，用于定位本次写入的记录
        pre_max_rep = db.session.query(db.func.max(AdsReplenishmentSuggest.id)).scalar() or 0
        pre_max_rpt = db.session.query(db.func.max(AdsDailyOpsReport.id)).scalar() or 0

        stats = WarehouseService().run_full_pipeline()
        AIGCService().generate_daily_report()

        # 查询本次 step5 新增的 ADS 记录 ID，回滚时按 ID 精确删除（避免误删当日历史数据）
        new_rep_ids = [
            r[0]
            for r in db.session.query(AdsReplenishmentSuggest.id).filter(AdsReplenishmentSuggest.id > pre_max_rep).all()
        ]
        new_rpt_ids = [
            r[0] for r in db.session.query(AdsDailyOpsReport.id).filter(AdsDailyOpsReport.id > pre_max_rpt).all()
        ]
        run_id = ClosedLoop.get_current_run_id()
        db.session.add(
            AuditLog(
                actor=actor,
                action="loop_step5_artifacts",
                target_type="loop",
                target_id=f"{run_id}-5",
                detail=json.dumps({"ads_replenishment_ids": new_rep_ids, "ads_daily_ops_ids": new_rpt_ids}),
            )
        )
        db.session.commit()

        # 兼容嵌套返回结构
        def _rows(v):
            if isinstance(v, dict):
                return v.get("rows", 0)
            return v

        return (
            f'ODS+{_rows(stats["ods"])} → DWD+{_rows(stats["dwd"])} → '
            f'DWS+{_rows(stats["dws"])} → ADS+{_rows(stats["ads"])}，指标已刷新，日报已生成'
        )

    @staticmethod
    def reset(actor="system"):
        """P0-4: 软结束当前轮次——将当前 run 的步骤标记为 archived，保留历史记录。

        避免全表删除导致历史运行记录丢失。get_current_run_id 会跳过 archived 记录，
        下次调用自动开启新一轮 run_id。
        """
        run_id = ClosedLoop.get_current_run_id()
        records = LoopState.query.filter_by(run_id=run_id).all()
        now = datetime.now()
        for rec in records:
            rec.status = "archived"
            rec.detail = (rec.detail or "") + f"（已由 {actor} 归档重置）"
            rec.finished_at = now
        db.session.commit()
        db.session.add(
            AuditLog(
                actor=actor,
                action="loop_reset",
                target_type="loop",
                target_id=str(run_id),
                detail=f"闭环 run #{run_id} 已归档重置（保留历史记录）",
            )
        )
        db.session.commit()
        return f"闭环 run #{run_id} 已重置（历史记录已归档保留）"

    @staticmethod
    def check_auto_trigger():
        from flask import current_app

        if not current_app.config.get("LOOP_AUTO_TRIGGER"):
            return {"triggered": False, "reason": "自动触发未启用"}
        status = ClosedLoop.get_status()
        if status["current_step"] <= 5 and not all(s["status"] == "done" for s in status["steps"]):
            return {"triggered": False, "reason": "当前轮次未完成"}
        from models.erp import Product

        low = Product.query.filter(Product.stock_qty < Product.safety_stock).count()
        if low > 0:
            return {"triggered": True, "reason": f"检测到{low}个低库存SKU", "low_count": low}
        return {"triggered": False, "reason": "无低库存"}

    @staticmethod
    def check_auto_trigger_with_anomaly(anomalies):
        """改进9：基于时序异常检测结果触发闭环。

        :param anomalies: list[dict] AnomalyDetector 检测出的 critical 异常列表
        :return: {triggered, reason, loop_run_id, anomaly_count}
        - 自动触发闭环 step 1（生成补货建议）
        - 异常产品优先纳入补货建议
        - 记录审计日志
        """
        from flask import current_app

        if not current_app.config.get("LOOP_AUTO_TRIGGER"):
            return {"triggered": False, "reason": "自动触发未启用（LOOP_AUTO_TRIGGER=False）"}

        if not anomalies:
            return {"triggered": False, "reason": "无 critical 异常"}

        # 检查当前闭环状态，若上一轮未完成则不触发（避免冲突）
        status = ClosedLoop.get_status()
        if status["current_step"] <= 5 and not all(s["status"] == "done" for s in status["steps"]):
            return {"triggered": False, "reason": "当前轮次未完成，跳过自动触发"}

        # 重置闭环，开启新一轮
        try:
            ClosedLoop.reset(actor="anomaly_detector")
            run_result = ClosedLoop.run_step(1, actor="anomaly_detector")

            # 记录审计日志
            db.session.add(
                AuditLog(
                    actor="anomaly_detector",
                    action="loop_auto_trigger_by_anomaly",
                    target_type="loop",
                    detail=f"时序异常检测触发闭环补货：{len(anomalies)} 个 critical 异常产品",
                )
            )
            db.session.commit()

            logger.info("[ClosedLoop] 异常检测触发闭环成功，异常数=%d", len(anomalies))
            return {
                "triggered": True,
                "reason": f"检测到 {len(anomalies)} 个 critical 时序异常，已触发闭环补货",
                "loop_run_id": ClosedLoop.get_status().get("run_id"),
                "anomaly_count": len(anomalies),
                "run_step_result": run_result,
            }
        except Exception as e:
            logger.error("[ClosedLoop] 异常检测触发闭环失败: %s", e)
            db.session.rollback()
            return {"triggered": False, "reason": f"触发失败：{e}", "error": str(e)}
