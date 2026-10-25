"""ERP 路由 Blueprint"""

from flask import Blueprint, jsonify, request

from services.erp_service import ERPService
from services.idempotency import idempotent
from services.rbac import get_actor_name, require_permission

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
@require_permission("return:write")
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
@require_permission("transfer:write")
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
        - page / per_page: 分页（默认 1 / 50，per_page 上限 200）
    """
    from models.erp import ReorderRule

    product_id = request.args.get("product_id", type=int)
    is_active = request.args.get("is_active", "true").lower() == "true"

    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 50, type=int), 1), 200)

    query = ReorderRule.query.filter_by(is_active=is_active) if is_active else ReorderRule.query
    if product_id:
        query = query.filter_by(product_id=product_id)
    pag = query.order_by(ReorderRule.id).paginate(page=page, per_page=per_page, error_out=False)
    return jsonify(
        {
            "items": [
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
                for r in pag.items
            ],
            "page": pag.page,
            "per_page": pag.per_page,
            "total": pag.total,
        }
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
        - page / per_page: 分页（默认 1 / 50，per_page 上限 200）
    """
    from models.erp import Budget

    cost_center_id = request.args.get("cost_center_id", type=int)
    period = request.args.get("period", type=str)
    is_active = request.args.get("is_active", "true").lower() == "true"

    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 50, type=int), 1), 200)

    query = Budget.query.filter_by(is_active=is_active) if is_active else Budget.query
    if cost_center_id:
        query = query.filter_by(cost_center_id=cost_center_id)
    if period:
        query = query.filter_by(period=period)
    pag = query.order_by(Budget.id).paginate(page=page, per_page=per_page, error_out=False)
    return jsonify(
        {
            "items": [
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
                for b in pag.items
            ],
            "page": pag.page,
            "per_page": pag.per_page,
            "total": pag.total,
        }
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


# ==================== 采购单 / 销售单（P0-fix: 补齐核心业务 HTTP 入口） ====================


@bp.route("/purchase-orders", methods=["POST"])
@require_permission("purchase:write")
@idempotent()
def create_purchase_order():
    """创建采购单（对应 Odoo purchase.order 创建，状态 draft）"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        order = ERPService().create_purchase_order(
            supplier_id=data["supplier_id"],
            product_id=data["product_id"],
            qty=data["qty"],
            unit_price=data["unit_price"],
            warehouse_id=data.get("warehouse_id"),
            ext_order_no=data.get("ext_order_no"),
            currency_code=data.get("currency_code", "CNY"),
            tax_rate=data.get("tax_rate", 0.13),
        )
        return jsonify({"order_no": order.order_no, "id": order.id}), 201
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/purchase-orders/<order_no>/confirm", methods=["POST"])
@require_permission("purchase:write")
@idempotent()
def confirm_purchase_order(order_no):
    """确认采购单：draft -> confirmed，生成应付凭证"""
    from models.erp import PurchaseOrder

    order = PurchaseOrder.query.filter_by(order_no=order_no).first()
    if not order:
        return jsonify({"error": f"采购单不存在: {order_no}"}), 404
    try:
        order = ERPService().confirm_purchase_order(order.id)
        return jsonify({"order_no": order.order_no, "status": order.status}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/purchase-orders/<order_no>/receive", methods=["POST"])
@require_permission("purchase:write")
@idempotent()
def receive_purchase_order(order_no):
    """到货入库：confirmed -> received，更新库存 + 移动加权平均成本 + 入库流水"""
    from models.erp import PurchaseOrder

    order = PurchaseOrder.query.filter_by(order_no=order_no).first()
    if not order:
        return jsonify({"error": f"采购单不存在: {order_no}"}), 404
    try:
        order = ERPService().receive_purchase_order(order.id)
        return jsonify({"order_no": order.order_no, "status": order.status}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/purchase-orders/<order_no>/cancel", methods=["POST"])
@require_permission("purchase:write")
@idempotent()
def cancel_purchase_order(order_no):
    """取消采购单：置 cancelled 状态"""
    from models.erp import PurchaseOrder

    order = PurchaseOrder.query.filter_by(order_no=order_no).first()
    if not order:
        return jsonify({"error": f"采购单不存在: {order_no}"}), 404
    try:
        order = ERPService().cancel_purchase_order(order.id)
        return jsonify({"order_no": order.order_no, "status": order.status}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/sale-orders", methods=["POST"])
@require_permission("sale:write")
@idempotent()
def create_sale_order():
    """创建销售单：扣减库存 + 出库流水 + 应收凭证 + 按 avg_cost 结转成本"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        order = ERPService().create_sale_order(
            product_id=data["product_id"],
            qty=data["qty"],
            unit_price=data["unit_price"],
            customer=data.get("customer", ""),
            warehouse_id=data.get("warehouse_id"),
            currency_code=data.get("currency_code", "CNY"),
            tax_rate=data.get("tax_rate", 0.13),
        )
        return jsonify({"order_no": order.order_no, "id": order.id}), 201
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


# ==================== P2-20 / P2-27 业务扩展端点 ====================


