"""RPA 路由 Blueprint"""
from flask import Blueprint, jsonify, request

from services.rbac import require_permission
from services.rpa_service import RPAService

bp = Blueprint('rpa', __name__, url_prefix='/api/v1/rpa')


@bp.route('/quotes')
@require_permission('rpa:read')
def quotes():
    return jsonify(RPAService().collect_supplier_quotes())


@bp.route('/sync-orders', methods=['POST'])
@require_permission('rpa:write')
def sync_orders():
    return jsonify({'results': RPAService().sync_ecommerce_orders()})


@bp.route('/schedule/status')
@require_permission('rpa:read')
def schedule_status():
    return jsonify(RPAService().get_schedule_status())


@bp.route('/schedule/run', methods=['POST'])
@require_permission('rpa:write')
def run_scheduled():
    return jsonify(RPAService().run_scheduled_tasks())
