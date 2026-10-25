"""P3 业务增强测试（F1 安全库存 / F2 ABC-XYZ 分级 / F3 高金额审批 / F4 期间锁定）。"""

import uuid
from datetime import date, datetime, timedelta

import pytest

from extensions import db
from models.aigc import Suggestion
from models.erp import AccountingPeriod, Product, Supplier
from models.external import ExtSupplierQuote as ExtQuote
from models.warehouse import AdsReplenishmentSuggest, DwsSalesSkuDaily
from services.aigc_service import AIGCService
from services.erp_service import ERPService
from services.forecast import safety_stock_recommendation
from services.warehouse_service import WarehouseService


def _uuid8():
    return uuid.uuid4().hex[:8]


@pytest.fixture(autouse=True)
def _cleanup(app):
    """清相关表（服务方法内部 commit，事务回滚失效，须显式清理）。"""
    tables = [
        Suggestion,
        ExtQuote,
        AdsReplenishmentSuggest,
        DwsSalesSkuDaily,
        AccountingPeriod,
        Product,
        Supplier,
    ]
    with app.app_context():
        for t in tables:
            db.session.query(t).delete()
        db.session.commit()
    yield
    with app.app_context():
        for t in tables:
            db.session.query(t).delete()
        db.session.commit()


def _make_product(sku, **kw):
    return Product(
        sku=sku,
        name=kw.pop("name", sku),
        cost_price=kw.pop("cost_price", 10),
        sale_price=kw.pop("sale_price", 20),
        stock_qty=kw.pop("stock_qty", 50),
        avg_cost=kw.pop("avg_cost", 10),
        safety_stock=kw.pop("safety_stock", 10),
        is_active=kw.pop("is_active", True),
        **kw,
    )


# =====================================================================
# F1: 安全库存
# =====================================================================


class TestSafetyStockPure:
    def test_ss_formula(self):
        """SS = z × σ × √LT：固定序列数学验证。"""
        base = date(2026, 1, 1)
        series = [(base + timedelta(days=i), 10) for i in range(30)]  # σ=0
        # 窗口 90 天含 60 个零日：均值 3.33、σ≈4.71 → SS>0（含零日语义）
        rec90 = safety_stock_recommendation(series, lead_days=4, service_level=0.95, window_days=90)
        assert rec90["sigma_daily"] > 0
        # 窗口与序列对齐：稳定需求不需要安全库存
        rec = safety_stock_recommendation(series, lead_days=4, service_level=0.95, window_days=30)
        assert rec["safety_stock"] == 0

        series2 = [(base + timedelta(days=i), 10 if i % 2 == 0 else 20) for i in range(30)]
        rec2 = safety_stock_recommendation(series2, lead_days=4, service_level=0.95, window_days=30)
        # σ(5,10,5,... 均值15)=5, z=1.6449, √4=2 → 1.6449*5*2 ≈ 16.45 → ceil 17
        assert rec2["safety_stock"] == pytest.approx(17, abs=1)

    def test_service_level_rejected(self):
        with pytest.raises(ValueError):
            safety_stock_recommendation([], 3, service_level=0.93)

    def test_zero_lead_days_treated_as_one(self):
        base = date(2026, 1, 1)
        series = [(base + timedelta(days=i), 10 if i % 2 == 0 else 20) for i in range(30)]
        rec = safety_stock_recommendation(series, lead_days=0, service_level=0.95, window_days=30)
        assert rec["safety_stock"] > 0  # lead=0 视 1，不出错


class TestSafetyStockService:
    def test_recalc_preview_and_apply(self, app):
        with app.app_context():
            p = _make_product(f"F1-{_uuid8()}")
            s = Supplier(name=f"S-{_uuid8()}", lead_days=9, is_active=True)
            db.session.add_all([p, s])
            db.session.flush()
            db.session.add(
                ExtQuote(
                    supplier_id=s.id, product_id=p.id, quote_price=15, quote_date=date.today()
                )
            )
            base = date.today() - timedelta(days=29)
            for i in range(30):
                q = 10 if i % 2 == 0 else 20
                db.session.add(
                    DwsSalesSkuDaily(
                        product_id=p.id, product_name=p.name, dt=base + timedelta(days=i),
                        sale_qty=q, sale_amount=q * 20,
                    )
                )
            db.session.commit()

            svc = ERPService()
            rec = svc.recalc_safety_stock(p.id, service_level=0.95, window_days=30)
            assert rec["lead_days"] == 9  # 报价供应商交期生效
            assert rec["current_safety_stock"] == 10
            assert rec["safety_stock"] == pytest.approx(25, abs=2)  # 1.6449*5*3≈24.7
            assert p.safety_stock == 10  # 预览不写回

            svc.recalc_safety_stock(p.id, service_level=0.95, window_days=30, apply=True, actor="qa")
            assert p.safety_stock == rec["safety_stock"]

    def test_recalc_product_not_found(self, app):
        with app.app_context(), pytest.raises(ValueError, match="产品不存在"):
            ERPService().recalc_safety_stock(999999)


