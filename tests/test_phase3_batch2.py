"""P3 第二批测试：G1 试算平衡 / G2 多对一+调节项 / G3 库位流水。"""
import uuid

import pytest

from extensions import db as _db
from models.erp import (
    AccountingPeriod,
    Product,
    PurchaseOrder,
    ReturnOrder,
    SaleOrder,
    StockMove,
    Supplier,
    Warehouse,
)
from models.finance import ReconciliationBatch, ReconciliationEntry
from services.erp_service import ERPService
from services.finance_recon_service import ReconciliationService


def _uid8():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def app_context(app):
    with app.app_context():
        yield


@pytest.fixture
def cleanup(app_context):
    """本文件所有用例自建自清，避免污染全局 db_session 依赖。"""
    yield
    from models.erp import AccountMove, StockLocation

    for tbl in (
        ReconciliationEntry,
        ReconciliationBatch,
        AccountMove,
        StockMove,
        StockLocation,
        ReturnOrder,
        SaleOrder,
        PurchaseOrder,
        AccountingPeriod,
        Product,
        Supplier,
        Warehouse,
    ):
        _db.session.query(tbl).delete()
    _db.session.commit()


# ==================== P3-G3: 库位 ====================

class TestStockLocation:
    def test_create_and_list_location(self, app_context, cleanup):
        wh = Warehouse(code=f"WH-{_uid8()}", name="主仓")
        _db.session.add(wh)
        _db.session.commit()

        svc = ERPService()
        loc = svc.create_location(warehouse_id=wh.id, code="A-01", name="A 区 1 排")
        assert loc.id is not None and loc.is_active is True

        # 同仓库重复 code 拒绝
        with pytest.raises(ValueError, match="已存在"):
            svc.create_location(warehouse_id=wh.id, code="A-01")

        # 另一仓库同 code 放行
        wh2 = Warehouse(code=f"WH-{_uid8()}", name="二仓")
        _db.session.add(wh2)
        _db.session.commit()
        loc2 = svc.create_location(warehouse_id=wh2.id, code="A-01")
        assert loc2.id != loc.id

        items = svc.list_locations(warehouse_id=wh.id)
        assert [loc.code for loc in items] == ["A-01"]
        assert len(svc.list_locations()) == 2

    def test_transfer_with_location_and_stock_view(self, app_context, cleanup):
        sup = Supplier(name=f"S-{_uid8()}")
        _db.session.add(sup)
        wh1 = Warehouse(code=f"WH-{_uid8()}", name="一仓")
        wh2 = Warehouse(code=f"WH-{_uid8()}", name="二仓")
        _db.session.add_all([sup, wh1, wh2])
        _db.session.commit()
        prod = Product(sku=f"SKU-{_uid8()}", name="测试品", avg_cost=10)
        _db.session.add(prod)
        _db.session.commit()

        svc = ERPService()
        loc_a = svc.create_location(warehouse_id=wh1.id, code="A-01")
        loc_b = svc.create_location(warehouse_id=wh2.id, code="B-01")

        po = svc.create_purchase_order(
            supplier_id=sup.id, product_id=prod.id, qty=100, unit_price=10, warehouse_id=wh1.id
        )
        svc.confirm_purchase_order(po.id)
        svc.receive_purchase_order(po.id)

        # 调拨 30 到二仓 B-01，源库位 A-01
        svc.transfer_stock(
            product_id=prod.id,
            qty=30,
            from_warehouse_id=wh1.id,
            to_warehouse_id=wh2.id,
            from_location_id=loc_a.id,
            to_location_id=loc_b.id,
        )

        stock_a = svc.location_stock(loc_a.id)
        stock_b = svc.location_stock(loc_b.id)
        # 入库 100 记在源仓库（无库位——历史/未指定库位不入库位视图），
        # 调拨出库 30 挂 A-01 → A-01 视图 = -30（该库位无入库事件）
        assert {i["product_id"]: i["qty"] for i in stock_a["items"]}[prod.id] == -30
        assert {i["product_id"]: i["qty"] for i in stock_b["items"]}[prod.id] == 30

    def test_transfer_rejects_location_from_wrong_warehouse(self, app_context, cleanup):
        sup = Supplier(name=f"S-{_uid8()}")
        _db.session.add(sup)
        wh1 = Warehouse(code=f"WH-{_uid8()}", name="一仓")
        wh2 = Warehouse(code=f"WH-{_uid8()}", name="二仓")
        _db.session.add_all([sup, wh1, wh2])
        _db.session.commit()
        prod = Product(sku=f"SKU-{_uid8()}", name="测试品")
        _db.session.add(prod)
        _db.session.commit()

        svc = ERPService()
        loc_b = svc.create_location(warehouse_id=wh2.id, code="B-01")
        po = svc.create_purchase_order(
            supplier_id=sup.id, product_id=prod.id, qty=10, unit_price=5, warehouse_id=wh1.id
        )
        svc.confirm_purchase_order(po.id)
        svc.receive_purchase_order(po.id)

        with pytest.raises(ValueError, match="不属于源仓库"):
            svc.transfer_stock(
                product_id=prod.id,
                qty=5,
                from_warehouse_id=wh1.id,
                to_warehouse_id=wh2.id,
                from_location_id=loc_b.id,  # B 属于 wh2，不属于 wh1
            )


# ==================== P3-G2: 多对一匹配 + 调节项 ====================

