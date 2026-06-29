"""系统层模型：闭环状态 + 审计日志 + RBAC"""
from datetime import datetime
from extensions import db


class LoopState(db.Model):
    """闭环运行状态记录"""
    __tablename__ = 'loop_state'
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, nullable=False, index=True)
    step = db.Column(db.Integer, nullable=False, index=True)
    step_name = db.Column(db.String(32))
    status = db.Column(db.String(16), default='pending', index=True)  # pending/running/done/failed/rolled_back
    detail = db.Column(db.Text)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)


class AuditLog(db.Model):
    """审计日志（所有关键操作留痕）"""
    __tablename__ = 'audit_log'
    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(64), default='system', index=True)  # 操作人
    action = db.Column(db.String(32), nullable=False, index=True)  # create/review/order/receive/etl/...
    target_type = db.Column(db.String(32))  # 操作对象类型
    target_id = db.Column(db.String(64))    # 操作对象ID
    detail = db.Column(db.Text)
    ip = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)


# ==================== RBAC ====================

user_roles = db.Table(
    'user_roles',
    db.Column('user_id', db.Integer, db.ForeignKey('sys_user.id'), primary_key=True),
    db.Column('role_id', db.Integer, db.ForeignKey('sys_role.id'), primary_key=True)
)


class User(db.Model):
    """用户"""
    __tablename__ = 'sys_user'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)  # werkzeug.security
    display_name = db.Column(db.String(64))
    is_active = db.Column(db.Boolean, default=True)
    must_change_password = db.Column(db.Boolean, default=False)  # 首次登录随机密码后强制改密
    roles = db.relationship('Role', secondary=user_roles, backref='users')
    created_at = db.Column(db.DateTime, default=datetime.now)


class TokenBlacklist(db.Model):
    """Token 黑名单（登出/改密后令牌失效）"""
    __tablename__ = 'sys_token_blacklist'
    id = db.Column(db.Integer, primary_key=True)
    jti = db.Column(db.String(64), unique=True, nullable=False, index=True)
    token_type = db.Column(db.String(16), nullable=False)  # access / refresh
    user_id = db.Column(db.Integer, index=True)
    revoked_at = db.Column(db.DateTime, default=datetime.now, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)  # 原始过期时间，便于清理


class Role(db.Model):
    """角色 + 权限"""
    __tablename__ = 'sys_role'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(32), unique=True, nullable=False, index=True)  # admin/operator/viewer
    permissions = db.Column(db.Text)  # JSON 数组：["erp:read","loop:run","aigc:review",...]
    description = db.Column(db.String(128))