# =====================================================================
# F2: ABC/XYZ 分级（ETL 注入）
# =====================================================================


class TestAbcXyzClassification:
    def test_etl_ads_writes_classification(self, app):
        with app.app_context():
            # 三个产品：垄断大额稳定 / 中额 / 小额不稳定
            p1 = _make_product(f"F2A-{_uuid8()}", name="垄断稳定")
            p2 = _make_product(f"F2B-{_uuid8()}", name="中额")
            p3 = _make_product(f"F2C-{_uuid8()}", name="小额波动")
            prods = [p1, p2, p3]
            db.session.add_all(prods)
            db.session.commit()

            base = date.today() - timedelta(days=29)
            # p1: 每天 100 件稳定（cv=0 → X），金额 300/件 = 900k 主导
            # p2: 每天 10 件稳定（X），金额 100/件 = 30k
            # p3: 交替 1/9 件（cv≈0.75 → Y），金额 50/件 = 7.5k
            for i in range(30):
                db.session.add(DwsSalesSkuDaily(product_id=p1.id, product_name=p1.name,
                                                dt=base + timedelta(days=i), sale_qty=100, sale_amount=30000))
                db.session.add(DwsSalesSkuDaily(product_id=p2.id, product_name=p2.name,
                                                dt=base + timedelta(days=i), sale_qty=10, sale_amount=1000))
                q = 1 if i % 2 == 0 else 9
                db.session.add(DwsSalesSkuDaily(product_id=p3.id, product_name=p3.name,
                                                dt=base + timedelta(days=i), sale_qty=q, sale_amount=q * 50))
            db.session.commit()

            result = WarehouseService()._etl_ads(target_date=date.today())

            by_pid = {r.product_id: r for r in AdsReplenishmentSuggest.query.all()}
            assert len(by_pid) == 3
            r1, r2, r3 = by_pid[p1.id], by_pid[p2.id], by_pid[p3.id]
            # 帕累托：p1 一家独大（>80%）→ rank0 强制 A；其余累计进入 B/C
            assert r1.abc_class == "A"
            assert {r2.abc_class, r3.abc_class} <= {"A", "B", "C"}
            # XYZ：p1/p2 cv=0 → X；p3 cv≈0.75 → Y
            assert r1.xyz_class == "X"
            assert r2.xyz_class == "X"
            assert r3.xyz_class == "Y"
            # 份额数值落库
            assert r1.revenue_share_90d is not None and float(r1.revenue_share_90d) > 0.9
            assert r3.cv_90d is not None and 0.7 < float(r3.cv_90d) <= 0.81
            assert result["details"]["ads_replenishment_suggest"] >= 3


# =====================================================================
# F3: 高金额审批
# =====================================================================


def _make_pending_suggestion(product_id, qty, unit_price=None):
    s = Suggestion(
        product_id=product_id,
        product_name="F3 商品",
        suggested_qty=qty,
        original_qty=qty,
        unit_price=unit_price,
        status="pending",
    )
    db.session.add(s)
    db.session.commit()
    return s


