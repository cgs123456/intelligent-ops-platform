"""LLM 经营日报测试"""
from datetime import date, timedelta

import pytest


class TestDailyReport:
    """改进10: 4 段式 LLM 经营日报"""

    def test_load_7d_trend_empty(self, app):
        """空数据库 _load_7d_trend 返回空列表"""
        from extensions import db
        from models.warehouse import AdsDailyOpsReport
        from services.aigc_service import AIGCService
        with app.app_context():
            # 清理可能残留的 ADS 数据
            db.session.query(AdsDailyOpsReport).delete()
            db.session.commit()
            svc = AIGCService()
            trend = svc._load_7d_trend(date.today())
            assert len(trend) == 0

    def test_build_report_context_no_trend(self, app):
        """无趋势时上下文仅含当日指标"""
        from models.warehouse import AdsDailyOpsReport
        from services.aigc_service import AIGCService
        with app.app_context():
            svc = AIGCService()
            fake = AdsDailyOpsReport(
                dt=date.today(), total_sales_amount=12500, total_purchase_amount=8000,
                total_sale_qty=125, inventory_value=45000, top_sku='商品A', low_stock_count=3,
            )
            ctx = svc._build_report_context(fake, date.today(), trend=None)
            assert '销售总额' in ctx
            assert '毛差' in ctx
            assert '7 天趋势' not in ctx

    def test_build_report_context_with_trend(self, app):
        """有趋势时上下文含 7 天趋势表 + 变化统计"""
        from models.warehouse import AdsDailyOpsReport
        from services.aigc_service import AIGCService
        with app.app_context():
            svc = AIGCService()
            fake = AdsDailyOpsReport(
                dt=date.today(), total_sales_amount=12500, total_purchase_amount=8000,
                total_sale_qty=125, inventory_value=45000, top_sku='商品A', low_stock_count=3,
            )
            trend = []
            for i in range(7):
                trend.append({
                    'dt': str(date.today() - timedelta(days=6 - i)),
                    'sales': 10000 + i * 500, 'purchase': 7000, 'sale_qty': 100,
                    'inventory_value': 40000, 'low_stock_count': 2, 'top_sku': 'SKU',
                })
            ctx = svc._build_report_context(fake, date.today(), trend=trend)
            assert '7 天趋势' in ctx
            assert '7日销售变化' in ctx

    def test_template_4_sections_with_trend(self, app):
        """规则模板 4 段式输出（含趋势）"""
        from models.warehouse import AdsDailyOpsReport
        from services.aigc_service import AIGCService
        with app.app_context():
            svc = AIGCService()
            fake = AdsDailyOpsReport(
                dt=date.today(), total_sales_amount=12500, total_purchase_amount=8000,
                total_sale_qty=125, inventory_value=45000, top_sku='商品A', low_stock_count=3,
            )
            trend = []
            for i in range(7):
                trend.append({
                    'dt': str(date.today() - timedelta(days=6 - i)),
                    'sales': 10000 + i * 500, 'purchase': 7000, 'sale_qty': 100,
                    'inventory_value': 40000, 'low_stock_count': 2, 'top_sku': 'SKU',
                })
            text = svc._template_daily_report(fake, date.today(), trend=trend)
            for section in ['【昨日回顾】', '【趋势分析】', '【风险提示】', '【建议行动】']:
                assert section in text, f'缺少段落: {section}'

    def test_template_no_trend_fallback(self, app):
        """无趋势时模板降级"""
        from models.warehouse import AdsDailyOpsReport
        from services.aigc_service import AIGCService
        with app.app_context():
            svc = AIGCService()
            fake = AdsDailyOpsReport(
                dt=date.today(), total_sales_amount=12500, total_purchase_amount=8000,
                total_sale_qty=125, inventory_value=45000, top_sku='商品A', low_stock_count=3,
            )
            text = svc._template_daily_report(fake, date.today(), trend=None)
            assert '历史数据不足' in text
