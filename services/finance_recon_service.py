"""财务对账服务

实现 ERP 账务与银行流水/外部系统对账的核心业务逻辑：
    1. 创建对账批次（按期间）
    2. 导入外部流水明细（银行流水等）
    3. 从 ERP 系统加载系统侧明细（订单/退货凭证汇总）
    4. 自动匹配（按 reference_no + amount 精确匹配）
    5. 计算系统侧/外部侧合计金额与差异
    6. 关闭批次（要求无未匹配明细）

金额计算统一使用 Decimal 避免浮点误差。
"""

import logging
import random
from datetime import datetime
from decimal import Decimal

from extensions import db
from models.erp import AccountMove
from models.finance import ReconciliationBatch, ReconciliationEntry

logger = logging.getLogger(__name__)


class ReconciliationService:
    """财务对账服务（实例方法风格，支持外部注入 db_session）"""

    def __init__(self, db_session=None):
        # 允许外部注入会话（便于测试/分库），默认用全局 db.session
        self.db = db_session or db.session

    # ==================== 内部工具 ====================

    @staticmethod
    def _gen_batch_no() -> str:
        """生成对账批次号：REC-YYYYMMDDHHMMSS-NNNN"""
        return f'REC-{datetime.now().strftime("%Y%m%d%H%M%S")}-{random.randint(1000, 9999)}'

    @staticmethod
    def _to_decimal(value) -> Decimal:
        """安全转换为 Decimal，None/空值返回 0"""
        if value is None:
            return Decimal(0)
        return Decimal(value)

    # ==================== 业务方法 ====================

    def create_batch(self, period: str, created_by: str = "system") -> ReconciliationBatch:
        """创建对账批次

        :param period: 对账期间，格式 YYYY-MM（如 '2026-07'）
        :param created_by: 创建人
        :return: ReconciliationBatch 对象
        :raises ValueError: period 格式非法时抛出
        """
        if not period or len(period) != 7 or period[4] != "-":
            raise ValueError("对账期间格式非法，应为 YYYY-MM")
        try:
            datetime.strptime(period, "%Y-%m")
        except ValueError as e:
            raise ValueError(f"对账期间格式非法：{e}") from e

        try:
            batch = ReconciliationBatch(
                batch_no=self._gen_batch_no(),
                period=period,
                status="draft",
                total_system_amount=Decimal(0),
                total_external_amount=Decimal(0),
                discrepancy_amount=Decimal(0),
                created_by=created_by or "system",
            )
            self.db.add(batch)
            self.db.commit()
            logger.info("对账批次已创建: %s period=%s", batch.batch_no, period)
            return batch
        except Exception as e:
            self.db.rollback()
            logger.exception("创建对账批次失败: %s", e)
            raise

    def import_entries(self, batch_id: int, entries: list) -> int:
        """导入外部流水明细到指定批次

        :param batch_id: 对账批次 ID
        :param entries: 明细列表，每项为 dict，支持字段：
            - source: 来源（erp/bank/manual），默认 bank
            - account_code: 账户代码（必填）
            - entry_date: 明细日期（YYYY-MM-DD 字符串或 datetime，必填）
            - amount: 金额（正数，必填）
            - direction: debit/credit（必填）
            - reference_no: 业务单号（可选）
        :return: 导入条数
        :raises ValueError: 批次不存在 / 状态非 draft|pending / 字段缺失时抛出
        """
        batch = self.db.get(ReconciliationBatch, batch_id)
        if not batch:
            raise ValueError(f"对账批次不存在: {batch_id}")
        if batch.status not in ("draft", "pending"):
            raise ValueError(f"批次状态 {batch.status} 不可导入明细")

        if not isinstance(entries, list):
            raise ValueError("entries 必须为列表")

        imported = 0
        try:
            for idx, item in enumerate(entries):
                if not isinstance(item, dict):
                    raise ValueError(f"第 {idx + 1} 条明细非 dict")
                account_code = item.get("account_code")
                entry_date = item.get("entry_date")
                amount = item.get("amount")
                direction = item.get("direction")

                if not account_code:
                    raise ValueError(f"第 {idx + 1} 条明细缺少 account_code")
                if entry_date is None:
                    raise ValueError(f"第 {idx + 1} 条明细缺少 entry_date")
                if amount is None:
                    raise ValueError(f"第 {idx + 1} 条明细缺少 amount")
                if direction not in ("debit", "credit"):
                    raise ValueError(f"第 {idx + 1} 条明细 direction 必须为 debit/credit")

                # 解析日期
                if isinstance(entry_date, str):
                    try:
                        entry_date_parsed = datetime.strptime(entry_date, "%Y-%m-%d")
                    except ValueError as e:
                        raise ValueError(f"第 {idx + 1} 条明细 entry_date 格式非法：{e}") from e
                elif isinstance(entry_date, datetime):
                    entry_date_parsed = entry_date
                else:
                    raise ValueError(f"第 {idx + 1} 条明细 entry_date 类型非法")

                amount_dec = self._to_decimal(amount)
                if amount_dec < 0:
                    raise ValueError(f"第 {idx + 1} 条明细 amount 不能为负")

                entry = ReconciliationEntry(
                    batch_id=batch_id,
                    source=item.get("source", "bank"),
                    account_code=account_code,
                    entry_date=entry_date_parsed,
                    amount=amount_dec,
                    direction=direction,
                    reference_no=item.get("reference_no"),
                    matched=False,
                )
                self.db.add(entry)
                imported += 1

            # 批次状态升级为 pending
            if batch.status == "draft":
                batch.status = "pending"

            self.db.commit()
            logger.info("对账批次 %s 导入 %d 条明细", batch.batch_no, imported)
            return imported
        except Exception as e:
            self.db.rollback()
            logger.exception("导入对账明细失败: %s", e)
            raise

    def load_system_entries(self, batch_id: int) -> list:
        """从 ERP 订单/退货汇总系统侧明细并写入对账明细表

        将 ERP 的应收凭证（AccountMove.ref_type=receivable）和退款凭证（refund）
        汇总为系统侧对账明细（source=erp），按 reference_no + amount 维度。

        :param batch_id: 对账批次 ID
        :return: 系统侧明细列表（list[dict]）
        :raises ValueError: 批次不存在时抛出
        """
        batch = self.db.get(ReconciliationBatch, batch_id)
        if not batch:
            raise ValueError(f"对账批次不存在: {batch_id}")

        try:
            # 按期间过滤 AccountMove（created_at 在 period 范围内）
            period = batch.period  # YYYY-MM
            try:
                year, month = period.split("-")
                period_start = datetime(int(year), int(month), 1)
                # 期间结束 = 次月 1 号（用于 < 比较）
                if int(month) == 12:
                    period_end = datetime(int(year) + 1, 1, 1)
                else:
                    period_end = datetime(int(year), int(month) + 1, 1)
            except (ValueError, AttributeError) as e:
                raise ValueError(f"批次 period 格式非法：{e}") from e

            entries = (
                self.db.query(AccountMove)
                .filter(AccountMove.created_at >= period_start)
                .filter(AccountMove.created_at < period_end)
                .filter(AccountMove.ref_type.in_(("receivable", "refund")))
                .all()
            )

            result = []
            for mv in entries:
                # refund 视为贷方（红字冲减应收），receivable 视为借方
                direction = "credit" if mv.ref_type == "refund" else "debit"
                amount_dec = self._to_decimal(mv.amount)
                entry = ReconciliationEntry(
                    batch_id=batch_id,
                    source="erp",
                    account_code=mv.account or "应收账款",
                    entry_date=mv.created_at or datetime.now(),
                    amount=amount_dec,
                    direction=direction,
                    reference_no=mv.ref_order,
                    matched=False,
                )
                self.db.add(entry)
                result.append(
                    {
                        "id": None,  # flush 后才有 id，此处返回前赋值
                        "source": "erp",
                        "account_code": entry.account_code,
                        "entry_date": entry.entry_date.isoformat() if entry.entry_date else None,
                        "amount": float(amount_dec),
                        "direction": direction,
                        "reference_no": entry.reference_no,
                    }
                )

            self.db.flush()
            # 回填 id
            created = (
                self.db.query(ReconciliationEntry)
                .filter_by(batch_id=batch_id, source="erp")
                .order_by(ReconciliationEntry.id.desc())
                .limit(len(result))
                .all()
            )
            for idx, ent in enumerate(reversed(created)):
                if idx < len(result):
                    result[idx]["id"] = ent.id

            self.db.commit()
            logger.info("对账批次 %s 加载系统侧明细 %d 条", batch.batch_no, len(result))
            return result
        except Exception as e:
            self.db.rollback()
            logger.exception("加载系统侧明细失败: %s", e)
            raise

    def auto_match(self, batch_id: int) -> dict:
        """自动匹配：按 reference_no + amount 精确匹配系统侧与外部侧明细

        匹配规则：
            - source=erp 与 source in (bank/manual) 的明细互相匹配
            - reference_no 必须相同（且非空）
            - amount 必须相等（Decimal 精确比较）
            - 已 matched 的明细不再参与匹配
            - 一对一匹配（每条 erp 明细最多匹配一条外部明细）

        :param batch_id: 对账批次 ID
        :return: {"matched_count": int, "unmatched_system": int, "unmatched_external": int}
        :raises ValueError: 批次不存在时抛出
        """
        batch = self.db.get(ReconciliationBatch, batch_id)
        if not batch:
            raise ValueError(f"对账批次不存在: {batch_id}")

        try:
            system_entries = (
                self.db.query(ReconciliationEntry)
                .filter_by(batch_id=batch_id, source="erp", matched=False)
                .all()
            )
            external_entries = (
                self.db.query(ReconciliationEntry)
                .filter(
                    ReconciliationEntry.batch_id == batch_id,
                    ReconciliationEntry.source != "erp",
                    ReconciliationEntry.matched.is_(False),
                )
                .all()
            )

            # 按 (reference_no, amount) 建外部明细索引，便于 O(1) 查找
            ext_map: dict = {}
            for ext in external_entries:
                if not ext.reference_no:
                    continue
                key = (ext.reference_no, self._to_decimal(ext.amount))
                ext_map.setdefault(key, []).append(ext)

            matched_count = 0
            for sys_e in system_entries:
                if not sys_e.reference_no:
                    continue
                key = (sys_e.reference_no, self._to_decimal(sys_e.amount))
                candidates = ext_map.get(key)
                if not candidates:
                    continue
                ext_e = candidates.pop(0)  # 取第一条可用
                sys_e.matched = True
                sys_e.matched_entry_id = ext_e.id
                sys_e.mismatch_reason = None
                ext_e.matched = True
                ext_e.matched_entry_id = sys_e.id
                ext_e.mismatch_reason = None
                matched_count += 1

            # 计算未匹配数
            unmatched_system = (
                self.db.query(ReconciliationEntry)
                .filter_by(batch_id=batch_id, source="erp", matched=False)
                .count()
            )
            unmatched_external = (
                self.db.query(ReconciliationEntry)
                .filter(
                    ReconciliationEntry.batch_id == batch_id,
                    ReconciliationEntry.source != "erp",
                    ReconciliationEntry.matched.is_(False),
                )
                .count()
            )

            # 标记未匹配原因
            for ue in (
                self.db.query(ReconciliationEntry)
                .filter_by(batch_id=batch_id, matched=False)
                .all()
            ):
                if not ue.mismatch_reason:
                    ue.mismatch_reason = "未找到对应明细"

            # 更新批次状态
            if unmatched_system == 0 and unmatched_external == 0:
                batch.status = "matched"
            else:
                batch.status = "discrepancy"

            self.db.commit()
            result = {
                "matched_count": matched_count,
                "unmatched_system": unmatched_system,
                "unmatched_external": unmatched_external,
            }
            logger.info("对账批次 %s 自动匹配: %s", batch.batch_no, result)
            return result
        except Exception as e:
            self.db.rollback()
            logger.exception("自动匹配失败: %s", e)
            raise

    def calculate_totals(self, batch_id: int) -> dict:
        """计算系统侧/外部侧合计金额与差异，并更新批次

        差异 = 系统侧合计 - 外部侧合计
        借方/贷方方向不影响金额合计（按绝对值累加），仅用于参考。

        :param batch_id: 对账批次 ID
        :return: {"total_system_amount": float, "total_external_amount": float, "discrepancy_amount": float}
        :raises ValueError: 批次不存在时抛出
        """
        batch = self.db.get(ReconciliationBatch, batch_id)
        if not batch:
            raise ValueError(f"对账批次不存在: {batch_id}")

        try:
            # 系统侧合计（source=erp）
            system_total = (
                self.db.query(db.func.sum(ReconciliationEntry.amount))
                .filter_by(batch_id=batch_id, source="erp")
                .scalar()
            ) or Decimal(0)
            system_total = self._to_decimal(system_total)

            # 外部侧合计（source != erp）
            external_total = (
                self.db.query(db.func.sum(ReconciliationEntry.amount))
                .filter(ReconciliationEntry.batch_id == batch_id, ReconciliationEntry.source != "erp")
                .scalar()
            ) or Decimal(0)
            external_total = self._to_decimal(external_total)

            discrepancy = system_total - external_total

            batch.total_system_amount = system_total
            batch.total_external_amount = external_total
            batch.discrepancy_amount = discrepancy

            self.db.commit()
            result = {
                "total_system_amount": float(system_total),
                "total_external_amount": float(external_total),
                "discrepancy_amount": float(discrepancy),
            }
            logger.info("对账批次 %s 金额计算: %s", batch.batch_no, result)
            return result
        except Exception as e:
            self.db.rollback()
            logger.exception("金额计算失败: %s", e)
            raise

    def close_batch(self, batch_id: int, closed_by: str = "system") -> bool:
        """关闭对账批次

        关闭前置条件：批次无未匹配明细（unmatched_system=0 且 unmatched_external=0）
        若存在未匹配，抛出 ValueError 提示差异未解决。

        :param batch_id: 对账批次 ID
        :param closed_by: 关闭人
        :return: True 表示关闭成功
        :raises ValueError: 批次不存在 / 状态非 matched/discrepancy / 存在未匹配时抛出
        """
        batch = self.db.get(ReconciliationBatch, batch_id)
        if not batch:
            raise ValueError(f"对账批次不存在: {batch_id}")
        if batch.status == "closed":
            raise ValueError("批次已关闭，不可重复关闭")
        if batch.status not in ("matched", "discrepancy"):
            raise ValueError(f"批次状态 {batch.status} 不可关闭，需先完成匹配")

        try:
            unmatched_system = (
                self.db.query(ReconciliationEntry)
                .filter_by(batch_id=batch_id, source="erp", matched=False)
                .count()
            )
            unmatched_external = (
                self.db.query(ReconciliationEntry)
                .filter(
                    ReconciliationEntry.batch_id == batch_id,
                    ReconciliationEntry.source != "erp",
                    ReconciliationEntry.matched.is_(False),
                )
                .count()
            )
            if unmatched_system > 0 or unmatched_external > 0:
                raise ValueError(
                    f"存在未匹配明细（系统侧 {unmatched_system} 条，外部侧 {unmatched_external} 条），"
                    f"无法关闭批次"
                )

            batch.status = "closed"
            batch.closed_at = datetime.now()
            self.db.commit()
            logger.info("对账批次 %s 已关闭 by=%s", batch.batch_no, closed_by)
            return True
        except Exception as e:
            self.db.rollback()
            logger.exception("关闭对账批次失败: %s", e)
            raise
