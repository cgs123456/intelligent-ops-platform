"""AIGC 层模型"""
from datetime import datetime, date
from extensions import db


class Suggestion(db.Model):
    """AIGC 生成的补货建议（需人工审核）"""
    __tablename__ = 'aigc_suggestion'
    __table_args__ = (
        db.Index('idx_suggestion_status_created', 'status', 'created_at'),
    )
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, nullable=False, index=True)
    product_name = db.Column(db.String(64))
    suggested_supplier_id = db.Column(db.Integer)
    suggested_supplier_name = db.Column(db.String(64))
    suggested_qty = db.Column(db.Integer, nullable=False)
    original_qty = db.Column(db.Integer)  # 原始建议量（审核修改前）
    unit_price = db.Column(db.Numeric(10, 2))
    reason = db.Column(db.Text)
    confidence = db.Column(db.Numeric(4, 2), default=0.80)  # 置信度 0-1
    status = db.Column(db.String(16), default='pending', index=True)  # pending/approved/rejected/ordered
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    reviewed_at = db.Column(db.DateTime)
    review_note = db.Column(db.String(256))
    reviewed_by = db.Column(db.String(64))  # 审核人


class DailyReport(db.Model):
    """AIGC 生成的经营日报"""
    __tablename__ = 'aigc_daily_report'
    id = db.Column(db.Integer, primary_key=True)
    dt = db.Column(db.Date, unique=True, index=True)
    report_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)


class ChatHistory(db.Model):
    """多轮对话历史"""
    __tablename__ = 'aigc_chat_history'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(64), nullable=False, index=True)  # 会话ID
    role = db.Column(db.String(16), nullable=False)  # user/assistant
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)


class SuggestionFeedback(db.Model):
    """审核反馈（用于 AIGC 自学习）"""
    __tablename__ = 'aigc_suggestion_feedback'
    id = db.Column(db.Integer, primary_key=True)
    suggestion_id = db.Column(db.Integer, nullable=False, index=True)
    original_qty = db.Column(db.Integer)
    final_qty = db.Column(db.Integer)  # 审核后的实际量（approved=原量或修改量, rejected=0）
    action = db.Column(db.String(16))  # approved/rejected
    qty_delta = db.Column(db.Integer)  # 修改差值（正=加量，负=减量）
    note = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.now)
