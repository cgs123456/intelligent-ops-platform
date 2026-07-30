"""财务对账路由 Blueprint

提供对账批次的创建、明细导入、自动匹配、报告查询、关闭等接口。
权限：
    - finance:read：查询批次列表 / 对账报告
    - finance:write：创建批次 / 导入明细 / 自动匹配 / 关闭批次
"""

from flask import Blueprint, request

from extensions import db
from models.finance import ReconciliationBatch, ReconciliationEntry
from services.finance_recon_service import ReconciliationService
from services.rbac import require_permission
from services.response import error_response, success_response

bp = Blueprint("finance", __name__, url_prefix="/api/v1/finance")


def _serialize_batch(batch: ReconciliationBatch) -> dict:
    """序列化对账批次为 dict"""
    return {
        "id": batch.id,
        "batch_no": batch.batch_no,
        "period": batch.period,
        "status": batch.status,
        "total_system_amount": float(batch.total_system_amount or 0),
        "total_external_amount": float(batch.total_external_amount or 0),
        "discrepancy_amount": float(batch.discrepancy_amount or 0),
        "created_by": batch.created_by,
        "created_at": batch.created_at.strftime("%Y-%m-%d %H:%M:%S") if batch.created_at else None,
        "closed_at": batch.closed_at.strftime("%Y-%m-%d %H:%M:%S") if batch.closed_at else None,
        "notes": batch.notes,
    }


@bp.route("/reconciliation/batches", methods=["POST"])
@require_permission("finance:write")
def create_batch():
    """创建对账批次

    Body:
        - period: 对账期间 YYYY-MM（必填）
        - created_by: 创建人（可选，默认 'system'）
        - notes: 备注（可选）
    """
    data = request.get_json(force=True, silent=True) or {}
    period = data.get("period")
    if not period:
        return error_response("ERR_SYS_002", "period 不能为空")
    try:
        svc = ReconciliationService()
        batch = svc.create_batch(period=period, created_by=data.get("created_by") or "system")
        if data.get("notes"):
            batch.notes = data.get("notes")
            db.session.commit()
        return success_response(data=_serialize_batch(batch), message="对账批次已创建")
    except ValueError as e:
        return error_response("ERR_SYS_002", str(e))
    except Exception as e:
        return error_response("ERR_SYS_001", f"创建对账批次失败：{e}")


@bp.route("/reconciliation/batches", methods=["GET"])
@require_permission("finance:read")
def list_batches():
    """查询对账批次列表

    Query:
        - period: 按期间过滤（可选）
        - status: 按状态过滤（可选）
        - limit: 返回条数上限，默认 50，最大 200
    """
    period = request.args.get("period")
    status = request.args.get("status")
    limit = request.args.get("limit", 50, type=int)
    limit = max(1, min(limit, 200))

    q = db.session.query(ReconciliationBatch)
    if period:
        q = q.filter_by(period=period)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(ReconciliationBatch.created_at.desc()).limit(limit).all()
    return success_response(data=[_serialize_batch(b) for b in rows])


@bp.route("/reconciliation/batches/<int:batch_id>/import", methods=["POST"])
@require_permission("finance:write")
def import_entries(batch_id: int):
    """导入外部流水明细到指定批次

    Body:
        - entries: list[dict]，每项包含 source/account_code/entry_date/amount/direction/reference_no
    """
    data = request.get_json(force=True, silent=True) or {}
    entries = data.get("entries")
    if not isinstance(entries, list):
        return error_response("ERR_SYS_002", "entries 必须为列表")
    try:
        svc = ReconciliationService()
        imported = svc.import_entries(batch_id, entries)
        return success_response(data={"imported": imported}, message=f"已导入 {imported} 条明细")
    except ValueError as e:
        return error_response("ERR_SYS_002", str(e))
    except Exception as e:
        return error_response("ERR_SYS_001", f"导入明细失败：{e}")


@bp.route("/reconciliation/batches/<int:batch_id>/auto-match", methods=["POST"])
@require_permission("finance:write")
def auto_match(batch_id: int):
    """自动匹配批次明细（按 reference_no + amount 精确匹配）

    可选在匹配前自动加载系统侧明细：Body 中 load_system=true 时先调用 load_system_entries
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        svc = ReconciliationService()
        if data.get("load_system"):
            svc.load_system_entries(batch_id)
        result = svc.auto_match(batch_id)
        return success_response(data=result, message="自动匹配完成")
    except ValueError as e:
        return error_response("ERR_SYS_002", str(e))
    except Exception as e:
        return error_response("ERR_SYS_001", f"自动匹配失败：{e}")


@bp.route("/reconciliation/batches/<int:batch_id>/report", methods=["GET"])
@require_permission("finance:read")
def batch_report(batch_id: int):
    """查询对账报告：批次信息 + 合计金额 + 未匹配明细清单

    Query:
        - calc: 是否重新计算合计（true/false，默认 false）
    """
    try:
        svc = ReconciliationService()
        batch = db.session.get(ReconciliationBatch, batch_id)
        if not batch:
            return error_response("ERR_SYS_003", f"对账批次不存在: {batch_id}")

        if request.args.get("calc", "false").lower() == "true":
            totals = svc.calculate_totals(batch_id)
        else:
            totals = {
                "total_system_amount": float(batch.total_system_amount or 0),
                "total_external_amount": float(batch.total_external_amount or 0),
                "discrepancy_amount": float(batch.discrepancy_amount or 0),
            }

        # 未匹配明细
        unmatched = (
            db.session.query(ReconciliationEntry)
            .filter_by(batch_id=batch_id, matched=False)
            .order_by(ReconciliationEntry.source, ReconciliationEntry.id)
            .all()
        )
        unmatched_list = [
            {
                "id": e.id,
                "source": e.source,
                "account_code": e.account_code,
                "entry_date": e.entry_date.strftime("%Y-%m-%d") if e.entry_date else None,
                "amount": float(e.amount or 0),
                "direction": e.direction,
                "reference_no": e.reference_no,
                "mismatch_reason": e.mismatch_reason,
            }
            for e in unmatched
        ]
        report = {
            "batch": _serialize_batch(batch),
            "totals": totals,
            "unmatched_count": len(unmatched_list),
            "unmatched": unmatched_list,
        }
        return success_response(data=report)
    except ValueError as e:
        return error_response("ERR_SYS_002", str(e))
    except Exception as e:
        return error_response("ERR_SYS_001", f"查询对账报告失败：{e}")


@bp.route("/reconciliation/batches/<int:batch_id>/close", methods=["POST"])
@require_permission("finance:write")
def close_batch(batch_id: int):
    """关闭对账批次（要求无未匹配明细）

    Body:
        - closed_by: 关闭人（可选，默认 'system'）
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        svc = ReconciliationService()
        ok = svc.close_batch(batch_id, closed_by=data.get("closed_by") or "system")
        return success_response(data={"closed": ok}, message="对账批次已关闭")
    except ValueError as e:
        return error_response("ERR_SYS_002", str(e))
    except Exception as e:
        return error_response("ERR_SYS_001", f"关闭批次失败：{e}")
