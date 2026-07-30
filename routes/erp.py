"""ERP 路由 Blueprint"""

from flask import Blueprint, jsonify, request

from services.erp_service import ERPService
from services.idempotency import idempotent
from services.rbac import require_permission

bp = Blueprint("erp", __name__, url_prefix="/api/v1/erp")


@bp.route("/inventory")
@require_permission("erp:read")
def inventory():
    return jsonify(ERPService().get_inventory_summary())


@bp.route("/orders")
@require_permission("erp:read")
def orders():
    # P0: limit 上限保护，防止拉爆内存
    limit = request.args.get("limit", 20, type=int)
    limit = max(1, min(limit, 100))
    return jsonify(ERPService().get_recent_orders(limit))


@bp.route("/account")
@require_permission("erp:read")
def account():
    return jsonify(ERPService().get_account_summary())


@bp.route("/warehouses")
@require_permission("erp:read")
def warehouses():
    from models.erp import Warehouse

    return jsonify(
        [
            {"id": w.id, "code": w.code, "name": w.name, "location": w.location}
            for w in Warehouse.query.filter_by(is_active=True).all()
        ]
    )


@bp.route("/returns", methods=["POST"])
@require_permission("erp:write")
@idempotent()
def create_return():
    data = request.get_json(force=True, silent=True) or {}
    # P0: 入参校验
    required = ["original_order_no", "product_id", "qty"]
    for k in required:
        if k not in data or data[k] is None:
            return jsonify({"error": f"参数 {k} 不能为空"}), 400
    if not isinstance(data["qty"], (int, float)) or data["qty"] <= 0:
        return jsonify({"error": "qty 必须为正数"}), 400
    try:
        ro = ERPService().create_return_order(
            original_order_no=data["original_order_no"],
            product_id=data["product_id"],
            qty=data["qty"],
            refund_amount=data.get("refund_amount", 0),
            reason=data.get("reason", ""),
        )
        return jsonify({"status": "ok", "return_no": ro.return_no})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/transfers", methods=["POST"])
@require_permission("erp:write")
@idempotent()
def create_transfer():
    from schemas import CreateTransferSchema

    data = request.get_json(force=True, silent=True) or {}
    # P0-1: 使用 Schema 校验，统一参数命名（修复 from_wh/from_warehouse 错位）
    errors = CreateTransferSchema().validate(data)
    if errors:
        return jsonify({"error": "参数校验失败", "detail": errors}), 400
    try:
        ref = ERPService().transfer_stock(
            product_id=data["product_id"],
            qty=data["qty"],
            from_warehouse_id=data["from_warehouse_id"],
            to_warehouse_id=data["to_warehouse_id"],
        )
        return jsonify({"status": "ok", "ref_order": ref})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ==================== P1-9 业务模块扩展端点 ====================


@bp.route("/reorder-rules")
@require_permission("erp:read")
def reorder_rules():
    """查询补货预警规则（对应 Odoo stock.warehouse.orderpoint 列表）

    支持查询参数：
        - product_id: 按产品过滤
        - is_active: 是否启用（true/false，默认 true）
    """
    from models.erp import ReorderRule

    product_id = request.args.get("product_id", type=int)
    is_active = request.args.get("is_active", "true").lower() == "true"

    query = ReorderRule.query.filter_by(is_active=is_active) if is_active else ReorderRule.query
    if product_id:
        query = query.filter_by(product_id=product_id)
    rules = query.all()
    return jsonify(
        [
            {
                "id": r.id,
                "product_id": r.product_id,
                "warehouse_id": r.warehouse_id,
                "min_qty": r.min_qty,
                "max_qty": r.max_qty,
                "supplier_id": r.supplier_id,
                "is_active": r.is_active,
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else None,
            }
            for r in rules
        ]
    )


@bp.route("/reorder-check")
@require_permission("erp:read")
def reorder_check():
    """触发库存预警检查（对应 Odoo cron 调用 stock.warehouse.orderpoint）"""
    try:
        alerts = ERPService().check_reorder_rules()
        return jsonify({"status": "ok", "alert_count": len(alerts), "alerts": alerts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/budgets")
@require_permission("erp:read")
def budgets():
    """查询预算列表（对应 Odoo account.budget 列表）

    支持查询参数：
        - cost_center_id: 按成本中心过滤
        - period: 按期间过滤（如 2026-07）
        - is_active: 是否启用（默认 true）
    """
    from models.erp import Budget

    cost_center_id = request.args.get("cost_center_id", type=int)
    period = request.args.get("period", type=str)
    is_active = request.args.get("is_active", "true").lower() == "true"

    query = Budget.query.filter_by(is_active=is_active) if is_active else Budget.query
    if cost_center_id:
        query = query.filter_by(cost_center_id=cost_center_id)
    if period:
        query = query.filter_by(period=period)
    rows = query.all()
    return jsonify(
        [
            {
                "id": b.id,
                "cost_center_id": b.cost_center_id,
                "period": b.period,
                "budget_amount": float(b.budget_amount or 0),
                "actual_amount": float(b.actual_amount or 0),
                # 使用率 = 实际 / 预算（防 0 除）
                "usage_rate": float(b.actual_amount or 0) / float(b.budget_amount or 1),
                "category": b.category,
                "is_active": b.is_active,
                "created_at": b.created_at.strftime("%Y-%m-%d %H:%M:%S") if b.created_at else None,
            }
            for b in rows
        ]
    )


@bp.route("/cost-centers")
@require_permission("erp:read")
def cost_centers():
    """查询成本中心列表（对应 Odoo account.analytic.account）"""
    from models.erp import CostCenter

    rows = CostCenter.query.filter_by(is_active=True).all()
    return jsonify(
        [
            {
                "id": c.id,
                "code": c.code,
                "name": c.name,
                "description": c.description,
                "is_active": c.is_active,
            }
            for c in rows
        ]
    )
