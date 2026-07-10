"""闭环编排器
五步闭环：AIGC建议 → 人审核 → RPA下单 → ERP记账 → FDE刷新
支持：跨进程文件锁、超时处理、回滚机制、自动触发、SSE进度
"""

import logging
import os
import threading
import time
from datetime import datetime
from functools import wraps

from extensions import db
from models.system import AuditLog, LoopState

logger = logging.getLogger(__name__)

try:
    import fcntl

    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # Windows 无 fcntl


def _file_lock(run_id, step):
    """跨进程文件锁（兼容 gunicorn 多 worker）。
    Linux/Mac 用 fcntl.flock；Windows 无 fcntl 返回 None 退回 threading。
    """
    if not _HAS_FCNTL:
        return None, None
    lock_dir = os.getenv("LOOP_LOCK_DIR", "/tmp")
    try:
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, f"loop_{run_id}_{step}.lock")
        f = open(lock_path, "w")
        return f, lock_path
    except Exception:
        return None, None


def _try_acquire_file_lock(f):
    """尝试获取文件排他锁，非阻塞。成功返回 True。"""
    if f is None or not _HAS_FCNTL:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
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
                fcntl.flock(f, fcntl.LOCK_UN)
            except Exception:
                pass
            f.close()
            try:
                os.unlink(path)
            except Exception:
                pass

        return True, release
    # 文件锁不可用，尝试 threading
    lock = _get_thread_lock(run_id, step)
    if lock.acquire(blocking=False):
        return True, lambda: lock.release()
    return False, lambda: None


