"""闭环路由 Blueprint + SSE 进度推送（含心跳）"""

import json
import logging
import time

from flask import Blueprint, Response, jsonify, request, stream_with_context

from services.closed_loop import ClosedLoop
from services.rbac import get_actor_name, require_permission, verify_sse_auth

logger = logging.getLogger(__name__)

bp = Blueprint("loop", __name__, url_prefix="/api/v1/loop")


@bp.route("/status")
@require_permission("loop:read")
def status():
    return jsonify(ClosedLoop.get_status())


@bp.route("/run-step", methods=["POST"])
@require_permission("loop:run")
def run_step():
    step = request.get_json(force=True, silent=True) or {}
    step_no = step.get("step")
    # P0-11: 操作人以认证身份为准，body 传入的 actor 仅作无认证场景兜底（防伪造审计）
    actor = get_actor_name(step.get("actor"))
    # P0-10: 校验 step_no 为 1-5 整数
    if not isinstance(step_no, int) or step_no < 1 or step_no > 5:
        return jsonify({"error": "step 参数无效，需 1-5"}), 400
    return jsonify(ClosedLoop.run_step(step_no, actor))


@bp.route("/run-step-async", methods=["POST"])
@require_permission("loop:run")
def run_step_async():
    """异步执行闭环步骤（通过 Celery）
    Body: {"step": 1, "actor": "system"}
    返回: {"task_id": "xxx"}，需轮询 /api/v1/loop/task/<task_id> 查询结果
    """
    data = request.get_json(force=True, silent=True) or {}
    step_no = data.get("step")
    actor = get_actor_name(data.get("actor"))
    if not isinstance(step_no, int) or step_no < 1 or step_no > 5:
        return jsonify({"error": "step 参数无效，需 1-5"}), 400
    try:
        from extensions import celery_app
        from tasks import run_loop_step_async

        if celery_app is None:
            return jsonify({"error": "Celery 未启用"}), 503
        result = run_loop_step_async.delay(step_no, actor)
        return jsonify({"task_id": result.id, "status": "queued"})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception:
        logger.exception("异步任务提交失败 step=%s actor=%s", step_no, actor)
        return jsonify({"error": "异步任务提交失败，请联系管理员"}), 500


@bp.route("/task/<task_id>")
@require_permission("loop:read")
def get_task_status(task_id):
    """查询异步任务状态"""
    try:
        from celery.result import AsyncResult

        from extensions import celery_app

        if celery_app is None:
            return jsonify({"error": "Celery 未启用"}), 503
        r = AsyncResult(task_id, app=celery_app)
        return jsonify(
            {
                "task_id": task_id,
                "status": r.status,  # PENDING / STARTED / SUCCESS / FAILURE / RETRY
                "ready": r.ready(),
                "result": r.result if r.successful() else None,
                "error": str(r.result) if r.failed() else None,
            }
        )
    except Exception:
        logger.exception("查询任务状态失败 task_id=%s", task_id)
        return jsonify({"error": "查询任务状态失败，请联系管理员"}), 500


@bp.route("/rollback", methods=["POST"])
@require_permission("loop:rollback")  # P0-9: 独立权限
def rollback():
    data = request.get_json(force=True, silent=True) or {}
    step = data.get("step")
    actor = get_actor_name(data.get("actor"))
    if not isinstance(step, int) or step < 1 or step > 5:
        return jsonify({"error": "step 参数无效，需 1-5"}), 400
    return jsonify(ClosedLoop.rollback_step(step, actor))


@bp.route("/reset", methods=["POST"])
@require_permission("loop:reset")  # P0-9: 独立权限，仅 admin
def reset():
    data = request.get_json(force=True, silent=True) or {}
    actor = get_actor_name(data.get("actor"))
    return jsonify(ClosedLoop.reset(actor))


@bp.route("/auto-trigger-check")
@require_permission("loop:read")
def auto_trigger():
    return jsonify(ClosedLoop.check_auto_trigger())


@bp.route("/stream")
def stream():
    """SSE 进度流：推送闭环状态变化，每 15 秒发心跳防断连。
    P0-3: EventSource 不支持自定义 Header，改为从 query 参数 ?token=xxx 传 token。
    """
    # P0-3: SSE 认证 — 从 query 参数读取 token（EventSource 无法携带 Authorization Header）
    err = verify_sse_auth("loop:read")
    if err is not None:
        return err

    def generate():
        last_status = None
        last_heartbeat = time.time()
        for _ in range(24):  # P1-6: 降低查询频率到 5 秒，最多 120 秒
            status = ClosedLoop.get_status()
            if status != last_status:
                yield f"data: {json.dumps(status, ensure_ascii=False)}\n\n"
                last_status = status
                cur = status["current_step"]
                steps = status["steps"]
                if cur > 5 or all(s["status"] == "done" for s in steps):
                    break
            # 心跳：15 秒无状态变化发 comment 保活
            now = time.time()
            if now - last_heartbeat > 15:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            time.sleep(5)  # P1-6: 降低查询频率，减少 DB session 占用
        yield 'data: {"event":"end"}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
