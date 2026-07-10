"""FDE 路由 Blueprint"""
import re
from datetime import datetime

from flask import Blueprint, jsonify, request

from services.rbac import require_permission
from services.warehouse_service import WarehouseService

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
    except ValueError:
        return jsonify({'error': '无效日期'}), 400

    if start > end:
        return jsonify({'error': '开始日期不能晚于结束日期'}), 400

    # 限制最大回刷范围 90 天
    if (end - start).days > 90:
        return jsonify({'error': '回刷范围不能超过 90 天'}), 400

    return jsonify(WarehouseService().backfill(start, end))


# ---- 改进9：FDE 时序异常检测 ----


@bp.route('/anomalies')
@require_permission('fde:read')
def anomalies():
    """检测销售时序异常（7 日移动平均 + 2σ）。

    查询参数：
        date: 可选，检测日期（YYYY-MM-DD，默认今天）
        trigger: 可选，是否自动触发闭环补货（true/false，默认 false）
    """
    from datetime import date, datetime

    from services.anomaly_detector import AnomalyDetector

    detector = AnomalyDetector()
    target_date = None
    date_str = request.args.get('date')
    if date_str:
        try:
            target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'error': '日期格式错误，需 YYYY-MM-DD'}), 400

    if request.args.get('trigger', '').lower() == 'true':
        result = detector.detect_and_trigger(target_date)
    else:
        result = detector.detect_sales_anomalies(target_date)
    return jsonify(result)
