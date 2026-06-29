"""FDE 路由 Blueprint"""
import re
from datetime import datetime
from flask import Blueprint, jsonify, request
from services.warehouse_service import WarehouseService
from services.rbac import require_permission

bp = Blueprint('fde', __name__, url_prefix='/api/v1/fde')


@bp.route('/run', methods=['POST'])
@require_permission('fde:run')
def run_etl():
    return jsonify(WarehouseService().run_full_pipeline())


@bp.route('/stats')
@require_permission('fde:read')
def stats():
    return jsonify(WarehouseService().get_layer_stats())


@bp.route('/ads')
@require_permission('fde:read')
def ads():
    return jsonify(WarehouseService().get_ads_data())


@bp.route('/lineage')
@require_permission('fde:read')
def lineage():
    return jsonify(WarehouseService().get_lineage())


@bp.route('/data-quality')
@require_permission('fde:read')
def data_quality():
    return jsonify(WarehouseService().get_dq_report())


@bp.route('/run-dq', methods=['POST'])
@require_permission('fde:run')
def run_dq():
    return jsonify(WarehouseService().run_data_quality_tests())


@bp.route('/backfill', methods=['POST'])
@require_permission('fde:run')
def backfill():
    data = request.get_json(force=True, silent=True) or {}
    start_str = data.get('start', '')
    end_str = data.get('end', '')

    # P0-8: 严格日期格式校验
    date_pattern = r'^\d{4}-\d{2}-\d{2}$'
    if not re.match(date_pattern, start_str) or not re.match(date_pattern, end_str):
        return jsonify({'error': '日期格式错误，需 YYYY-MM-DD'}), 400

    try:
        start = datetime.strptime(start_str, '%Y-%m-%d').date()
        end = datetime.strptime(end_str, '%Y-%m-%d').date()
    except ValueError as e:
        return jsonify({'error': '无效日期'}), 400

    if start > end:
        return jsonify({'error': '开始日期不能晚于结束日期'}), 400

    # 限制最大回刷范围 90 天
    if (end - start).days > 90:
        return jsonify({'error': '回刷范围不能超过 90 天'}), 400

    return jsonify(WarehouseService().backfill(start, end))
