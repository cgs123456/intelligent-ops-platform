"""ERP 路由 Blueprint"""
from flask import Blueprint, jsonify, request

from services.erp_service import ERPService
from services.idempotency import idempotent
from services.rbac import require_permission

bp = Blueprint('erp', __name__, url_prefix='/api/v1/erp')


@bp.route('/inventory')
@require_permission('erp:read')
def inventory():
    return jsonify(ERPService().get_inventory_summary())


@bp.route('/orders')
@require_permission('erp:read')
def orders():
    # P0: limit 上限保护，防止拉爆内存
    limit = request.args.get('limit', 20, type=int)
    limit = max(1, min(limit, 100))
    return jsonify(ERPService().get_recent_orders(limit))


@bp.route('/account')
@require_permission('erp:read')
def account():
    return jsonify(ERPService().get_account_summary())


@bp.route('/warehouses')
@require_permission('erp:read')
def warehouses():
    from models.erp import Warehouse
    return jsonify([{
        'id': w.id, 'code': w.code, 'name': w.name, 'location': w.location
    } for w in Warehouse.query.filter_by(is_active=True).all()])


@bp.route('/returns', methods=['POST'])
@require_permission('erp:write')
@idempotent()
def create_return():
    data = request.get_json(force=True, silent=True) or {}
    # P0: 入参校验
    required = ['original_order_no', 'product_id', 'qty']
    for k in required:
        if k not in data or data[k] is None:
            return jsonify({'error': f'参数 {k} 不能为空'}), 400
    if not isinstance(data['qty'], (int, float)) or data['qty'] <= 0:
        return jsonify({'error': 'qty 必须为正数'}), 400
    try:
        ro = ERPService().create_return_order(
            original_order_no=data['original_order_no'],
            product_id=data['product_id'],
            qty=data['qty'],
            refund_amount=data.get('refund_amount', 0),
            reason=data.get('reason', '')
        )
        return jsonify({'status': 'ok', 'return_no': ro.return_no})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@bp.route('/transfers', methods=['POST'])
@require_permission('erp:write')
@idempotent()
def create_transfer():
    data = request.get_json(force=True, silent=True) or {}
    # P0: 入参校验
    required = ['product_id', 'qty']
    for k in required:
        if k not in data or data[k] is None:
            return jsonify({'error': f'参数 {k} 不能为空'}), 400
    if not isinstance(data['qty'], (int, float)) or data['qty'] <= 0:
        return jsonify({'error': 'qty 必须为正数'}), 400
    try:
        ERPService().transfer_stock(
            product_id=data['product_id'], qty=data['qty'],
            from_wh=data.get('from_wh'), to_wh=data.get('to_wh')
        )
        return jsonify({'status': 'ok'})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
