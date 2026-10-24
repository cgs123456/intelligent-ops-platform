"""财务对账服务测试

覆盖：
- 创建对账批次（含 period 格式校验）
- 导入外部流水明细 + 自动匹配（金额匹配场景）
- 差异金额计算（系统侧 vs 外部侧）
- 关闭批次（有未匹配时应失败，全匹配时应成功）
"""

import pytest

from extensions import db
from models.finance import ReconciliationBatch, ReconciliationEntry
from services.finance_recon_service import ReconciliationService


@pytest.fixture(autouse=True)
def _cleanup_finance_tables(app):
    """每个测试前后清理财务对账表 + AccountMove，避免数据污染（service 方法会 commit，db_session fixture 失效）"""
    from models.erp import AccountMove

    with app.app_context():
        for model in [ReconciliationEntry, ReconciliationBatch, AccountMove]:
            db.session.query(model).delete()
        db.session.commit()
    yield
    with app.app_context():
        for model in [ReconciliationEntry, ReconciliationBatch, AccountMove]:
            db.session.query(model).delete()
        db.session.commit()


@pytest.mark.unit
class TestReconciliationBatch:
    """对账批次基本测试"""

    def test_create_batch_success(self, app):
        """创建批次：状态 draft，金额为 0"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07", created_by="tester")
            assert batch.id is not None
            assert batch.batch_no.startswith("REC-")
            assert batch.period == "2026-07"
            assert batch.status == "draft"
            assert batch.created_by == "tester"
            assert float(batch.total_system_amount) == 0.0
            assert float(batch.total_external_amount) == 0.0

    def test_create_batch_invalid_period(self, app):
        """period 格式非法时抛 ValueError"""
        with app.app_context():
            svc = ReconciliationService()
            with pytest.raises(ValueError, match="对账期间格式非法"):
                svc.create_batch(period="2026/07")
            with pytest.raises(ValueError, match="对账期间格式非法"):
                svc.create_batch(period="2026-13")  # 月份非法

    def test_create_batch_default_created_by(self, app):
        """created_by 默认 system"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            assert batch.created_by == "system"


