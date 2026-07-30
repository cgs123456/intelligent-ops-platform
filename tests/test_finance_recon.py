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
    """每个测试前后清理财务对账表，避免数据污染（service 方法会 commit，db_session fixture 失效）"""
    with app.app_context():
        for model in [ReconciliationEntry, ReconciliationBatch]:
            db.session.query(model).delete()
        db.session.commit()
    yield
    with app.app_context():
        for model in [ReconciliationEntry, ReconciliationBatch]:
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
            ext_e = ReconciliationEntry.query.filter_by(
                batch_id=batch.id, source="bank", reference_no="SO-001"
            ).first()
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
