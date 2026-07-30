"""统一响应 envelope 测试 — success/error/paginate + 错误码映射

覆盖：
- success_response 默认参数 + 自定义 message/code
- error_response 默认 http_status 从映射取 + 自定义 http_status
- error_response 未知 code 回退 400
- paginate_response 分页 + 越界保护
- request_id 透传到响应 envelope
- ErrorCode 常量 + HTTP_STATUS 映射完整性
"""

import pytest

from extensions import db
from models.erp import Product
from services.response import (
    ErrorCode,
    HTTP_STATUS,
    error_response,
    paginate_response,
    success_response,
)


@pytest.mark.usefixtures("db_session")
class TestSuccessResponse:
    """success_response 成功响应测试"""

    def test_success_default(self, app):
        """默认参数：code=OK, message=success, data=None, http=200"""
        with app.test_request_context("/"):
            resp, status = success_response()
            assert status == 200
            data = resp.get_json()
            assert data["code"] == ErrorCode.OK
            assert data["message"] == "success"
            assert data["data"] is None
            assert "timestamp" in data
            assert "request_id" in data

    def test_success_with_data(self, app):
        """携带 data 时原样返回"""
        with app.test_request_context("/"):
            resp, status = success_response(data={"id": 1, "name": "测试"})
            assert status == 200
            data = resp.get_json()
            assert data["data"]["id"] == 1
            assert data["data"]["name"] == "测试"

    def test_success_custom_message(self, app):
        """自定义 message"""
        with app.test_request_context("/"):
            resp, _ = success_response(data={"ok": True}, message="创建成功")
            data = resp.get_json()
            assert data["message"] == "创建成功"

    def test_success_envelope_fields(self, app):
        """envelope 包含 5 个必需字段：code/message/data/request_id/timestamp"""
        with app.test_request_context("/"):
            resp, _ = success_response(data={"x": 1})
            data = resp.get_json()
            assert set(data.keys()) >= {"code", "message", "data", "request_id", "timestamp"}


@pytest.mark.usefixtures("db_session")
class TestErrorResponse:
    """error_response 错误响应测试"""

    def test_error_default_http_status_from_mapping(self, app):
        """未传 http_status 时，从 HTTP_STATUS 映射取（BAD_REQUEST → 400）"""
        with app.test_request_context("/"):
            resp, status = error_response(ErrorCode.BAD_REQUEST, "参数错误")
            assert status == 400
            data = resp.get_json()
            assert data["code"] == ErrorCode.BAD_REQUEST
            assert data["message"] == "参数错误"
            assert data["data"] is None

    def test_error_custom_http_status(self, app):
        """自定义 http_status 覆盖映射"""
        with app.test_request_context("/"):
            resp, status = error_response(ErrorCode.NOT_FOUND, "不存在", http_status=422)
            assert status == 422
            assert resp.get_json()["code"] == ErrorCode.NOT_FOUND

    def test_error_unknown_code_defaults_400(self, app):
        """未映射的 code 默认 http_status=400"""
        with app.test_request_context("/"):
            resp, status = error_response("ERR_UNKNOWN_999", "未知错误")
            assert status == 400
            assert resp.get_json()["code"] == "ERR_UNKNOWN_999"

    def test_error_with_data(self, app):
        """错误响应携带附加 data（如字段校验详情）"""
        with app.test_request_context("/"):
            resp, _ = error_response(
                ErrorCode.VALIDATION_ERROR,
                "校验失败",
                data={"fields": ["name", "qty"]},
            )
            data = resp.get_json()
            assert data["data"]["fields"] == ["name", "qty"]

    def test_error_auth_codes_map_to_401(self, app):
        """认证类错误码映射到 401"""
        with app.test_request_context("/"):
            for code in (ErrorCode.AUTH_INVALID_CREDENTIALS, ErrorCode.AUTH_TOKEN_EXPIRED, ErrorCode.AUTH_TOKEN_INVALID):
                _, status = error_response(code, "认证失败")
                assert status == 401

    def test_error_rate_limited_maps_429(self, app):
        """RATE_LIMITED → 429"""
        with app.test_request_context("/"):
            _, status = error_response(ErrorCode.RATE_LIMITED, "请求过快")
            assert status == 429


