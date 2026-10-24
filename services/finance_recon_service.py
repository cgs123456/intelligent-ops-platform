"""财务对账服务

实现 ERP 账务与银行流水/外部系统对账的核心业务逻辑：
    1. 创建对账批次（按期间）
    2. 导入外部流水明细（银行流水等）
    3. 从 ERP 系统加载系统侧明细（订单/退货凭证汇总）
    4. 自动匹配（按 reference_no + 金额容差匹配，容差覆盖税额）
    5. 计算系统侧/外部侧合计金额与差异
    6. 关闭批次（要求无未匹配明细）

金额计算统一使用 Decimal 避免浮点误差。
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from extensions import db
from models.erp import AccountMove
from models.finance import ReconciliationBatch, ReconciliationEntry
from models.system import AuditLog

logger = logging.getLogger(__name__)


class ReconciliationService:
    """财务对账服务（实例方法风格，支持外部注入 db_session）"""

    def __init__(self, db_session=None):
        # 允许外部注入会话（便于测试/分库），默认用全局 db.session
        self.db = db_session or db.session

    # ==================== 内部工具 ====================

    def _audit(self, action, detail, target_type="reconciliation_batch", target_id=None, actor="system"):
        """P2-22: 写审计日志（对应 Odoo auditlog，财务对账关键操作留痕）

        :param action: 动作（create/import/match/close 等）
        :param detail: 明细描述
        :param target_type: 操作对象类型，默认 reconciliation_batch
        :param target_id: 操作对象 ID
        :param actor: 操作人，默认 system
        """
        log = AuditLog(
            actor=actor or "system",
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=detail,
        )
        self.db.add(log)
        return log

    @staticmethod
    def _gen_batch_no() -> str:
        """生成对账批次号：REC-YYYYMMDDHHMMSS-XXXXXXXX

        P0/L2: 改用 uuid4 后缀 —— 原 random.randint(1000, 9999) 与 batch_no 的唯一约束
        冲突时直接 500（erp_service 的单号已改 uuid，此处对齐）。
        """
        return f'REC-{datetime.now().strftime("%Y%m%d%H%M%S")}-{uuid.uuid4().hex[:8]}'

    @staticmethod
    def _to_decimal(value) -> Decimal:
        """安全转换为 Decimal，None/空值返回 0"""
        if value is None:
            return Decimal(0)
        if isinstance(value, Decimal):
            return value
        # P2-23: 使用 Decimal(str(value)) 避免浮点直接转换导致的精度丢失
        # 例如 Decimal(0.1) 会产生 0.1000000000000000055511151231...
        # 而 Decimal(str(0.1)) 才能得到精确的 0.1
        return Decimal(str(value))

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
            self.db.flush()  # 取 batch.id 用于审计
            # P2-22: 审计日志
            self._audit(
                "create",
                f"创建对账批次 {batch.batch_no} period={period} by={created_by}",
                target_id=batch.id,
                actor=created_by,
            )
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

            # P2-22: 审计日志
            self._audit(
                "import",
                f"导入对账明细 batch={batch.batch_no} 条数={imported} 状态->{batch.status}",
                target_id=batch_id,
            )
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

            # P1-15: 幂等——先删除该批次 source=erp 的旧记录，再重新加载，避免明细翻倍
            self.db.query(ReconciliationEntry).filter_by(batch_id=batch_id, source="erp").delete()

            # P1-8: 加 order_by(AccountMove.id.asc()) 保证回填 id 顺序与查询顺序一致
            entries = (
                self.db.query(AccountMove)
                .filter(AccountMove.created_at >= period_start)
                .filter(AccountMove.created_at < period_end)
                .filter(AccountMove.ref_type.in_(("receivable", "refund")))
                .order_by(AccountMove.id.asc())
                .all()
            )

            result = []
            # P1-8: 保存创建的 entry 对象引用，flush 后直接读取 id（避免再次查询导致顺序错乱）
            created_entries = []
            for mv in entries:
                # refund 视为贷方（红字冲减应收），receivable 视为借方
                direction = "credit" if mv.ref_type == "refund" else "debit"
                amount_dec = self._to_decimal(mv.amount)
                # P0-ERP5: 从 AccountMove 推导税率（tax_amount / amount），用于后续容差匹配
                # AccountMove.amount 为不含税金额，tax_amount 为税额；推导失败时默认 0.13
                mv_tax = self._to_decimal(mv.tax_amount)
                if amount_dec > 0 and mv_tax > 0:
                    tax_rate_dec = mv_tax / amount_dec
                else:
                    tax_rate_dec = Decimal("0.13")
                entry = ReconciliationEntry(
                    batch_id=batch_id,
                    source="erp",
                    account_code=mv.account or "应收账款",
                    entry_date=mv.created_at or datetime.now(),
                    amount=amount_dec,
                    direction=direction,
                    reference_no=mv.ref_order,
                    matched=False,
                    tax_rate=tax_rate_dec,
                )
                self.db.add(entry)
                created_entries.append(entry)
                result.append(
                    {
                        "id": None,  # flush 后直接从 entry 对象读取
                        "source": "erp",
                        "account_code": entry.account_code,
                        "entry_date": entry.entry_date.isoformat() if entry.entry_date else None,
                        "amount": float(amount_dec),
                        "direction": direction,
                        "reference_no": entry.reference_no,
                    }
                )

            self.db.flush()
            # P1-8: 直接从已创建的 entry 对象读取 id（避免再次查询导致顺序错乱）
            for idx, entry in enumerate(created_entries):
                result[idx]["id"] = entry.id

            self.db.commit()
            logger.info("对账批次 %s 加载系统侧明细 %d 条", batch.batch_no, len(result))
            return result
        except Exception as e:
            self.db.rollback()
            logger.exception("加载系统侧明细失败: %s", e)
            raise

    def auto_match(self, batch_id: int) -> dict:
        """自动匹配：按 reference_no + 容差匹配系统侧与外部侧明细

        匹配规则：
            - source=erp 与 source in (bank/manual) 的明细互相匹配
            - reference_no 必须相同（且非空）
            - 金额容差匹配：|system_amount - external_amount| <= max(system_amount * tax_rate, 0.01)
              其中 tax_rate 从系统侧明细获取（来源于 AccountMove 税额推导），默认 0.13（标准增值税率）
              —— 原因：AccountMove.amount 为不含税金额，银行流水为含税实收金额，需容差覆盖税额
            - 已 matched 的明细不再参与匹配
            - 一对一匹配（每条 erp 明细最多匹配一条外部明细）
            - 匹配类型记录为 exact（差额 < 0.01）或 tolerance（容差内匹配），便于审计

        :param batch_id: 对账批次 ID
        :return: {"matched_count": int, "unmatched_system": int, "unmatched_external": int}
        :raises ValueError: 批次不存在时抛出
        """
        batch = self.db.get(ReconciliationBatch, batch_id)
        if not batch:
            raise ValueError(f"对账批次不存在: {batch_id}")

        try:
            # P1-7: 加行锁 SELECT FOR UPDATE，防止并发匹配导致同一条明细被多次匹配
            # with_for_update() 在 SQLite 下被忽略，PostgreSQL 下生效
            system_entries = (
                self.db.query(ReconciliationEntry)
                .filter_by(batch_id=batch_id, source="erp", matched=False)
                .with_for_update()
                .all()
            )
            external_entries = (
                self.db.query(ReconciliationEntry)
                .filter(
                    ReconciliationEntry.batch_id == batch_id,
                    ReconciliationEntry.source != "erp",
                    ReconciliationEntry.matched.is_(False),
                )
                .with_for_update()
                .all()
            )

            # 按 reference_no 建外部明细索引（不再按金额精确索引，改用容差匹配）
            ext_map: dict = {}
            for ext in external_entries:
                if not ext.reference_no:
                    continue
                ext_map.setdefault(ext.reference_no, []).append(ext)

            matched_count = 0
            for sys_e in system_entries:
                if not sys_e.reference_no:
                    continue
                candidates = ext_map.get(sys_e.reference_no)
                if not candidates:
                    continue

                sys_amount = self._to_decimal(sys_e.amount)
                # P0-ERP5: 容差 = max(|system_amount| * tax_rate, 0.01)
                # tax_rate 从系统侧明细获取（load_system_entries 已从 AccountMove 推导并写入），
                # 无法获取时默认 0.13（标准增值税率）
                tax_rate = getattr(sys_e, "tax_rate", None)
                if tax_rate is None:
                    tax_rate = Decimal("0.13")
                else:
                    tax_rate = self._to_decimal(tax_rate)
                tolerance = max(abs(sys_amount) * tax_rate, Decimal("0.01"))

                # 在候选中寻找容差内的匹配（取第一条满足条件的）
                matched_ext = None
                match_type = None
                for ext_e in candidates:
                    ext_amount = self._to_decimal(ext_e.amount)
                    diff = abs(sys_amount - ext_amount)
                    if diff <= tolerance:
                        matched_ext = ext_e
                        match_type = "exact" if diff < Decimal("0.01") else "tolerance"
                        break

                if matched_ext is None:
                    continue

                candidates.remove(matched_ext)
                sys_e.matched = True
                sys_e.matched_entry_id = matched_ext.id
                sys_e.mismatch_reason = None
                sys_e.match_type = match_type
                matched_ext.matched = True
                matched_ext.matched_entry_id = sys_e.id
                matched_ext.mismatch_reason = None
                matched_ext.match_type = match_type
                matched_count += 1

            # 计算未匹配数
            unmatched_system = (
                self.db.query(ReconciliationEntry).filter_by(batch_id=batch_id, source="erp", matched=False).count()
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
            for ue in self.db.query(ReconciliationEntry).filter_by(batch_id=batch_id, matched=False).all():
                if not ue.mismatch_reason:
                    ue.mismatch_reason = "未找到对应明细"

            # 更新批次状态
            if unmatched_system == 0 and unmatched_external == 0:
                batch.status = "matched"
            else:
                batch.status = "discrepancy"

            # P2-22: 审计日志
            self._audit(
                "match",
                f"自动匹配 batch={batch.batch_no} matched={matched_count} "
                f"unmatched_sys={unmatched_system} unmatched_ext={unmatched_external} 状态->{batch.status}",
                target_id=batch_id,
            )
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
                self.db.query(ReconciliationEntry).filter_by(batch_id=batch_id, source="erp", matched=False).count()
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
                    f"存在未匹配明细（系统侧 {unmatched_system} 条，外部侧 {unmatched_external} 条），" f"无法关闭批次"
                )

            # P0-4: 关账口径与 auto_match 对齐 —— 按匹配对逐条校验，精确（<0.01）或
            # 税容差内（max(|系统金额|*tax_rate, 0.01)）的匹配允许关账。此前以汇总
            # |discrepancy_amount|>0.01 一刀切：含税容差匹配的批次汇总差异永不为 0，
            # 导致"匹配成功却永远无法关账"的自锁。discrepancy_amount 保留为参考指标。
            bad_pairs = []
            matched_system = (
                self.db.query(ReconciliationEntry)
                .filter_by(batch_id=batch_id, source="erp", matched=True)
                .all()
            )
            for sys_e in matched_system:
                if sys_e.matched_entry_id is None:
                    continue
                ext_e = self.db.get(ReconciliationEntry, sys_e.matched_entry_id)
                if ext_e is None:
                    continue
                sys_amount = self._to_decimal(sys_e.amount)
                diff = abs(sys_amount - self._to_decimal(ext_e.amount))
                tax_rate = getattr(sys_e, "tax_rate", None)
                tax_rate = self._to_decimal(tax_rate) if tax_rate is not None else Decimal("0.13")
                tolerance = max(abs(sys_amount) * tax_rate, Decimal("0.01"))
                if diff > tolerance:
                    bad_pairs.append(f"{sys_e.reference_no or sys_e.id}(差异 {float(diff):.2f})")
            if bad_pairs:
                raise ValueError(
                    f"存在超出税容差的匹配对 {len(bad_pairs)} 条（如 {bad_pairs[0]}），无法关闭"
                )

            batch.status = "closed"
            batch.closed_at = datetime.now()
            # P2-22: 审计日志
            self._audit(
                "close",
                f"关闭对账批次 {batch.batch_no} by={closed_by}",
                target_id=batch_id,
                actor=closed_by,
            )
            self.db.commit()
            logger.info("对账批次 %s 已关闭 by=%s", batch.batch_no, closed_by)
            return True
        except Exception as e:
            self.db.rollback()
            logger.exception("关闭对账批次失败: %s", e)
            raise
