"""routes 包初始化 · 注册所有 Blueprint

旧路径兼容说明（P0 安全修复）：
  原 /api/xxx 旧路径全部为重复实现，绕过权限校验与输入校验，存在安全风险。
  现统一改为 redirect 到对应 /api/v1/xxx 新路径，让新路径的 @require_permission
  装饰器统一生效。/api/dashboard 无对应 v1 路由，保留实现但加 @require_permission。
"""
from flask import jsonify, redirect, request, url_for

from services.rbac import require_permission

from .aigc import bp as aigc_bp
from .audit import bp as audit_bp
from .auth import bp as auth_bp
from .erp import bp as erp_bp
from .fde import bp as fde_bp
from .loop import bp as loop_bp
from .rpa import bp as rpa_bp


def register_blueprints(app):
    """注册全部 Blueprint + 兼容旧路径（重定向到 v1）"""
    app.register_blueprint(auth_bp)
    app.register_blueprint(erp_bp)
    app.register_blueprint(rpa_bp)
    app.register_blueprint(fde_bp)
    app.register_blueprint(aigc_bp)
    app.register_blueprint(loop_bp)
    app.register_blueprint(audit_bp)

    # ---- /api/dashboard：保留实现，但加权限校验 ----
    @app.route('/api/dashboard')
    @require_permission('erp:read')
    def dashboard():
        from services.aigc_service import AIGCService
        from services.erp_service import ERPService
        from services.warehouse_service import WarehouseService
        inv = ERPService().get_inventory_summary()
        acct = ERPService().get_account_summary()
        ads = WarehouseService().get_ads_data()
        report = ads.get('report')
        pending = len(AIGCService().get_pending_suggestions())
        return jsonify({
            'total_skus': len(inv),
            'low_stock_count': sum(1 for i in inv if i.get('is_low')),
            'inventory_value': sum(i.get('stock_value', 0) for i in inv),
            'total_payable': acct.get('payable', 0),
            'total_receivable': acct.get('net_receivable', acct.get('receivable', 0)),
            'pending_suggestions': pending,
            'sales_7d': report['total_sales_amount'] if report else 0,
            'sale_qty_7d': report['total_sale_qty'] if report else 0,
            'top_sku': report['top_sku'] if report else '-'
        })

    # ---- 旧路径统一 308 永久重定向到 v1 新路径 ----
    # 308 保留 method 和 body，POST 请求不会丢 body
    _legacy_redirect_map = {
        '/api/erp/inventory': '/api/v1/erp/inventory',
        '/api/erp/orders': '/api/v1/erp/orders',
        '/api/erp/account': '/api/v1/erp/account',
        '/api/rpa/quotes': '/api/v1/rpa/quotes',
        '/api/rpa/sync-orders': '/api/v1/rpa/sync-orders',
        '/api/fde/run': '/api/v1/fde/run',
        '/api/fde/stats': '/api/v1/fde/stats',
        '/api/fde/ads': '/api/v1/fde/ads',
        '/api/aigc/suggestions': '/api/v1/aigc/suggestions',
        '/api/aigc/generate-suggestions': '/api/v1/aigc/generate-suggestions',
        '/api/aigc/review': '/api/v1/aigc/review',
        '/api/aigc/report': '/api/v1/aigc/report',
        '/api/aigc/generate-report': '/api/v1/aigc/generate-report',
        '/api/aigc/query': '/api/v1/aigc/query',
        '/api/loop/status': '/api/v1/loop/status',
        '/api/loop/run-step': '/api/v1/loop/run-step',
        '/api/loop/reset': '/api/v1/loop/reset',
    }

    def _make_redirect(new_path):
        def _redirect_view(**kwargs):
            return redirect(new_path, code=308)
        return _redirect_view

    for old_path, new_path in _legacy_redirect_map.items():
        # GET 和 POST 都要支持
        app.add_url_rule(old_path, endpoint=f'legacy_{old_path.strip("/")}',
                         view_func=_make_redirect(new_path),
                         methods=['GET', 'POST'])