class TestHighValueApproval:
    def test_amount_quote_preferred_over_avg_cost(self, app):
        with app.app_context():
            p = _make_product(f"F3-{_uuid8()}", avg_cost=100)
            sup = Supplier(name=f"S-{_uuid8()}", is_active=True)
            db.session.add_all([p, sup])
            db.session.flush()
            db.session.add(ExtQuote(supplier_id=sup.id, product_id=p.id,
                                    quote_price=120, quote_date=date.today()))
            db.session.commit()
            s = _make_pending_suggestion(p.id, 100)
            amount = AIGCService()._suggestion_amount(s)
            assert amount == pytest.approx(12000)  # 报价优先于 avg_cost

    def test_amount_fallback_avg_cost(self, app):
        with app.app_context():
            p = _make_product(f"F3-{_uuid8()}", avg_cost=80)
            db.session.add(p)
            db.session.commit()
            s = _make_pending_suggestion(p.id, 50)
            assert AIGCService()._suggestion_amount(s) == pytest.approx(4000)

    def test_high_value_requires_permission(self, app):
        with app.app_context():
            p = _make_product(f"F3-{_uuid8()}", avg_cost=100)
            db.session.add(p)
            db.session.commit()
            s = _make_pending_suggestion(p.id, 1000)  # 1000×100 = 100000 >= 50000

            svc = AIGCService()
            with pytest.raises(ValueError, match="aigc:approve_high"):
                svc.review_suggestion(s.id, "approve", actor_permissions={"aigc:review"})

            # 已拒后状态仍 pending（校验先于状态变更）
            db.session.refresh(s)
            assert s.status == "pending"

    def test_high_value_admin_wildcard_passes(self, app):
        with app.app_context():
            p = _make_product(f"F3-{_uuid8()}", avg_cost=100)
            db.session.add(p)
            db.session.commit()
            s = _make_pending_suggestion(p.id, 1000)
            AIGCService().review_suggestion(s.id, "approve", actor_permissions={"*:*"})
            db.session.refresh(s)
            assert s.status == "approved"

    def test_system_internal_call_passes(self, app):
        with app.app_context():
            p = _make_product(f"F3-{_uuid8()}", avg_cost=100)
            db.session.add(p)
            db.session.commit()
            s = _make_pending_suggestion(p.id, 1000)
            AIGCService().review_suggestion(s.id, "approve", actor_permissions=None)
            db.session.refresh(s)
            assert s.status == "approved"

    def test_low_value_normal_permission_passes(self, app):
        with app.app_context():
            p = _make_product(f"F3-{_uuid8()}", avg_cost=100)
            db.session.add(p)
            db.session.commit()
            s = _make_pending_suggestion(p.id, 10)  # 10×100 = 1000 < 50000
            AIGCService().review_suggestion(s.id, "approve", actor_permissions={"aigc:review"})
            db.session.refresh(s)
            assert s.status == "approved"

    def test_reject_not_gated_by_amount(self, app):
        with app.app_context():
            p = _make_product(f"F3-{_uuid8()}", avg_cost=100)
            db.session.add(p)
            db.session.commit()
            s = _make_pending_suggestion(p.id, 10000)  # 巨额但 reject 不受金额门槛限制
            AIGCService().review_suggestion(s.id, "reject", actor_permissions={"aigc:review"})
            db.session.refresh(s)
            assert s.status == "rejected"


# =====================================================================
# F4: 会计期间锁定
# =====================================================================


class TestAccountingPeriod:
    def test_create_format_validation(self, app):
        with app.app_context():
            with pytest.raises(ValueError, match="YYYY-MM"):
                ERPService().create_accounting_period("2026/10")
            with pytest.raises(ValueError, match="YYYY-MM"):
                ERPService().create_accounting_period("2026-13")

    def test_create_duplicate_rejected(self, app):
        with app.app_context():
            name = datetime.now().strftime("%Y-%m")
            ERPService().create_accounting_period(name)
            with pytest.raises(ValueError, match="已存在"):
                ERPService().create_accounting_period(name)

    def test_close_blocks_posting(self, app):
        with app.app_context():
            name = datetime.now().strftime("%Y-%m")
            ERPService().create_accounting_period(name)
            ERPService().close_accounting_period(name, actor="qa")

            # 关账月过账被拒：销售开单走 create_sale_order（内部 _assert_period_open）
            p = _make_product(f"F4-{_uuid8()}", stock_qty=100)
            db.session.add(p)
            db.session.commit()
            with pytest.raises(ValueError, match="已关账"):
                ERPService().create_sale_order(
                    product_id=p.id, qty=1, unit_price=10, customer="测试客户",
                )

    def test_close_idempotent_and_open_period_passes(self, app):
        with app.app_context():
            name = datetime.now().strftime("%Y-%m")
            ERPService().create_accounting_period(name)
            # 未关账：正常过账（建供应商+采购单确认）
            sup = Supplier(name=f"S-{_uuid8()}", is_active=True)
            p = _make_product(f"F4-{_uuid8()}")
            db.session.add_all([sup, p])
            db.session.commit()
            po = ERPService().create_purchase_order(
                supplier_id=sup.id, product_id=p.id, qty=5, unit_price=10,
            )
            ERPService().confirm_purchase_order(po.id)
            # 关账幂等
            ERPService().close_accounting_period(name, actor="qa")
            period2 = ERPService().close_accounting_period(name, actor="qa2")
            db.session.refresh(period2)
            assert period2.is_closed is True
            assert period2.closed_by == "qa"  # 幂等重复关闭不覆盖首次关账人

    def test_list_order_desc(self, app):
        with app.app_context():
            ERPService().create_accounting_period("2026-01")
            ERPService().create_accounting_period("2026-02")
            names = [p.name for p in ERPService().list_accounting_periods()]
            assert names[:2] == ["2026-02", "2026-01"]
