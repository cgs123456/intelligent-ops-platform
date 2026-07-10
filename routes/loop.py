"""闭环路由 Blueprint + SSE 进度推送（含心跳）"""
import json
import time

from flask import Blueprint, Response, jsonify, request, stream_with_context

from services.closed_loop import ClosedLoop
from services.rbac import require_permission

bp = Blueprint('loop', __name__, url_prefix='/api/v1/loop')


@bp.route('/status')
@require_permission('loop:read')
def status():
    return jsonify(ClosedLoop.get_status())


@bp.route('/run-step', methods=['POST'])
@require_permission('loop:run')
def run_step():
    step = request.get_json(force=True, silent=True) or {}
    step_no = step.get('step')
    actor = step.get('actor', 'system')
    return jsonify(ClosedLoop.run_step(step_no, actor))


@bp.route('/run-step-async', methods=['POST'])
@require_permission('loop:run')
def run_step_async():
    """异步执行闭环步骤（通过 Celery）
    Body: {"step": 1, "actor": "system"}
    返回: {"task_id": "xxx"}，需轮询 /api/v1/loop/task/<task_id> 查询结果
    """
    data = request.get_json(force=True, silent=True) or {}
    step_no = data.get('step')
    actor = data.get('actor', 'system')
    if not isinstance(step_no, int) or step_no < 1 or step_no > 5:
        return jsonify({'error': 'step 参数无效，需 1-5'}), 400
    try:
        from extensions import celery_app
        from tasks import run_loop_step_async
        if celery_app is None:
            return jsonify({'error': 'Celery 未启用'}), 503
        result = run_loop_step_async.delay(step_no, actor)
        return jsonify({'task_id': result.id, 'status': 'queued'})
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        return jsonify({'error': f'异步任务提交失败：{e}'}), 500


@bp.route('/task/<task_id>')
@require_permission('loop:read')
def get_task_status(task_id):
    """查询异步任务状态"""
    try:
        from celery.result import AsyncResult

        from extensions import celery_app
        if celery_app is None:
            return jsonify({'error': 'Celery 未启用'}), 503
        r = AsyncResult(task_id, app=celery_app)
        return jsonify({
            'task_id': task_id,
            'status': r.status,  # PENDING / STARTED / SUCCESS / FAILURE / RETRY
            'ready': r.ready(),
            'result': r.result if r.successful() else None,
            'error': str(r.result) if r.failed() else None,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/rollback', methods=['POST'])
@require_permission('loop:rollback')  # P0-9: 独立权限
def rollback():
    data = request.get_json(force=True, silent=True) or {}
    step = data.get('step')
    actor = data.get('actor', 'system')
    if not isinstance(step, int) or step < 1 or step > 5:
        return jsonify({'error': 'step 参数无效，需 1-5'}), 400
    return jsonify(ClosedLoop.rollback_step(step, actor))


@bp.route('/reset', methods=['POST'])
@require_permission('loop:reset')  # P0-9: 独立权限，仅 admin
def reset():
    data = request.get_json(force=True, silent=True) or {}
    actor = data.get('actor', 'system')
    return jsonify(ClosedLoop.reset(actor))


@bp.route('/auto-trigger-check')
@require_permission('loop:read')
def auto_trigger():
    return jsonify(ClosedLoop.check_auto_trigger())


@bp.route('/stream')
@require_permission('loop:read')
def stream():
    """SSE 进度流：推送闭环状态变化，每 15 秒发心跳防断连。"""
    def generate():
        last_status = None
        last_heartbeat = time.time()
        for _ in range(120):  # 最多 120 秒
            status = ClosedLoop.get_status()
            if status != last_status:
                yield f'data: {json.dumps(status, ensure_ascii=False)}\n\n'
                last_status = status
                cur = status['current_step']
                steps = status['steps']
                if cur > 5 or all(s['status'] == 'done' for s in steps):
                    break
            # 心跳：15 秒无状态变化发 comment 保活
            now = time.time()
            if now - last_heartbeat > 15:
                yield ': heartbeat\n\n'
                last_heartbeat = now
            time.sleep(1)
        yield 'data: {"event":"end"}\n\n'
    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no',
                 'Connection': 'keep-alive'}
    )
