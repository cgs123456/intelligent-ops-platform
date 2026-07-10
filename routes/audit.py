"""审计日志路由 Blueprint

提供企业合规要求的审计日志查询能力：
- 支持按 actor / action / target_type / 时间范围过滤
- 分页查询
- 仅 admin 角色可访问
"""
from datetime import datetime

from flask import Blueprint, jsonify, request

from extensions import db
from models.system import AuditLog
from services.rbac import require_permission

bp = Blueprint('audit', __name__, url_prefix='/api/v1/audit')


def _parse_date(s, end_of_day=False):
    """解析 YYYY-MM-DD 为 datetime，end_of_day=True 时返回当日 23:59:59"""
    if not s:
        return None
    try:
        d = datetime.strptime(s, '%Y-%m-%d')
        if end_of_day:
            from datetime import timedelta
            return d.replace(hour=23, minute=59, second=59)
        return d
    except ValueError:
        return None


@bp.route('/logs')
@require_permission('audit:read')
def list_logs():
    """查询审计日志
    Query 参数：
      - page: 页码，默认 1
      - per_page: 每页条数，默认 50，最大 200
      - actor: 操作人筛选
      - action: 动作筛选（create/review/order/receive/etl/...）
      - target_type: 对象类型筛选
      - start: 起始日期 YYYY-MM-DD
      - end: 结束日期 YYYY-MM-DD
    """
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    per_page = max(1, min(per_page, 200))  # 上限 200

    q = db.session.query(AuditLog)

    actor = request.args.get('actor', '').strip()
    if actor:
        q = q.filter(AuditLog.actor == actor)

    action = request.args.get('action', '').strip()
    if action:
        q = q.filter(AuditLog.action == action)

    target_type = request.args.get('target_type', '').strip()
    if target_type:
        q = q.filter(AuditLog.target_type == target_type)

    start = _parse_date(request.args.get('start', ''))
    if start:
        q = q.filter(AuditLog.created_at >= start)

    end = _parse_date(request.args.get('end', ''), end_of_day=True)
    if end:
        q = q.filter(AuditLog.created_at <= end)

    total = q.count()
    rows = (q.order_by(AuditLog.created_at.desc())
             .offset((page - 1) * per_page)
             .limit(per_page)
             .all())

    return jsonify({
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page,
        'items': [{
            'id': r.id,
            'actor': r.actor,
            'action': r.action,
            'target_type': r.target_type,
            'target_id': r.target_id,
            'detail': r.detail,
            'ip': r.ip,
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S') if r.created_at else None,
        } for r in rows],
    })


@bp.route('/stats')
@require_permission('audit:read')
def stats():
    """审计日志统计：按 action 聚合最近 30 天"""
    from datetime import datetime, timedelta

    from sqlalchemy import func
    since = datetime.now() - timedelta(days=30)
    rows = (db.session.query(AuditLog.action, func.count(AuditLog.id))
            .filter(AuditLog.created_at >= since)
            .group_by(AuditLog.action)
            .order_by(func.count(AuditLog.id).desc())
            .all())
    return jsonify({
        'since': since.strftime('%Y-%m-%d %H:%M:%S'),
        'items': [{'action': a, 'count': c} for a, c in rows],
    })
