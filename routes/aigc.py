"""AIGC 路由 Blueprint

P0-3: 新增 SSE 流式查询接口 /api/v1/aigc/query-stream
P1-1 架构: 已移除内嵌 Celery 任务定义，统一到 tasks.py
"""
import json
import time
import uuid

from flask import Blueprint, Response, jsonify, request, stream_with_context

from services.aigc_service import AIGCService
from services.rbac import require_permission

bp = Blueprint('aigc', __name__, url_prefix='/api/v1/aigc')


@bp.route('/suggestions')
@require_permission('aigc:read')
def suggestions():
    return jsonify(AIGCService().get_pending_suggestions())


@bp.route('/generate-suggestions', methods=['POST'])
@require_permission('aigc:review')
def gen_suggestions():
    results = AIGCService().generate_suggestions()
    count = len(results) if isinstance(results, list) else 0
    return jsonify({'count': count, 'results': results})


@bp.route('/review', methods=['POST'])
@require_permission('aigc:review')
def review():
    """审核单条补货建议。
    支持可选 final_qty：审核时修改最终采购数量（P1-3）。
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        AIGCService().review_suggestion(
            data['id'], data['action'],
            data.get('note', ''), data.get('reviewer', 'system'),
            final_qty=data.get('final_qty'),
        )
        return jsonify({
            'status': 'ok', 'suggestion_id': data['id'], 'new_status': data['action'],
            'final_qty': data.get('final_qty'),
        })
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@bp.route('/batch-review', methods=['POST'])
@require_permission('aigc:review')
def batch_review():
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get('ids', [])
    action = data.get('action', 'approve')
    results = []
    for sid in ids:
        try:
            AIGCService().review_suggestion(sid, action, reviewer=data.get('reviewer', 'system'))
            results.append({'id': sid, 'status': 'ok', 'new_status': action})
        except ValueError as e:
            results.append({'id': sid, 'status': 'fail', 'error': str(e)})
    return jsonify({'results': results})


@bp.route('/report')
@require_permission('aigc:read')
def report():
    from models.aigc import DailyReport
    latest = DailyReport.query.order_by(DailyReport.created_at.desc()).first()
    return jsonify({'text': latest.report_text if latest else '暂无日报，请先运行 FDE 刷新 + AIGC 生成日报'})


@bp.route('/generate-report', methods=['POST'])
@require_permission('fde:run')
def gen_report():
    """改进10：生成 LLM 经营日报（昨日回顾+趋势+风险+建议）。
    查询参数：
        push: 可选，true 时通过 Notifier 推送多渠道告警
        date: 可选，YYYY-MM-DD，默认今天
    """
    push = request.args.get('push', '').lower() == 'true'
    date_str = request.args.get('date')
    dt = None
    if date_str:
        from datetime import datetime as _dt
        try:
            dt = _dt.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'error': '日期格式错误，需 YYYY-MM-DD'}), 400
    report = AIGCService().generate_daily_report(dt=dt, push=push)
    if report is None:
        return jsonify({'error': 'ADS 日报数据不存在，请先运行 FDE 刷新'}), 404
    return jsonify({
        'text': report.report_text,
        'dt': str(report.dt),
        'pushed': push,
    })


@bp.route('/query', methods=['POST'])
@require_permission('aigc:read')  # P0: 防止 LLM 配额被盗用
def query():
    data = request.get_json(force=True, silent=True) or {}
    q = data.get('question', '').strip()
    # P0-8: 输入校验
    if not q:
        return jsonify({'error': '问题不能为空'}), 400
    if len(q) > 500:
        return jsonify({'error': '问题长度不能超过 500 字'}), 400
    session_id = data.get('session_id')
    result = AIGCService().natural_language_query(q, session_id)
    if isinstance(result, dict):
        return jsonify(result)
    return jsonify({'answer': result, 'session_id': session_id or str(uuid.uuid4())})


@bp.route('/query-stream', methods=['POST'])
@require_permission('aigc:read')
def query_stream():
    """P0-3: SSE 流式输出智能问答
    复用后端 SSE 基础设施，前端边接收边渲染，避免长等待。
    Body: {"question": "...", "session_id": "..."}
    """
    data = request.get_json(force=True, silent=True) or {}
    q = data.get('question', '').strip()
    if not q:
        return jsonify({'error': '问题不能为空'}), 400
    if len(q) > 500:
        return jsonify({'error': '问题长度不能超过 500 字'}), 400
    session_id = data.get('session_id')

    def generate():
        svc = AIGCService()
        try:
            result = svc.natural_language_query(q, session_id)
            answer = result.get('answer', '') if isinstance(result, dict) else str(result)
            new_sid = result.get('session_id', session_id) if isinstance(result, dict) else session_id

            # 先发 session_id，让前端建立会话
            yield f'data: {json.dumps({"type": "session", "session_id": new_sid}, ensure_ascii=False)}\n\n'

            # 分段流式输出（按句号/换行切分，模拟流式效果）
            chunks = _split_text_chunks(answer, chunk_size=40)
            for chunk in chunks:
                yield f'data: {json.dumps({"type": "chunk", "text": chunk}, ensure_ascii=False)}\n\n'
                time.sleep(0.05)  # 50ms 间隔，前端能看到流式效果

            yield f'data: {json.dumps({"type": "done", "route": result.get("route") if isinstance(result, dict) else None}, ensure_ascii=False)}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"type": "error", "error": str(e)[:200]}, ensure_ascii=False)}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'},
    )


def _split_text_chunks(text, chunk_size=40):
    """把文本按 chunk_size 字符切分，尽量在句号/逗号边界切"""
    if not text:
        return ['']
    chunks = []
    buf = ''
    for ch in text:
        buf += ch
        if len(buf) >= chunk_size and ch in '。.！!？?，,；;\n':
            chunks.append(buf)
            buf = ''
    if buf:
        chunks.append(buf)
    return chunks


@bp.route('/chat-history/<session_id>')
@require_permission('aigc:read')
def chat_history(session_id):
    from models.aigc import ChatHistory
    msgs = ChatHistory.query.filter_by(session_id=session_id).order_by(
        ChatHistory.created_at.desc()
    ).limit(20).all()
    return jsonify([{
        'role': m.role, 'content': m.content,
        'time': m.created_at.strftime('%Y-%m-%d %H:%M:%S')
    } for m in reversed(msgs)])


@bp.route('/feedback-stats')
@require_permission('aigc:read')
def feedback_stats():
    return jsonify(AIGCService().get_feedback_stats())


# ---- 异步任务（统一从 tasks.py 调用，不再在此文件混入 Celery 任务定义）----
# 如需异步生成日报/建议，请使用：
#   from tasks import generate_daily_report_async, generate_suggestions_async
#   result = generate_daily_report_async.delay()
#   task_id = result.id


# ---- 改进7：多 Agent 采购博弈 ----


@bp.route('/negotiate', methods=['POST'])
@require_permission('aigc:review')
def negotiate():
    """多 Agent 采购博弈：买方 Agent + 2 个供应商 Agent 竞价。

    请求体：
        product_id: int   必填
        suggested_qty: int 可选，建议采购量
        supplier_ids: list[int] 可选，指定候选供应商（默认取全部启用供应商）
    返回：
        buyer_demand / quotes / best / negotiation_log
    """
    from extensions import db
    from models.erp import Product, Supplier
    from services.multiagent import MultiAgentNegotiator

    data = request.get_json(force=True, silent=True) or {}
    product_id = data.get('product_id')
    if not product_id:
        return jsonify({'error': 'product_id 必填'}), 400

    product = db.session.get(Product, product_id)
    if not product:
        return jsonify({'error': f'产品 {product_id} 不存在'}), 404

    # 候选供应商
    supplier_ids = data.get('supplier_ids')
    if supplier_ids:
        suppliers = (
            db.session.query(Supplier)
            .filter(Supplier.id.in_(supplier_ids))
            .filter(Supplier.is_active.is_(True))
            .all()
        )
    else:
        suppliers = (
            db.session.query(Supplier)
            .filter(Supplier.is_active.is_(True))
            .all()
        )

    negotiator = MultiAgentNegotiator(db_session=db.session)
    result = negotiator.negotiate(
        product, suppliers,
        suggested_qty=data.get('suggested_qty'),
    )
    return jsonify(result)

# ---- 改进8：Data Agent Text2SQL 全链路 ----


@bp.route('/data-query', methods=['POST'])
@require_permission('aigc:read')
def data_query():
    """Data Agent：NL → SQL（LLM 生成）→ AST 校验 → 真执行 → NL 回复。

    请求体：
        question: str    必填，自然语言问题
        session_id: str  可选，会话 ID（支持多轮）
    返回：
        session_id / route / sql / result / rows_count / answer
    """
    from services.data_agent import DataAgent
    data = request.get_json(force=True, silent=True) or {}
    question = data.get('question')
    if not question:
        return jsonify({'error': 'question 必填'}), 400
    agent = DataAgent()
    result = agent.query(question, session_id=data.get('session_id'))
    # result 含 _recorded 内部字段，移除后返回
    result.pop('_recorded', None)
    return jsonify(result)