@pytest.mark.unit
class TestReconciliationImportAndMatch:
    """导入明细 + 自动匹配测试"""

    def test_import_entries_success(self, app):
        """导入明细：返回导入条数，批次状态升级为 pending"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            entries = [
                {
                    "source": "bank",
                    "account_code": "BANK-001",
                    "entry_date": "2026-07-15",
                    "amount": "100.00",
                    "direction": "credit",
                    "reference_no": "SO-001",
                },
                {
                    "source": "bank",
                    "account_code": "BANK-001",
                    "entry_date": "2026-07-16",
                    "amount": "200.00",
                    "direction": "credit",
                    "reference_no": "SO-002",
                },
            ]
            imported = svc.import_entries(batch.id, entries)
            assert imported == 2
            # 状态升级为 pending
            assert batch.status == "pending"
            # 入库条数
            rows = ReconciliationEntry.query.filter_by(batch_id=batch.id).all()
            assert len(rows) == 2

    def test_import_entries_missing_field(self, app):
        """明细缺少必填字段时抛 ValueError"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            with pytest.raises(ValueError, match="缺少 account_code"):
                svc.import_entries(batch.id, [{"entry_date": "2026-07-15", "amount": "100", "direction": "debit"}])

    def test_import_entries_invalid_direction(self, app):
        """direction 非法时抛 ValueError"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            with pytest.raises(ValueError, match="direction 必须为"):
                svc.import_entries(
                    batch.id,
                    [
                        {
                            "account_code": "BANK-001",
                            "entry_date": "2026-07-15",
                            "amount": "100",
                            "direction": "invalid",
                        }
                    ],
                )

    def test_auto_match_full_matched(self, app):
        """构造金额 + 单号完全匹配场景：matched_count > 0，无未匹配，批次状态 matched"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")

            # 系统侧明细（source=erp）
            sys_entries = [
                {
                    "source": "erp",
                    "account_code": "应收账款",
                    "entry_date": "2026-07-15",
                    "amount": "100.00",
                    "direction": "debit",
                    "reference_no": "SO-001",
                },
                {
                    "source": "erp",
                    "account_code": "应收账款",
                    "entry_date": "2026-07-16",
                    "amount": "200.00",
                    "direction": "debit",
                    "reference_no": "SO-002",
                },
            ]
            svc.import_entries(batch.id, sys_entries)

            # 外部侧明细（source=bank）—— 完全匹配
            ext_entries = [
                {
                    "source": "bank",
                    "account_code": "BANK-001",
                    "entry_date": "2026-07-15",
                    "amount": "100.00",
                    "direction": "credit",
                    "reference_no": "SO-001",
                },
                {
                    "source": "bank",
                    "account_code": "BANK-001",
                    "entry_date": "2026-07-16",
                    "amount": "200.00",
                    "direction": "credit",
                    "reference_no": "SO-002",
                },
            ]
            svc.import_entries(batch.id, ext_entries)

            result = svc.auto_match(batch.id)
            assert result["matched_count"] == 2
            assert result["unmatched_system"] == 0
            assert result["unmatched_external"] == 0
            assert batch.status == "matched"

            # 校验 matched_entry_id 已互相指向
            sys_e = ReconciliationEntry.query.filter_by(batch_id=batch.id, source="erp", reference_no="SO-001").first()
            ext_e = ReconciliationEntry.query.filter_by(batch_id=batch.id, source="bank", reference_no="SO-001").first()
            assert sys_e.matched is True
            assert ext_e.matched is True
            assert sys_e.matched_entry_id == ext_e.id
            assert ext_e.matched_entry_id == sys_e.id

    def test_auto_match_with_discrepancy(self, app):
        """构造金额不匹配场景：存在未匹配，批次状态 discrepancy"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")

            # 系统侧 100，外部侧 150（金额不一致 → 未匹配）
            svc.import_entries(
                batch.id,
                [
                    {
                        "source": "erp",
                        "account_code": "应收账款",
                        "entry_date": "2026-07-15",
                        "amount": "100.00",
                        "direction": "debit",
                        "reference_no": "SO-001",
                    }
                ],
            )
            svc.import_entries(
                batch.id,
                [
                    {
                        "source": "bank",
                        "account_code": "BANK-001",
                        "entry_date": "2026-07-15",
                        "amount": "150.00",
                        "direction": "credit",
                        "reference_no": "SO-001",
                    }
                ],
            )
            result = svc.auto_match(batch.id)
            assert result["matched_count"] == 0
            assert result["unmatched_system"] == 1
            assert result["unmatched_external"] == 1
            assert batch.status == "discrepancy"


@pytest.mark.unit
class TestReconciliationTotalsAndClose:
    """差异计算 + 关闭批次测试"""

    def test_calculate_totals(self, app):
        """差异 = 系统侧合计 - 外部侧合计"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            svc.import_entries(
                batch.id,
                [
                    {
                        "source": "erp",
                        "account_code": "应收账款",
                        "entry_date": "2026-07-15",
                        "amount": "300.00",
                        "direction": "debit",
                        "reference_no": "SO-001",
                    },
                    {
                        "source": "bank",
                        "account_code": "BANK-001",
                        "entry_date": "2026-07-15",
                        "amount": "250.00",
                        "direction": "credit",
                        "reference_no": "SO-001",
                    },
                ],
            )
            totals = svc.calculate_totals(batch.id)
            assert float(totals["total_system_amount"]) == 300.0
            assert float(totals["total_external_amount"]) == 250.0
            assert float(totals["discrepancy_amount"]) == 50.0

            # 同步更新到批次
            batch_db = db.session.get(ReconciliationBatch, batch.id)
            assert float(batch_db.total_system_amount) == 300.0
            assert float(batch_db.discrepancy_amount) == 50.0

    def test_close_batch_with_unmatched_fails(self, app):
        """有未匹配明细时关闭批次应失败"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            # 仅导入系统侧（无外部侧匹配），auto_match 后必有未匹配
            svc.import_entries(
                batch.id,
                [
                    {
                        "source": "erp",
                        "account_code": "应收账款",
                        "entry_date": "2026-07-15",
                        "amount": "100.00",
                        "direction": "debit",
                        "reference_no": "SO-001",
                    }
                ],
            )
            svc.auto_match(batch.id)
            assert batch.status == "discrepancy"
            with pytest.raises(ValueError, match="存在未匹配明细"):
                svc.close_batch(batch.id)

    def test_close_batch_success(self, app):
        """全部匹配时关闭批次成功，状态 closed，closed_at 写入"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            svc.import_entries(
                batch.id,
                [
                    {
                        "source": "erp",
                        "account_code": "应收账款",
                        "entry_date": "2026-07-15",
                        "amount": "100.00",
                        "direction": "debit",
                        "reference_no": "SO-001",
                    },
                    {
                        "source": "bank",
                        "account_code": "BANK-001",
                        "entry_date": "2026-07-15",
                        "amount": "100.00",
                        "direction": "credit",
                        "reference_no": "SO-001",
                    },
                ],
            )
            svc.auto_match(batch.id)
            assert batch.status == "matched"
            ok = svc.close_batch(batch.id, closed_by="finance_manager")
            assert ok is True
            assert batch.status == "closed"
            assert batch.closed_at is not None

    def test_close_batch_tax_tolerance_match_succeeds(self, app):
        """P0-4: 税容差内匹配（113 vs 100，diff 13 ≤ 113*13%=14.69）的批次必须能关账。

        原实现以汇总 |discrepancy_amount|>0.01 一刀切，此类批次匹配成功却永远无法关闭（自锁）。
        """
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            svc.import_entries(
                batch.id,
                [
                    {
                        "source": "erp",
                        "account_code": "应收账款",
                        "entry_date": "2026-07-15",
                        "amount": "113.00",
                        "direction": "debit",
                        "reference_no": "SO-TAX",
                    },
                    {
                        "source": "bank",
                        "account_code": "BANK-001",
                        "entry_date": "2026-07-15",
                        "amount": "100.00",
                        "direction": "credit",
                        "reference_no": "SO-TAX",
                    },
                ],
            )
            svc.auto_match(batch.id)
            assert batch.status == "matched", "税容差内应被 auto_match 匹配"
            ok = svc.close_batch(batch.id, closed_by="finance_manager")
            assert ok is True
            assert batch.status == "closed"

    def test_close_batch_matched_pair_beyond_tolerance_fails(self, app):
        """匹配对金额事后漂移超出税容差时必须拒绝关账（P0-4 新关账闸门）"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            svc.import_entries(
                batch.id,
                [
                    {
                        "source": "erp",
                        "account_code": "应收账款",
                        "entry_date": "2026-07-15",
                        "amount": "100.00",
                        "direction": "debit",
                        "reference_no": "SO-BAD",
                    },
                    {
                        "source": "bank",
                        "account_code": "BANK-001",
                        "entry_date": "2026-07-15",
                        "amount": "100.00",
                        "direction": "credit",
                        "reference_no": "SO-BAD",
                    },
                ],
            )
            svc.auto_match(batch.id)
            assert batch.status == "matched"
            # 模拟落库后系统侧金额被调整：匹配对差异 50 > max(150*13%, 0.01)=19.5
            sys_entry = (
                db.session.query(ReconciliationEntry).filter_by(batch_id=batch.id, source="erp").first()
            )
            sys_entry.amount = 150
            db.session.commit()
            with pytest.raises(ValueError, match="税容差"):
                svc.close_batch(batch.id)

    def test_close_batch_already_closed_fails(self, app):
        """已关闭批次不可重复关闭"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            svc.import_entries(
                batch.id,
                [
                    {
                        "source": "erp",
                        "account_code": "应收账款",
                        "entry_date": "2026-07-15",
                        "amount": "100.00",
                        "direction": "debit",
                        "reference_no": "SO-001",
                    },
                    {
                        "source": "bank",
                        "account_code": "BANK-001",
                        "entry_date": "2026-07-15",
                        "amount": "100.00",
                        "direction": "credit",
                        "reference_no": "SO-001",
                    },
                ],
            )
            svc.auto_match(batch.id)
            svc.close_batch(batch.id)
            with pytest.raises(ValueError, match="已关闭"):
                svc.close_batch(batch.id)