@bp.route("/budgets/<int:budget_id>/refresh", methods=["POST"])
@require_permission("erp:write")
@idempotent()
def refresh_budget(budget_id: int):
    """刷新预算实际发生额（对应 Odoo account.budget cron 汇总）

    按 cost_center_id + period 聚合 AccountMove 金额，回填 Budget.actual_amount。
    """
    try:
        actual = ERPService().refresh_budget_actual(budget_id)
        return jsonify({"status": "ok", "budget_id": budget_id, "actual_amount": float(actual)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/purchase-orders/<order_no>/return", methods=["POST"])
@require_permission("purchase:write")
@idempotent()
def return_purchase_order(order_no: str):
    """采购退货：减少库存 + 红字应付凭证 + 状态置 returned

    Body:
        - qty: 退货数量（正数，必填）
        - reason: 退货原因（可选）
    """
    data = request.get_json(force=True, silent=True) or {}
    qty = data.get("qty")
    if qty is None:
        return jsonify({"error": "qty 不能为空"}), 400
    if not isinstance(qty, (int, float)) or qty <= 0:
        return jsonify({"error": "qty 必须为正数"}), 400
    try:
        order = ERPService().create_purchase_return(
            order_no=order_no,
            qty=int(qty),
            reason=data.get("reason", ""),
        )
        return jsonify({"order_no": order.order_no, "status": order.status}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ==================== 库存盘点（P1: cycle count）====================


@bp.route("/stock-takes", methods=["POST"])
@require_permission("erp:write")
@idempotent()
def create_stock_take():
    """创建盘点单（对应 Odoo stock.inventory 创建）

    Body:
        - product_id: 盘点产品 ID（必填）
        - warehouse_id: 盘点仓库 ID（可选，None 表示总库存盘点）
    """
    data = request.get_json(force=True, silent=True) or {}
    product_id = data.get("product_id")
    if product_id is None:
        return jsonify({"error": "product_id 不能为空"}), 400
    if not isinstance(product_id, int) or product_id <= 0:
        return jsonify({"error": "product_id 必须为正整数"}), 400
    try:
        take = ERPService().create_stock_take(
            product_id=product_id,
            warehouse_id=data.get("warehouse_id"),
        )
        return jsonify({"id": take.id, "status": take.status, "system_qty": take.system_qty}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/stock-takes", methods=["GET"])
@require_permission("erp:read")
def list_stock_takes():
    """查询盘点单列表（对应 Odoo stock.inventory 列表）

    查询参数：
        - product_id: 按产品过滤（可选）
        - status: 按状态过滤（draft/confirmed，可选）
        - limit: 返回条数上限（默认 50，最大 200）
    """
    product_id = request.args.get("product_id", type=int)
    status = request.args.get("status", type=str)
    limit = request.args.get("limit", 50, type=int)
    try:
        rows = ERPService().list_stock_takes(product_id=product_id, status=status, limit=limit)
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/stock-takes/<int:take_id>/confirm", methods=["POST"])
@require_permission("erp:write")
@idempotent()
def confirm_stock_take(take_id: int):
    """确认盘点单：计算差异并调整库存（对应 Odoo stock.inventory.action_done）

    Body:
        - actual_qty: 实际盘点数量（非负整数，必填）
    """
    data = request.get_json(force=True, silent=True) or {}
    actual_qty = data.get("actual_qty")
    if actual_qty is None:
        return jsonify({"error": "actual_qty 不能为空"}), 400
    # 允许 JSON 传入整数，拒绝浮点/字符串
    if isinstance(actual_qty, bool) or not isinstance(actual_qty, int) or actual_qty < 0:
        return jsonify({"error": "actual_qty 必须为非负整数"}), 400
    try:
        take = ERPService().confirm_stock_take(take_id=take_id, actual_qty=actual_qty)
        return (
            jsonify(
                {
                    "id": take.id,
                    "status": take.status,
                    "system_qty": take.system_qty,
                    "actual_qty": take.actual_qty,
                    "diff": take.diff,
                }
            ),
            200,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@bp.route("/products/<int:product_id>/safety-stock-recalc", methods=["POST"])
@require_permission("erp:write")
def safety_stock_recalc(product_id: int):
    """P3-F1: 重算产品安全库存（SS = z × σ_日需求 × √提前期）。

    Body 可选:
        - service_level: 服务水位 0.90/0.95/0.98/0.99（默认 0.95）
        - window_days: 统计窗口天数（默认 90）
        - apply: true 时把建议值写回 Product.safety_stock 并审计（默认 false 仅预览）
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        service_level = float(data.get("service_level", 0.95))
        window_days = int(data.get("window_days", 90))
    except (TypeError, ValueError):
        return jsonify({"error": "service_level / window_days 必须为数值"}), 400
    apply = bool(data.get("apply", False))
    try:
        rec = ERPService().recalc_safety_stock(
            product_id,
            service_level=service_level,
            window_days=window_days,
            apply=apply,
            actor=get_actor_name(),
        )
        return jsonify(rec), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/accounting-periods")
@require_permission("erp:read")
def accounting_periods():
    """P3-F4: 会计期间列表（名称/关账状态/关账人/关账时间）。"""
    periods = ERPService().list_accounting_periods()
    return jsonify(
        [
            {
                "id": p.id,
                "name": p.name,
                "is_closed": p.is_closed,
                "closed_by": p.closed_by,
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            }
            for p in periods
        ]
    )


@bp.route("/accounting-periods", methods=["POST"])
@require_permission("erp:write")
def create_accounting_period():
    """P3-F4: 创建会计期间（YYYY-MM）。"""
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"error": "缺少必填参数 name（YYYY-MM）"}), 400
    try:
        period = ERPService().create_accounting_period(name, actor=get_actor_name())
        return jsonify({"id": period.id, "name": period.name, "is_closed": period.is_closed}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/accounting-periods/<name>/close", methods=["POST"])
@require_permission("erp:write")
def close_accounting_period(name: str):
    """P3-F4: 关闭会计期间——该月过账入口（采购确认/销售/退货/采购退）全部拒绝。"""
    try:
        period = ERPService().close_accounting_period(name, actor=get_actor_name())
        return jsonify(
            {
                "id": period.id,
                "name": period.name,
                "is_closed": period.is_closed,
                "closed_by": period.closed_by,
            }
        ), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