@pytest.mark.usefixtures("db_session")
class TestPaginateResponse:
    """paginate_response 分页响应测试"""

    @pytest.fixture(autouse=True)
    def _cleanup_products(self, app):
        with app.app_context():
            Product.query.delete()
            db.session.commit()
        yield
        with app.app_context():
            Product.query.delete()
            db.session.commit()

    def _seed_products(self, app, n=10):
        with app.app_context():
            for i in range(n):
                db.session.add(
                    Product(
                        sku=f"PAGE-{i:03d}",
                        name=f"分页商品{i}",
                        cost_price=10,
                        sale_price=20,
                        stock_qty=100,
                        safety_stock=10,
                    )
                )
            db.session.commit()

    def test_paginate_basic(self, app):
        """基础分页：page=1, per_page=5"""
        self._seed_products(app, 10)
        with app.app_context():
            query = Product.query.order_by(Product.id.asc())
            resp, status = paginate_response(query, page=1, per_page=5, serializer=lambda p: {"id": p.id, "sku": p.sku})
            assert status == 200
            data = resp.get_json()["data"]
            assert len(data["items"]) == 5
            assert data["total"] == 10
            assert data["page"] == 1
            assert data["per_page"] == 5
            assert data["pages"] == 2

    def test_paginate_last_page(self, app):
        """最后一页：page=2, per_page=5 → 5 条"""
        self._seed_products(app, 10)
        with app.app_context():
            query = Product.query
            resp, _ = paginate_response(query, page=2, per_page=5, serializer=lambda p: {"id": p.id})
            data = resp.get_json()["data"]
            assert len(data["items"]) == 5

    def test_paginate_page_below_1_clamped(self, app):
        """page < 1 时钳制为 1"""
        self._seed_products(app, 3)
        with app.app_context():
            query = Product.query
            resp, _ = paginate_response(query, page=0, per_page=5, serializer=lambda p: {"id": p.id})
            data = resp.get_json()["data"]
            assert data["page"] == 1

    def test_paginate_per_page_above_200_clamped(self, app):
        """per_page > 200 时钳制为 200"""
        self._seed_products(app, 3)
        with app.app_context():
            query = Product.query
            resp, _ = paginate_response(query, page=1, per_page=500, serializer=lambda p: {"id": p.id})
            data = resp.get_json()["data"]
            assert data["per_page"] == 200

    def test_paginate_empty_result(self, app):
        """空结果集：items=[], total=0"""
        with app.app_context():
            query = Product.query
            resp, _ = paginate_response(query, page=1, per_page=10, serializer=lambda p: {"id": p.id})
            data = resp.get_json()["data"]
            assert data["items"] == []
            assert data["total"] == 0
            assert data["pages"] == 0


class TestRequestIdInEnvelope:
    """request_id 透传到响应 envelope"""

    def test_request_id_from_g(self, app):
        """g.request_id 透传到 envelope.request_id"""
        with app.app_context():
            from flask import g

            g.request_id = "envelope-rid-001"
            resp, _ = success_response(data={"ok": True})
            assert resp.get_json()["request_id"] == "envelope-rid-001"

    def test_request_id_none_when_no_g(self, app):
        """无 g.request_id 时 envelope.request_id 为 None"""
        # 推入新的 app context，g 为干净状态（无 request_id 属性）
        with app.app_context():
            from flask import g

            # 确认 g 上没有 request_id
            assert not hasattr(g, "request_id")
            resp, _ = success_response(data={"ok": True})
            data = resp.get_json()
            # g.request_id 不存在 → getattr 返回 None
            assert data["request_id"] is None


class TestErrorCodeConstants:
    """ErrorCode 常量 + HTTP_STATUS 映射完整性"""

    def test_error_code_format(self):
        """所有错误码遵循 ERR_{模块}_{3位编号} 格式（OK 除外）"""
        codes = [v for k, v in vars(ErrorCode).items() if not k.startswith("_") and isinstance(v, str)]
        for code in codes:
            if code == ErrorCode.OK:
                continue
            # ERR_模块_3位数字
            parts = code.split("_")
            assert len(parts) == 3, f"错误码 {code} 格式不符"
            assert parts[0] == "ERR"
            assert len(parts[2]) == 3 and parts[2].isdigit(), f"错误码 {code} 编号应为 3 位数字"

    def test_http_status_covers_common_codes(self):
        """HTTP_STATUS 映射覆盖常用错误码"""
        assert HTTP_STATUS[ErrorCode.OK] == 200
        assert HTTP_STATUS[ErrorCode.BAD_REQUEST] == 400
        assert HTTP_STATUS[ErrorCode.UNAUTHORIZED] == 401
        assert HTTP_STATUS[ErrorCode.NOT_FOUND] == 404
        assert HTTP_STATUS[ErrorCode.FORBIDDEN] == 403
        assert HTTP_STATUS[ErrorCode.RATE_LIMITED] == 429
        assert HTTP_STATUS[ErrorCode.INTERNAL_ERROR] == 500

    def test_module_code_groups(self):
        """按模块分组：SYS/AUTH/ERP/LOOP/RPA/AIGC/IDEM"""
        assert ErrorCode.INTERNAL_ERROR.startswith("ERR_SYS_")
        assert ErrorCode.AUTH_INVALID_CREDENTIALS.startswith("ERR_AUTH_")
        assert ErrorCode.ERP_PRODUCT_NOT_FOUND.startswith("ERR_ERP_")
        assert ErrorCode.LOOP_STEP_INVALID.startswith("ERR_LOOP_")
        assert ErrorCode.RPA_BACKEND_ERROR.startswith("ERR_RPA_")
        assert ErrorCode.AIGC_LLM_ERROR.startswith("ERR_AIGC_")
        assert ErrorCode.IDEM_KEY_MISSING.startswith("ERR_IDEM_")