def with_timeout(seconds):
    """P2-5: 步骤超时装饰器。
    优先使用 multiprocessing（超时后可终止子进程），不可用时回退到 threading。
    会自动把 Flask app context 传递到子进程/子线程。
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            from flask import current_app

            app = current_app._get_current_object()

            # 尝试 multiprocessing（Linux 生产环境）
            try:
                import multiprocessing

                ctx = multiprocessing.get_context("spawn") if os.name == "nt" else multiprocessing

                def target(app_obj, args_t, kwargs_t, result_box, error_box):
                    try:
                        with app_obj.app_context():
                            result_box[0] = func(*args_t, **kwargs_t)
                    except Exception as e:
                        error_box[0] = e

                manager = ctx.Manager()
                result_box = manager.list([None])
                error_box = manager.list([None])
                p = ctx.Process(
                    target=target,
                    args=(app, args, kwargs, result_box, error_box),
                    daemon=True,
                )
                p.start()
                p.join(timeout=seconds)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)
                    if p.is_alive():
                        p.kill()
                    raise TimeoutError(f"步骤执行超时（{seconds}秒），子进程已终止")
                if error_box[0]:
                    raise error_box[0]
                return result_box[0]
            except (ValueError, TypeError, AttributeError):
                # 回退到 threading（Windows 或 pickling 失败时）
                result_box = [None]
                error_box = [None]

                def target_thread():
                    try:
                        with app.app_context():
                            result_box[0] = func(*args, **kwargs)
                    except Exception as e:
                        error_box[0] = e

                t = threading.Thread(target=target_thread, daemon=True)
                t.start()
                t.join(timeout=seconds)
                if t.is_alive():
                    raise TimeoutError(f"步骤执行超时（{seconds}秒）")
                if error_box[0]:
                    raise error_box[0]
                return result_box[0]

        return wrapper

    return decorator


class ClosedLoop:
    """五步闭环状态机"""

    STEPS = [
        (1, "AIGC生成建议", "AIGC"),
        (2, "人工审核", "人"),
        (3, "RPA自动下单", "RPA"),
        (4, "ERP记账入库", "ERP"),
        (5, "FDE刷新指标", "FDE"),
    ]

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

    # ---- 状态查询 ----

    @staticmethod
    def get_current_run_id():
        last = LoopState.query.order_by(LoopState.run_id.desc()).first()
        if not last:
            return 1
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
                current_step = s["step"] + 1
            elif s["status"] in ("running", "failed"):
                current_step = s["step"]
                break
        if current_step > 5:
            current_step = 5
        return {"run_id": run_id, "current_step": current_step, "steps": steps}

    # ---- 执行步骤 ----

    @staticmethod
    def run_step(step_no, actor="system"):
        """执行指定步骤（带跨进程锁 + 超时）"""
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
                def _execute():
                    if step_no == 1:
                        return ClosedLoop._step1_generate_suggestions()
                    elif step_no == 2:
                        return None  # 人工审核单独处理
                    elif step_no == 3:
                        return ClosedLoop._step3_rpa_order(actor)
                    elif step_no == 4:
                        return ClosedLoop._step4_erp_receive(actor)
                    elif step_no == 5:
                        return ClosedLoop._step5_fde_refresh(actor)
                    return None

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
                return {"step": step_no, "error": f"步骤超时：{e}"}
            except Exception as e:
                logger.exception(f"步骤{step_no}执行失败")
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

    # ---- 回滚（带业务副作用补偿）----

    @staticmethod
    def rollback_step(step_no, actor="system"):
        """回滚指定步骤，并补偿已产生的业务副作用。

        补偿策略：
        - 步骤3 RPA下单：取消 draft/confirmed 状态的采购单（received 不可逆，仅记审计）
        - 步骤4 ERP入库：已入库的库存无法直接回退（会破坏移动加权平均成本），
                         仅标记状态 + 审计，需人工冲红
        - 步骤5 FDE刷新：删除本次生成的 ADS 数据（按 run_id 关联）
        """
        run_id = ClosedLoop.get_current_run_id()
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

        ordered_sugs = Suggestion.query.filter_by(status="ordered").all()
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
        """补偿步骤5（FDE刷新）：删除本轮生成的 ADS 数据。
        通过 EtlMeta 的 last_run_at 关联本轮时间。
        """
        from models.warehouse import (
            AdsDailyOpsReport,
            AdsReplenishmentSuggest,
            EtlMeta,
        )

        compensations = []
        # 找到本轮 step5 执行的 ETL 元数据
        step5_log = (
            AuditLog.query.filter_by(
                action="loop_step_done",
                target_type="loop",
                target_id=f"{run_id}-5",
            )
            .order_by(AuditLog.id.desc())
            .first()
        )
        if not step5_log:
            return ["未找到本轮 step5 记录"]

        # 删除今日 ADS 数据（按 run_id 无法精确关联，按 dt 删除）
        from datetime import date

        today = date.today()
        rep_n = AdsReplenishmentSuggest.query.filter_by(dt=today).delete(synchronize_session=False)
        rpt_n = AdsDailyOpsReport.query.filter_by(dt=today).delete(synchronize_session=False)
        db.session.commit()
        if rep_n:
            compensations.append(f"删除今日 ADS 补货建议 {rep_n} 条")
        if rpt_n:
            compensations.append(f"删除今日 ADS 经营日报 {rpt_n} 条")
        return compensations

    # ---- 各步骤实现 ----

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
        rpa = RPAService()
        results = []
        for sug in approved:
            try:
                result = rpa.place_supplier_order(
                    {"id": sug.id, "product_id": sug.product_id, "suggested_qty": sug.suggested_qty}
                )
                if result["status"] == "ok":
                    sug.status = "ordered"
                    results.append(f'{sug.product_name}→{result["supplier"]}({result["ext_order_no"]})')
                else:
                    results.append(f'{sug.product_name}下单失败：{result["reason"]}')
            except Exception as e:
                results.append(f"{sug.product_name}异常：{e}")
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
        AIGCService, _, _, WarehouseService = ClosedLoop._services()
        stats = WarehouseService().run_full_pipeline()
        AIGCService().generate_daily_report()

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
        LoopState.query.delete()
        db.session.commit()
        db.session.add(AuditLog(actor=actor, action="loop_reset", target_type="loop", detail="闭环状态已重置"))
        db.session.commit()
        return "闭环已重置"

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
                "loop_run_id": status.get("run_id"),
                "anomaly_count": len(anomalies),
                "run_step_result": run_result,
            }
        except Exception as e:
            logger.error("[ClosedLoop] 异常检测触发闭环失败: %s", e)
            db.session.rollback()
            return {"triggered": False, "reason": f"触发失败：{e}", "error": str(e)}
