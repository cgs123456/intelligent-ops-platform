"""API Schema 定义（基于 marshmallow，供 flask-smorest 自动生成 OpenAPI 文档）

使用方式：
    from schemas import LoginSchema, ChangePasswordSchema
    from flask_smorest import Blueprint

    @bp.route('/login')
    @bp.arguments(LoginSchema)
    @bp.response(200, LoginResponseSchema)
    def login(args):
        ...

完整接入需要把 Blueprint 改为 flask_smorest.Blueprint，工作量大。
此处先定义 Schema 类，供后续渐进迁移；同时也可用于手动验证请求体。

注意：marshmallow 3.x 的 Field 不再接受 description= 关键字参数，
      描述信息需通过 metadata={"description": "..."} 传递。
"""

from marshmallow import Schema, fields, validate


class LoginSchema(Schema):
    """登录请求"""

    username = fields.String(required=True, validate=validate.Length(min=1, max=64), metadata={"description": "用户名"})
    password = fields.String(
        required=True, validate=validate.Length(min=1, max=128), metadata={"description": "密码"}, load_only=True
    )


class LoginResponseSchema(Schema):
    """登录响应"""

    access_token = fields.String(metadata={"description": "JWT access token（30min 过期）"})
    refresh_token = fields.String(metadata={"description": "JWT refresh token（7d 过期）"})
    token_type = fields.String(metadata={"description": "Bearer"})
    expires_in = fields.Integer(metadata={"description": "access token 有效期（秒）"})
    must_change_password = fields.Boolean(metadata={"description": "是否需要立即修改密码"})
    user = fields.Dict(metadata={"description": "用户基本信息"})


class RefreshSchema(Schema):
    """刷新 token 请求"""

    refresh_token = fields.String(required=True, metadata={"description": "refresh token"})


class LogoutSchema(Schema):
    """登出请求"""

    refresh_token = fields.String(required=False, metadata={"description": "要吊销的 refresh token"})


class ChangePasswordSchema(Schema):
    """修改密码请求"""

    old_password = fields.String(required=True, validate=validate.Length(min=1, max=128), load_only=True)
    new_password = fields.String(
        required=True,
        validate=validate.Length(min=8, max=128),
        metadata={"description": "新密码（至少8位，含大小写数字特殊字符）"},
        load_only=True,
    )


class CreateReturnSchema(Schema):
    """创建退货单请求"""

    original_order_no = fields.String(
        required=True, validate=validate.Length(min=1, max=64), metadata={"description": "原销售订单号"}
    )
    product_id = fields.Integer(required=True, metadata={"description": "产品 ID"})
    qty = fields.Integer(
        required=True, validate=validate.Range(min=1), metadata={"description": "退货数量（必须为正数）"}
    )
    reason = fields.String(required=False, validate=validate.Length(max=500), metadata={"description": "退货原因"})


class CreateTransferSchema(Schema):
    """创建调拨单请求"""

    product_id = fields.Integer(required=True, metadata={"description": "产品 ID"})
    qty = fields.Integer(required=True, validate=validate.Range(min=1), metadata={"description": "调拨数量"})
    from_warehouse_id = fields.Integer(required=True, metadata={"description": "源仓库 ID"})
    to_warehouse_id = fields.Integer(required=True, metadata={"description": "目标仓库 ID"})


class ReviewSuggestionSchema(Schema):
    """审核补货建议"""

    action = fields.String(
        required=True, validate=validate.OneOf(["approve", "reject"]), metadata={"description": "审核动作"}
    )
    final_qty = fields.Integer(required=False, validate=validate.Range(min=0), metadata={"description": "最终采购数量"})
    note = fields.String(required=False, validate=validate.Length(max=500), metadata={"description": "审核备注"})


class AuditLogQuerySchema(Schema):
    """审计日志查询参数"""

    page = fields.Integer(load_default=1, validate=validate.Range(min=1))
    per_page = fields.Integer(load_default=50, validate=validate.Range(min=1, max=200))
    actor = fields.String(load_default="")
    action = fields.String(load_default="")
    target_type = fields.String(load_default="")
    start = fields.String(load_default="", metadata={"description": "起始日期 YYYY-MM-DD"})
    end = fields.String(load_default="", metadata={"description": "结束日期 YYYY-MM-DD"})


class RunLoopStepSchema(Schema):
    """执行闭环步骤"""

    step = fields.Integer(required=True, validate=validate.Range(min=1, max=5), metadata={"description": "步骤号 1-5"})
    actor = fields.String(load_default="system")


class IdempotencyHeaderSchema(Schema):
    """Idempotency-Key Header（仅用于文档展示）"""

    Idempotency_Key = fields.String(load_default="", metadata={"description": "幂等键，相同 key 重放返回首次结果"})


# 通用响应
class ErrorResponseSchema(Schema):
    """错误响应"""

    error = fields.String(metadata={"description": "错误描述"})
