"""统一响应 envelope + 错误码体系（P0-3）

所有 API 接口应使用 make_response/success_response/error_response 包装返回值，
确保响应格式统一：{code, message, data, request_id, timestamp}

错误码规范：ERR_{模块}_{3位编号}，如 ERR_AUTH_001、ERR_ERP_002
"""

from datetime import datetime
from flask import g, jsonify


# ==================== 错误码定义 ====================

class ErrorCode:
    """错误码常量（按模块分组）"""

    # 通用 0xx
    OK = "OK"
    INTERNAL_ERROR = "ERR_SYS_001"
    BAD_REQUEST = "ERR_SYS_002"
    NOT_FOUND = "ERR_SYS_003"
    FORBIDDEN = "ERR_SYS_004"
    UNAUTHORIZED = "ERR_SYS_005"
    RATE_LIMITED = "ERR_SYS_006"
    VALIDATION_ERROR = "ERR_SYS_007"

    # 认证 AUTH 1xx
    AUTH_INVALID_CREDENTIALS = "ERR_AUTH_001"
    AUTH_TOKEN_EXPIRED = "ERR_AUTH_002"
    AUTH_TOKEN_INVALID = "ERR_AUTH_003"
    AUTH_ACCOUNT_LOCKED = "ERR_AUTH_004"
    AUTH_PASSWORD_WEAK = "ERR_AUTH_005"
    AUTH_PASSWORD_CHANGED = "ERR_AUTH_006"
    AUTH_MFA_REQUIRED = "ERR_AUTH_007"
    AUTH_MFA_INVALID = "ERR_AUTH_008"

    # ERP 2xx
    ERP_PRODUCT_NOT_FOUND = "ERR_ERP_001"
    ERP_INSUFFICIENT_STOCK = "ERR_ERP_002"
    ERP_ORDER_NOT_FOUND = "ERR_ERP_003"
    ERP_INVALID_STATUS = "ERR_ERP_004"

    # 闭环 LOOP 3xx
    LOOP_STEP_INVALID = "ERR_LOOP_001"
    LOOP_RUN_NOT_FOUND = "ERR_LOOP_002"
    LOOP_LOCK_FAILED = "ERR_LOOP_003"

    # RPA 4xx
    RPA_BACKEND_ERROR = "ERR_RPA_001"
    RPA_TIMEOUT = "ERR_RPA_002"

    # AIGC 5xx
    AIGC_LLM_ERROR = "ERR_AIGC_001"
    AIGC_SQL_INVALID = "ERR_AIGC_002"

    # 幂等 IDEM 6xx
    IDEM_KEY_MISSING = "ERR_IDEM_001"
    IDEM_CONFLICT = "ERR_IDEM_002"


# HTTP 状态码映射
HTTP_STATUS = {
    ErrorCode.OK: 200,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    ErrorCode.AUTH_TOKEN_EXPIRED: 401,
    ErrorCode.AUTH_TOKEN_INVALID: 401,
    ErrorCode.AUTH_ACCOUNT_LOCKED: 423,
    ErrorCode.AUTH_PASSWORD_WEAK: 400,
    ErrorCode.AUTH_MFA_REQUIRED: 401,
    ErrorCode.AUTH_MFA_INVALID: 401,
}


def _build(code, message, data, http_status):
    """构造统一响应 envelope"""
    request_id = getattr(g, "request_id", None) if g else None
    return jsonify(
        {
            "code": code,
            "message": message,
            "data": data,
            "request_id": request_id,
            "timestamp": datetime.now().isoformat(),
        }
    ), http_status


def success_response(data=None, message="success", code=ErrorCode.OK):
    """成功响应 envelope"""
    return _build(code, message, data, 200)


def error_response(code, message, http_status=None, data=None):
    """错误响应 envelope

    :param code: ErrorCode 常量
    :param message: 错误消息
    :param http_status: HTTP 状态码，默认从 HTTP_STATUS 映射
    :param data: 可选附加数据（如字段校验错误详情）
    """
    if http_status is None:
        http_status = HTTP_STATUS.get(code, 400)
    return _build(code, message, data, http_status)


def paginate_response(query, page, per_page, serializer):
    """分页响应 envelope

    :param query: SQLAlchemy query 对象
    :param page: 页码（从 1 开始）
    :param per_page: 每页条数
    :param serializer: 序列化函数，接收单条记录返回 dict
    :return: success_response 包装的分页结果
    """
    page = max(1, page)
    per_page = max(1, min(per_page, 200))
    total = query.count()
    pages = (total + per_page - 1) // per_page
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    return success_response(
        data={
            "items": [serializer(item) for item in items],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }
    )
