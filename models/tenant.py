"""租户模型与租户隔离基础设施

多租户架构决策（v1）：
- 隔离模型：共库共表 + tenant_id 列 + ORM 层强制过滤（with_loader_criteria）。
  应用层过滤覆盖全部 ORM 查询（含 relationship 懒加载），PG RLS 可作为后续
  纵深防御叠加，当前以应用层为准。
- 默认租户：id=1（DEFAULT_TENANT_ID）。未指定租户的写入一律落默认租户，
  保证存量单租户行为与测试零变化。
- 平台管理员：User.tenant_id IS NULL 的用户跨租户（不自动过滤）。
- 审计链：AuditLog.tenant_id 仅作归属记录，hash chain 保持全局单链
  （防篡改是平台级完整性，跨租户混链不影响校验语义）。
"""

from datetime import datetime

from extensions import db

DEFAULT_TENANT_ID = 1


class Tenant(db.Model):
    """租户（平台全局表，不参与租户过滤）"""

    __tablename__ = "sys_tenant"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)


class TenantScoped:
    """租户隔离标记 mixin。

    继承本 mixin 的模型自动获得 tenant_id 列，并被 extensions 中注册的
    do_orm_execute 事件按当前租户自动过滤；写入侧由 before_flush 统一
    填充（见 extensions.py），无 app context 时落 DEFAULT_TENANT_ID。

    唯一约束必须复合化：本 mixin 列 + 原业务键（如 (tenant_id, sku)），
    各租户单号/编码序列独立。
    """

    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("sys_tenant.id"),
        nullable=False,
        index=True,
    )
