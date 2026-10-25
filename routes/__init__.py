"""routes 包初始化 · 注册所有 Blueprint

旧无版本路径（/api/erp/* 等）曾有 308 兼容重定向，契约对齐时移除：
前端已全部直连 /api/v1/*，项目从未上线，无存量外部消费者。
/api/dashboard 无对应 v1 路由，保留实现并加 @require_permission。
"""

from flask import jsonify

from services.rbac import require_permission

from .aigc import bp as aigc_bp
from .audit import bp as audit_bp
from .auth import bp as auth_bp
from .erp import bp as erp_bp
from .fde import bp as fde_bp
from .finance import bp as finance_bp
from .loop import bp as loop_bp
from .mrp import bp as mrp_bp
from .rpa import bp as rpa_bp
from .tenant import bp as tenant_bp


def register_blueprints(app):
    """注册全部 Blueprint + 兼容旧路径（重定向到 v1）"""
    app.register_blueprint(auth_bp)
    app.register_blueprint(erp_bp)
    app.register_blueprint(rpa_bp)
    app.register_blueprint(fde_bp)
    app.register_blueprint(aigc_bp)
    app.register_blueprint(loop_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(finance_bp)  # P2-17: 财务对账模块
    app.register_blueprint(mrp_bp)  # P0-fix: MRP/BOM 模块
    app.register_blueprint(tenant_bp)  # 多租户：平台管理员租户/用户管理

    # ---- /api/dashboard：保留实现，但加权限校验 ----
    @app.route("/api/dashboard")
    @require_permission("erp:read")
    def dashboard():
        from services.aigc_service import AIGCService
        from services.erp_service import ERPService
        from services.warehouse_service import WarehouseService

        inv = ERPService().get_inventory_summary()
        acct = ERPService().get_account_summary()
        ads = WarehouseService().get_ads_data()
        report = ads.get("report")
        pending = len(AIGCService().get_pending_suggestions())
        return jsonify(
            {
                "total_skus": len(inv),
                "low_stock_count": sum(1 for i in inv if i.get("is_below_safety")),
                "inventory_value": sum(i.get("stock_value", 0) for i in inv),
                "total_payable": acct.get("payable", 0),
                "total_receivable": acct.get("net_receivable", acct.get("receivable", 0)),
                "pending_suggestions": pending,
                "sales_7d": report["total_sales_amount"] if report else 0,
                "sale_qty_7d": report["total_sale_qty"] if report else 0,
                "top_sku": report["top_sku"] if report else "-",
            }
        )

    # ---- 旧路径统一 308 永久重定向到 v1 新路径 ----
    # 旧无版本路径已在契约对齐时移除：前端已全部直连 /api/v1/*，
    # 项目从未上线部署，无存量外部消费者，不再保留 308 兼容层。