@pytest.mark.unit
class TestReconciliationBatchNotFound:
    """批次不存在场景测试"""

    def test_import_entries_batch_not_found(self, app):
        with app.app_context():
            svc = ReconciliationService()
            with pytest.raises(ValueError, match="对账批次不存在"):
                svc.import_entries(99999, [])

    def test_auto_match_batch_not_found(self, app):
        with app.app_context():
            svc = ReconciliationService()
            with pytest.raises(ValueError, match="对账批次不存在"):
                svc.auto_match(99999)

    def test_close_batch_not_found(self, app):
        with app.app_context():
            svc = ReconciliationService()
            with pytest.raises(ValueError, match="对账批次不存在"):
                svc.close_batch(99999)


@pytest.mark.unit
class TestLoadSystemEntries:
    """load_system_entries 系统侧明细加载测试"""

    def test_load_system_entries_success(self, app):
        """从 AccountMove 加载系统侧明细：receivable→debit，refund→credit"""
        with app.app_context():
            from datetime import datetime

            from models.erp import AccountMove

            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")

            # 创建 AccountMove：2 条应收 + 1 条退款
            db.session.add_all(
                [
                    AccountMove(
                        ref_type="receivable",
                        ref_order="SO-001",
                        amount=100,
                        account="应收账款",
                        tax_amount=13,
                        created_at=datetime(2026, 7, 15),
                    ),
                    AccountMove(
                        ref_type="receivable",
                        ref_order="SO-002",
                        amount=200,
                        account="应收账款",
                        tax_amount=26,
                        created_at=datetime(2026, 7, 20),
                    ),
                    AccountMove(
                        ref_type="refund",
                        ref_order="SO-001",
                        amount=50,
                        account="应收账款",
                        tax_amount=6.5,
                        created_at=datetime(2026, 7, 25),
                    ),
                ]
            )
            db.session.commit()

            result = svc.load_system_entries(batch.id)
            assert len(result) == 3
            # 验证方向映射
            # directions mapping verified below
            # receivable → debit，refund → credit
            refund_entry = [r for r in result if r["direction"] == "credit"]
            assert len(refund_entry) == 1
            assert refund_entry[0]["reference_no"] == "SO-001"

            debit_entries = [r for r in result if r["direction"] == "debit"]
            assert len(debit_entries) == 2

    def test_load_system_entries_idempotent(self, app):
        """P1-15: 重复调用 load_system_entries 不应导致明细翻倍"""
        with app.app_context():
            from datetime import datetime

            from models.erp import AccountMove

            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")

            db.session.add_all(
                [
                    AccountMove(
                        ref_type="receivable",
                        ref_order="SO-001",
                        amount=100,
                        account="应收账款",
                        tax_amount=13,
                        created_at=datetime(2026, 7, 15),
                    ),
                    AccountMove(
                        ref_type="receivable",
                        ref_order="SO-002",
                        amount=200,
                        account="应收账款",
                        tax_amount=26,
                        created_at=datetime(2026, 7, 20),
                    ),
                ]
            )
            db.session.commit()

            # 第一次加载
            result1 = svc.load_system_entries(batch.id)
            assert len(result1) == 2

            # 第二次加载（幂等：应先删除旧记录再重新加载，不翻倍）
            result2 = svc.load_system_entries(batch.id)
            assert len(result2) == 2

            # 数据库中只有 2 条 source=erp 的明细（不是 4 条）
            count = ReconciliationEntry.query.filter_by(batch_id=batch.id, source="erp").count()
            assert count == 2

    def test_load_system_entries_period_filter(self, app):
        """load_system_entries 仅加载期间内的 AccountMove"""
        with app.app_context():
            from datetime import datetime

            from models.erp import AccountMove

            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")

            # 7 月的（应加载）
            db.session.add(
                AccountMove(
                    ref_type="receivable",
                    ref_order="SO-JUL",
                    amount=100,
                    account="应收账款",
                    tax_amount=13,
                    created_at=datetime(2026, 7, 15),
                )
            )
            # 6 月的（不应加载）
            db.session.add(
                AccountMove(
                    ref_type="receivable",
                    ref_order="SO-JUN",
                    amount=200,
                    account="应收账款",
                    tax_amount=26,
                    created_at=datetime(2026, 6, 15),
                )
            )
            # 8 月的（不应加载）
            db.session.add(
                AccountMove(
                    ref_type="receivable",
                    ref_order="SO-AUG",
                    amount=300,
                    account="应收账款",
                    tax_amount=39,
                    created_at=datetime(2026, 8, 15),
                )
            )
            db.session.commit()

            result = svc.load_system_entries(batch.id)
            assert len(result) == 1
            assert result[0]["reference_no"] == "SO-JUL"

    def test_load_system_entries_batch_not_found(self, app):
        """load_system_entries：批次不存在时抛 ValueError"""
        with app.app_context():
            svc = ReconciliationService()
            with pytest.raises(ValueError, match="对账批次不存在"):
                svc.load_system_entries(99999)

    def test_load_system_entries_empty(self, app):
        """load_system_entries：无 AccountMove 时返回空列表"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            result = svc.load_system_entries(batch.id)
            assert result == []

    def test_load_system_entries_tax_rate_derivation(self, app):
        """load_system_entries：税率推导（tax_amount / amount）"""
        with app.app_context():
            from datetime import datetime

            from models.erp import AccountMove

            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")

            # amount=100, tax_amount=13 → tax_rate=0.13
            db.session.add(
                AccountMove(
                    ref_type="receivable",
                    ref_order="SO-TAX",
                    amount=100,
                    account="应收账款",
                    tax_amount=13,
                    created_at=datetime(2026, 7, 15),
                )
            )
            # amount=0, tax_amount=0 → 默认 0.13
            db.session.add(
                AccountMove(
                    ref_type="receivable",
                    ref_order="SO-ZERO",
                    amount=0,
                    account="应收账款",
                    tax_amount=0,
                    created_at=datetime(2026, 7, 16),
                )
            )
            db.session.commit()

            svc.load_system_entries(batch.id)
            entries = ReconciliationEntry.query.filter_by(batch_id=batch.id, source="erp").all()
            tax_rates = {e.reference_no: float(e.tax_rate) for e in entries}
            # SO-TAX: 13/100 = 0.13
            assert abs(tax_rates["SO-TAX"] - 0.13) < 0.001
            # SO-ZERO: amount=0 时默认 0.13
            assert abs(tax_rates["SO-ZERO"] - 0.13) < 0.001


@pytest.mark.unit
class TestReconciliationCalculateTotalsEdgeCases:
    """calculate_totals 边界场景测试"""

    def test_calculate_totals_empty_batch(self, app):
        """空批次的差异计算：全部为 0"""
        with app.app_context():
            svc = ReconciliationService()
            batch = svc.create_batch(period="2026-07")
            totals = svc.calculate_totals(batch.id)
            assert float(totals["total_system_amount"]) == 0.0
            assert float(totals["total_external_amount"]) == 0.0
            assert float(totals["discrepancy_amount"]) == 0.0

    def test_calculate_totals_batch_not_found(self, app):
        """calculate_totals：批次不存在时抛 ValueError"""
        with app.app_context():
            svc = ReconciliationService()
            with pytest.raises(ValueError, match="对账批次不存在"):
                svc.calculate_totals(99999)

    def test_close_batch_not_found_raises(self, app):
        """close_batch：批次不存在时抛 ValueError"""
        with app.app_context():
            svc = ReconciliationService()
            with pytest.raises(ValueError, match="对账批次不存在"):
                svc.close_batch(99999, closed_by="tester")