class TestMultiMatchAndAdjustment:
    def _make_batch_with_split_payment(self, app_context):
        """系统侧 130 元（1 条），外部侧同 reference_no 拆 100+30（2 条）。"""
        batch = ReconciliationBatch(period="2026-10", batch_no=f"REC-{_uid8()}", created_by="t")
        _db.session.add(batch)
        _db.session.commit()
        _db.session.add_all(
            [
                ReconciliationEntry(
                    batch_id=batch.id, source="erp", account_code="AR",
                    entry_date=_dt(), amount=130, direction="debit",
                    reference_no="INV-001", tax_rate=0,
                ),
                ReconciliationEntry(
                    batch_id=batch.id, source="bank", account_code="BANK",
                    entry_date=_dt(), amount=100, direction="credit",
                    reference_no="INV-001",
                ),
                ReconciliationEntry(
                    batch_id=batch.id, source="bank", account_code="BANK",
                    entry_date=_dt(), amount=30, direction="credit",
                    reference_no="INV-001",
                ),
            ]
        )
        _db.session.commit()
        return batch

    def test_multi_match_pairs_split_payments(self, app_context, cleanup):
        batch = self._make_batch_with_split_payment(app_context)
        result = ReconciliationService().auto_match(batch.id)
        assert result["matched_count"] == 1
        assert result["unmatched_system"] == 0
        assert result["unmatched_external"] == 0

        sys_e = ReconciliationEntry.query.filter_by(batch_id=batch.id, source="erp").first()
        bank_entries = ReconciliationEntry.query.filter_by(
            batch_id=batch.id, source="bank"
        ).all()
        assert sys_e.matched and sys_e.match_type == "multi"
        assert all(b.matched and b.match_type == "multi" for b in bank_entries)
        # 星型：erp -> 组合首条；组合成员 -> erp
        assert sys_e.matched_entry_id == bank_entries[0].id
        assert {b.matched_entry_id for b in bank_entries} == {sys_e.id}

    def test_multi_match_respects_tolerance(self, app_context, cleanup):
        """组合和超出容差（tax_rate=0 → 容差 0.01）不得误配。"""
        batch = self._make_batch_with_split_payment(app_context)
        # 把一条银行明细改成 100.5：组合和 130.5，差 0.5 > 0.01
        be = ReconciliationEntry.query.filter_by(batch_id=batch.id, source="bank").order_by(
            ReconciliationEntry.id
        ).all()
        be[0].amount = 100.5
        _db.session.commit()

        result = ReconciliationService().auto_match(batch.id)
        assert result["matched_count"] == 0
        assert result["unmatched_system"] == 1

    def test_adjustment_balances_batch(self, app_context, cleanup):
        """调节项 + 重跑匹配 → 批次配平可关闭（P3-G2 完整闭环）。"""
        batch = self._make_batch_with_split_payment(app_context)
        # 外部侧只有 100（另 30 为手续费无法收回 → 调节项核销）
        be = ReconciliationEntry.query.filter_by(batch_id=batch.id, source="bank").order_by(
            ReconciliationEntry.id
        ).all()
        _db.session.delete(be[1])
        _db.session.commit()

        svc = ReconciliationService()
        svc.auto_match(batch.id)  # 1:1 配平 100 那条？130 vs 100 tax_rate=0 容差 0.01 → 不中
        result1 = ReconciliationService().auto_match(batch.id)
        assert result1["unmatched_system"] == 1

        adj = svc.create_adjustment(
            batch_id=batch.id,
            reference_no="INV-001",
            amount=30,
            direction="credit",
            reason="银行手续费，经审批核销",
            actor="tester",
        )
        assert adj.source == "manual" and adj.matched is False

        # 参数校验
        with pytest.raises(ValueError, match="正数"):
            svc.create_adjustment(batch.id, "INV-001", -1, "credit", "x")
        with pytest.raises(ValueError, match="必填"):
            svc.create_adjustment(batch.id, "INV-001", 1, "credit", "  ")

        result2 = ReconciliationService().auto_match(batch.id)
        assert result2["unmatched_system"] == 0
        assert result2["unmatched_external"] == 0
        assert ReconciliationBatch.query.get(batch.id).status == "matched"

        # 配平后可关闭
        assert svc.close_batch(batch.id, closed_by="tester") is True


def _dt():
    from datetime import datetime

    return datetime(2026, 10, 20, 12, 0, 0)


# ==================== P3-G1: 试算平衡 ====================

class TestTrialBalance:
    def test_trial_balance_aggregates_and_finds_dangling(self, app_context, cleanup):
        sup = Supplier(name=f"S-{_uid8()}")
        _db.session.add(sup)
        _db.session.commit()
        prod = Product(sku=f"SKU-{_uid8()}", name="测试品", avg_cost=10, tax_rate=0.13)
        _db.session.add(prod)
        _db.session.commit()

        svc = ERPService()
        # 采购 100@10 确认 → 应付 1000 + 税 130
        po = svc.create_purchase_order(
            supplier_id=sup.id, product_id=prod.id, qty=100, unit_price=10
        )
        svc.confirm_purchase_order(po.id)

        tb = svc.trial_balance()
        by_key = {(a["account"], a["ref_type"]): a for a in tb["accounts"]}
        ap = by_key[("应付账款", "payable")]
        assert ap["net_amount"] == 1000.0
        assert ap["tax_amount"] == 130.0
        assert tb["totals"]["entry_count"] >= 1
        assert tb["dangling_refs"] == []

        # 伪造断链：直接删采购单（流水仍在）→ dangling_refs 出现
        _db.session.delete(po)
        _db.session.commit()
        tb2 = svc.trial_balance()
        assert any(d["ref_order"] == po.order_no for d in tb2["dangling_refs"])
