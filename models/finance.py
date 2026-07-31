"""财务对账模型

用于 ERP 账务（应付/应收/退款凭证）与银行流水、外部系统（如第三方支付、电商对账单）
之间的对账，支持按期间生成对账批次 → 导入外部流水 → 自动匹配 → 计算差异 → 关闭批次
的完整闭环。

核心概念：
    - ReconciliationBatch：对账批次，按 period（YYYY-MM）维度归集
    - ReconciliationEntry：对账明细，区分 source_system（ERP 侧）与 source_external（外部侧）
      通过 reference_no + amount 精确匹配，matched_entry_id 自引用记录配对关系

金额字段统一使用 Numeric(18,2)，避免浮点误差。
"""

from datetime import datetime
from decimal import Decimal

from extensions import db


class ReconciliationBatch(db.Model):
    """对账批次（按期间归集一次对账流程）

    status 流转：draft -> pending -> matched/discrepancy -> closed
        - draft：新建，未导入明细
        - pending：已导入明细，待匹配
        - matched：自动匹配完成且无差异
        - discrepancy：存在差异（金额不一致 / 未匹配明细）
        - closed：关闭批次（不可再修改）
    """

    __tablename__ = "finance_reconciliation_batch"
    id = db.Column(db.Integer, primary_key=True)
    batch_no = db.Column(db.String(40), unique=True, nullable=False, index=True)  # 对账批次号
    period = db.Column(db.String(7), nullable=False, index=True)  # 对账期间 YYYY-MM
    status = db.Column(
        db.String(16), nullable=False, default="draft", index=True
    )  # draft/pending/matched/discrepancy/closed
    total_system_amount = db.Column(db.Numeric(18, 2), nullable=False, default=0)  # 系统侧合计金额
    total_external_amount = db.Column(db.Numeric(18, 2), nullable=False, default=0)  # 外部侧合计金额
    discrepancy_amount = db.Column(db.Numeric(18, 2), nullable=False, default=0)  # 差异金额 = 系统侧 - 外部侧
    created_by = db.Column(db.String(64), nullable=False, default="system")  # 创建人
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    closed_at = db.Column(db.DateTime)  # 关闭时间
    notes = db.Column(db.Text)  # 备注


class ReconciliationEntry(db.Model):
    """对账明细（系统侧 + 外部侧统一建模）

    source 取值：
        - erp：来自 ERP 系统的凭证（应付/应收/退款）
        - bank：银行流水
        - manual：人工录入（差错调整等）

    direction：debit(借方) / credit(贷方)
    matched：是否已匹配成功
    matched_entry_id：自引用 FK，指向配对的对侧明细
    """

    __tablename__ = "finance_reconciliation_entry"
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("finance_reconciliation_batch.id"), nullable=False, index=True)
    source = db.Column(db.String(16), nullable=False, index=True)  # erp/bank/manual
    account_code = db.Column(db.String(32), nullable=False)  # 账户代码（如银行账号 / 应收账款科目）
    entry_date = db.Column(db.DateTime, nullable=False, index=True)  # 明细日期
    amount = db.Column(db.Numeric(18, 2), nullable=False)  # 金额（正数）
    direction = db.Column(db.String(8), nullable=False)  # debit/credit
    reference_no = db.Column(db.String(64), index=True)  # 业务单号（订单号 / 银行流水号）
    matched = db.Column(db.Boolean, nullable=False, default=False, index=True)
    matched_entry_id = db.Column(db.Integer, db.ForeignKey("finance_reconciliation_entry.id"), nullable=True)
    mismatch_reason = db.Column(db.String(256))  # 未匹配/差异原因
    # P0-ERP5: 容差匹配支持——系统侧明细记录税率（用于计算匹配容差），匹配类型用于审计
    tax_rate = db.Column(db.Numeric(5, 4), nullable=True, default=Decimal("0.13"))  # 税率（如 0.13 表示 13%）
    match_type = db.Column(db.String(16), nullable=True)  # 匹配类型：exact(精确)/tolerance(容差)
    created_at = db.Column(db.DateTime, default=datetime.now)
