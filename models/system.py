"""系统层模型：闭环状态 + 审计日志 + RBAC"""

import hashlib
import json
from datetime import datetime

from sqlalchemy import Index, PrimaryKeyConstraint
from sqlalchemy.event import listens_for
from sqlalchemy.ext.hybrid import hybrid_property

from extensions import db


def _get_field_crypto():
    """延迟导入字段加密器（避免循环依赖）"""
    from services.crypto import field_crypto

    return field_crypto


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
    # P1-8: 显式声明复合主键 (id, created_at)，与分区迁移后的 DB schema 一致
    __table_args__ = (
        PrimaryKeyConstraint("id", "created_at"),
        Index("idx_audit_actor_action_created", "actor", "action", "created_at"),
    )
    # P1-8: id 为整数列，复合主键 (id, created_at) 由 __table_args__ 中的
    # PrimaryKeyConstraint 显式声明。SQLite 下由 before_insert 钩子手动计算 MAX(id)+1
    # 兜底自增；PostgreSQL 下由迁移设置的序列/IDENTITY 自动生成。
    id = db.Column(db.Integer)
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
                expected_prev,
                rec.actor,
                rec.action,
                rec.target_type,
                rec.target_id,
                rec.detail,
                rec.ip,
                rec.request_id,
                rec.created_at,
            )
            if rec.hash != expected_hash or (idx > 0 and rec.prev_hash != expected_prev):
                return rec.id
            prev_hash = rec.hash or ""
        return None


@listens_for(AuditLog, "before_insert")
def _audit_log_set_hash(mapper, connection, target):
    """before_insert 钩子：自动计算 prev_hash 和 hash。

    P1-7: 使用 PostgreSQL advisory lock 串行化 hash chain 计算，避免并发插入时
    多个事务读到相同 prev_hash 导致链断裂。SQLite 下跳过（单写者无并发问题）。
    P1-8: 复合主键 (id, created_at) 下 SQLite 无 INTEGER PRIMARY KEY 自增，
    手动用 MAX(id)+1 兜底；PostgreSQL 由迁移设置的序列/IDENTITY 自动生成。
    """
    from sqlalchemy import text

    # P0-5 fix: fill request_id BEFORE hash computation
    if target.request_id is None:
        try:
            from flask import g, has_request_context

            if has_request_context():
                target.request_id = getattr(g, "request_id", None)
        except Exception:
            pass

    # P1-7: PostgreSQL 事务级 advisory lock 串行化 hash chain 计算
    if connection.dialect.name == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('audit_log_chain'))"))

    # P1-8: SQLite 复合主键无自增，手动计算 MAX(id)+1 作为 id 兜底
    if connection.dialect.name == "sqlite" and target.id is None:
        target.id = connection.execute(text("SELECT COALESCE(MAX(id), 0) + 1 FROM audit_log")).scalar()

    # P2-8: 仅取 hash 列（with_entities）避免全表扫描加载整行；order_by id desc + limit 1
    # 走 id 索引，配合 LIMIT 1 仅读取最新一条的 hash 列，消除原 .first() 的全行物化开销。
    last = AuditLog.query.with_entities(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1).first()
    prev = last.hash if last and last.hash else ""
    target.prev_hash = prev
    target.hash = AuditLog._compute_hash(
        prev,
        target.actor,
        target.action,
        target.target_type,
        target.target_id,
        target.detail,
        target.ip,
        target.request_id,
        target.created_at or datetime.now(),
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
    # P0-fix: mfa_secret 加密存储（通过 _mfa_secret 密文列 + hybrid property 明文访问）
    _mfa_secret = db.Column("mfa_secret", db.String(256), nullable=True)  # TOTP secret（加密密文）
    mfa_enabled = db.Column(db.Boolean, default=False, nullable=False)  # 是否已启用 MFA
    mfa_backup_codes = db.Column(db.Text, nullable=True)  # 备用恢复码（JSON 数组，哈希存储）
    # P0-8: 密码历史（JSON 数组，最近 N 条密码哈希，防止重用）
    password_history = db.Column(db.Text, nullable=True)
    roles = db.relationship("Role", secondary=user_roles, backref="users")
    created_at = db.Column(db.DateTime, default=datetime.now)

    @hybrid_property
    def mfa_secret(self):
        """TOTP secret（明文读取，密文存储）"""
        if not self._mfa_secret:
            return None
        return _get_field_crypto().decrypt(self._mfa_secret)

    @mfa_secret.setter
    def mfa_secret(self, value):
        self._mfa_secret = _get_field_crypto().encrypt(value) if value else None


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


# P0-5 fix: _auditlog_fill_request_id merged into _audit_log_set_hash
