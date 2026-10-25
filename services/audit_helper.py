"""审计日志公共写入助手。

三个服务（ERP / 财务对账 / RPA）各自维护过一份 AuditLog 构造逻辑，
此处收口为单一实现；调用方保留原 _audit 签名做薄适配。

事务语义：
- 默认只 add，随业务事务一起提交（推荐——审计与业务同生共死）；
- commit=True 用于"业务已提交后"的补充审计（如 RPA 采集结果），
  此时写入失败仅记日志，不向调用方抛错，避免审计拖垮主流程。
"""

import logging

logger = logging.getLogger(__name__)


def record_audit(session, actor, action, target_type, target_id, detail="", ip=None, commit=False):
    """构造并写入一条审计日志。

    :param session: SQLAlchemy session（业务 session）
    :param actor: 操作人，空值落 "system"
    :param commit: True 则立即提交且失败不抛错（补充审计场景）
    :return: 已 add 的 AuditLog 对象（commit 失败时返回 None）
    """
    from models.system import AuditLog

    log = AuditLog(
        actor=actor or "system",
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        detail=detail or "",
        ip=ip,
    )
    session.add(log)
    if commit:
        try:
            session.commit()
        except Exception as e:
            logger.error("[audit] 审计日志提交失败: %s", e)
            session.rollback()
            return None
    return log
