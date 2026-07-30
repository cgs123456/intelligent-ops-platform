"""系统层模型：闭环状态 + 审计日志 + RBAC"""

import hashlib
import json
from datetime import datetime

from sqlalchemy import Index
from sqlalchemy.event import listens_for

from extensions import db


class LoopState(db.Model):
    """闭环运行状态记录"""

    __tablename__ = "loop_state"
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, nullable=False, index=True)
    step = db.Column(db.Integer, nullable=False, index=True)
    step_name = db.Column(db.String(32))
    status = db.Column(db.String(16), default="pending", index=True)  # pending/running/done/failed/rolled_back
    detail = db.Column(db.Text)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)


class AuditLog(db.Model):
    """审计日志（所有关键操作留痕）

    P0-1: hash chain 防篡改 —— 每条记录包含 prev_hash（上一条记录的 hash）和
    hash（本条记录的 hash = sha256(prev_hash + canonical_json(字段))）。
    任何字段被直接修改 DB 后，校验时 hash 不匹配即可检测篡改。
    """

    __tablename__ = "audit_log"
    # P1-13: (actor, action, created_at) 复合索引优化审计日志高频查询
    __table_args__ = (Index("idx_audit_actor_action_created", "actor", "action", "created_at"),)
    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(64), default="system", index=True)  # 操作人
    action = db.Column(db.String(32), nullable=False, index=True)  # create/review/order/receive/etl/...
    target_type = db.Column(db.String(32))  # 操作对象类型
    target_id = db.Column(db.String(64))  # 操作对象ID
    detail = db.Column(db.Text)
    ip = db.Column(db.String(64))
    # P1-17: 关联 request_id，串联请求日志与审计记录
    request_id = db.Column(db.String(64), index=True)
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)
    # P0-1: hash chain 字段（防篡改）
    prev_hash = db.Column(db.String(64), default="")  # 上一条记录的 hash，首条为空
    hash = db.Column(db.String(64), default="", index=True)  # 本条记录的 hash

    @classmethod
    def _compute_hash(cls, prev_hash, actor, action, target_type, target_id, detail, ip, request_id, created_at):
        """计算 hash = sha256(prev_hash + canonical_json(字段))。

        字段顺序固定，JSON 用 sort_keys 确保可重现。
        datetime 转为 ISO 字符串，避免时区差异。
        """
        record = {
            "actor": actor or "system",
            "action": action or "",
            "target_type": target_type or "",
            "target_id": str(target_id) if target_id is not None else "",
            "detail": detail or "",
            "ip": ip or "",
            "request_id": request_id or "",
            "created_at": created_at.isoformat() if isinstance(created_at, datetime) else str(created_at or ""),
        }
        payload = (prev_hash or "") + json.dumps(record, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def verify_chain(cls, start_id=None, end_id=None):
        """校验 hash chain 完整性，返回首个被篡改的记录 id（无篡改返回 None）。

        :param start_id: 起始记录 id（默认从第一条）
        :param end_id: 结束记录 id（默认到最后一条）
        :return: 被篡改的记录 id，或 None 表示完整
        """
        query = cls.query.order_by(cls.id)
        if start_id:
            query = query.filter(cls.id >= start_id)
        if end_id:
            query = query.filter(cls.id <= end_id)
        records = query.all()
        prev_hash = ""
        for idx, rec in enumerate(records):
            expected_prev = "" if idx == 0 else prev_hash
            expected_hash = cls._compute_hash(
                expected_prev, rec.actor, rec.action, rec.target_type,
                rec.target_id, rec.detail, rec.ip, rec.request_id, rec.created_at
            )
            if rec.hash != expected_hash or (idx > 0 and rec.prev_hash != expected_prev):
                return rec.id
            prev_hash = rec.hash or ""
        return None


@listens_for(AuditLog, "before_insert")
def _audit_log_set_hash(mapper, connection, target):
    """before_insert 钩子：自动计算 prev_hash 和 hash。

    取上一条记录的 hash 作为 prev_hash，保证链式完整。
    """
    last = AuditLog.query.order_by(AuditLog.id.desc()).first()
    prev = last.hash if last and last.hash else ""
    target.prev_hash = prev
    target.hash = AuditLog._compute_hash(
        prev, target.actor, target.action, target.target_type,
        target.target_id, target.detail, target.ip, target.request_id,
        target.created_at or datetime.now()
    )


# ==================== RBAC ====================

user_roles = db.Table(
    "user_roles",
    db.Column("user_id", db.Integer, db.ForeignKey("sys_user.id"), primary_key=True),
    db.Column("role_id", db.Integer, db.ForeignKey("sys_role.id"), primary_key=True),
)


class User(db.Model):
    """用户"""

    __tablename__ = "sys_user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)  # werkzeug.security
    display_name = db.Column(db.String(64))
    is_active = db.Column(db.Boolean, default=True)
    must_change_password = db.Column(db.Boolean, default=False)  # 首次登录随机密码后强制改密
    # P0-1: token_version 用于改密后使所有旧 token 失效（签发时写入，校验时比对，改密时递增）
    token_version = db.Column(db.Integer, default=0, nullable=False)
    # P0-8: MFA TOTP 双因素认证
    mfa_secret = db.Column(db.String(64), nullable=True)  # TOTP secret（加密存储）
    mfa_enabled = db.Column(db.Boolean, default=False, nullable=False)  # 是否已启用 MFA
    mfa_backup_codes = db.Column(db.Text, nullable=True)  # 备用恢复码（JSON 数组，哈希存储）
    # P0-8: 密码历史（JSON 数组，最近 N 条密码哈希，防止重用）
    password_history = db.Column(db.Text, nullable=True)
    roles = db.relationship("Role", secondary=user_roles, backref="users")
    created_at = db.Column(db.DateTime, default=datetime.now)


class TokenBlacklist(db.Model):
    """Token 黑名单（登出/改密后令牌失效）"""

    __tablename__ = "sys_token_blacklist"
    # P1-13: (user_id, expires_at) 复合索引优化清理过期 token 任务
    __table_args__ = (Index("idx_token_user_expires", "user_id", "expires_at"),)
    id = db.Column(db.Integer, primary_key=True)
    jti = db.Column(db.String(64), unique=True, nullable=False, index=True)
    token_type = db.Column(db.String(16), nullable=False)  # access / refresh
    # P1-12: 外键约束，删除用户时级联清理黑名单
    user_id = db.Column(db.Integer, db.ForeignKey("sys_user.id", ondelete="CASCADE"), index=True)
    revoked_at = db.Column(db.DateTime, default=datetime.now, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)  # 原始过期时间，便于清理

    @classmethod
    def cleanup_expired(cls):
        """P1-10: 清理已过期的黑名单记录（expires_at < 当前时间）。

        由定时任务每天 03:00 调用，避免黑名单表无限增长。
        :return: 已删除的记录数
        """
        from extensions import db

        expired_records = cls.query.filter(cls.expires_at < datetime.now()).all()
        count = len(expired_records)
        for record in expired_records:
            db.session.delete(record)
        db.session.commit()
        return count


class Role(db.Model):
    """角色 + 权限"""

    __tablename__ = "sys_role"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(32), unique=True, nullable=False, index=True)  # admin/operator/viewer
    permissions = db.Column(db.Text)  # JSON 数组：["erp:read","loop:run","aigc:review",...]
    description = db.Column(db.String(128))


# P1-17: AuditLog 写入时自动从请求上下文填充 request_id（零侵入，业务代码无需改动）
@db.event.listens_for(AuditLog, "before_insert")
def _auditlog_fill_request_id(mapper, connection, target):
    """自动填充 request_id：有请求上下文时从 g.request_id 取，否则保持 None（后台任务）"""
    if target.request_id is None:
        try:
            from flask import g, has_request_context

            if has_request_context():
                target.request_id = getattr(g, "request_id", None)
        except Exception:
            pass
