"""MRP/BOM 路由 Blueprint（P0-fix: 补齐核心业务 HTTP 入口）"""

from flask import Blueprint, jsonify, request

from services.idempotency import idempotent
from services.mrp_service import MRPService
from services.rbac import require_permission
from services.response import parse_page_args

bp = Blueprint("mrp", __name__, url_prefix="/api/v1/mrp")


@bp.route("/boms", methods=["POST"])
@require_permission("mrp:write")
@idempotent()
def create_bom():
    """创建 BOM（物料清单）

    items 每项为 dict，需包含 component_product_id 与 quantity，例如：
        [{"component_product_id": 1, "quantity": 2}, ...]
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        bom = MRPService().create_bom(
            product_id=data["product_id"],
            items=data["items"],
        )
        return jsonify({"id": bom.id, "product_id": bom.product_id}), 201
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/boms", methods=["GET"])
@require_permission("mrp:read")
def list_boms():
    """查询 BOM 列表（P2-4: 分页，默认 1/50，per_page 上限 200）

    与 /api/v1/erp/reorder-rules 分页结构一致：{items, page, per_page, total}
    """
    from models.bom import BOM
    from models.erp import Product

    page, per_page = parse_page_args()

    pag = BOM.query.order_by(BOM.id).paginate(page=page, per_page=per_page, error_out=False)
    boms = pag.items
    # 批量预取产品，避免 N+1
    product_ids = {b.product_id for b in boms}
    product_map = {}
    if product_ids:
        product_map = {p.id: p for p in Product.query.filter(Product.id.in_(product_ids)).all()}
    return jsonify(
        {
            "items": [
                {
                    "id": b.id,
                    "code": b.code,
                    "product_id": b.product_id,
                    "product_sku": product_map[b.product_id].sku if b.product_id in product_map else None,
                    "product_name": product_map[b.product_id].name if b.product_id in product_map else None,
                    "version": b.version,
                    "status": b.status,
                    "quantity": float(b.quantity or 1),
                    "unit": b.unit,
                    "description": b.description,
                    "created_at": b.created_at.strftime("%Y-%m-%d %H:%M:%S") if b.created_at else None,
                }
                for b in boms
            ],
            "page": page,
            "per_page": per_page,
            "total": pag.total,
        }
    )


@bp.route("/plans", methods=["POST"])
@require_permission("mrp:write")
@idempotent()
def create_mrp_plan():
    """创建 MRP 计划

    必填：product_id、bom_id、planned_qty、planned_date（YYYY-MM-DD）
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        plan = MRPService().create_plan(
            product_id=data["product_id"],
            bom_id=data["bom_id"],
            planned_qty=data["planned_qty"],
            planned_date=data["planned_date"],
        )
        return jsonify({"id": plan.id, "plan_no": plan.plan_no}), 201
    except (KeyError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/plans/<int:plan_id>/calculate", methods=["POST"])
@require_permission("mrp:write")
@idempotent()
def calculate_mrp(plan_id):
    """执行 MRP 净需求计算"""
    try:
        result = MRPService().calculate_mrp(plan_id)
        return jsonify(result), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/plans/<int:plan_id>/release", methods=["POST"])
@require_permission("mrp:write")
@idempotent()
def release_mrp_plan(plan_id):
    """下达 MRP 计划：将建议采购需求转为草稿采购单"""
    try:
        result = MRPService().release_plan(plan_id)
        return jsonify(result), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
