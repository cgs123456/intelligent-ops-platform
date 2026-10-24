"""Phase 2（P1）修复回归测试

第 1 节：采购单幂等键三件套（P2-1）
- suggestion_id 唯一约束：同建议第二次建单被 DB 拒绝
- ext_order_no 唯一约束：外部单号重复被拒；多 NULL（手工单）放行
- step3 崩溃窗口重跑：已有 PO 的建议跳过并补推状态，不重复下单
"""

import pytest
from sqlalchemy.exc import IntegrityError

from extensions import db
from models.aigc import Suggestion
from models.erp import Product, PurchaseOrder, Supplier
from models.system import User
from services.closed_loop import ClosedLoop
from services.erp_service import ERPService


@pytest.fixture(autouse=True)
def _cleanup(app):
    tables = [PurchaseOrder, Suggestion, Product, Supplier]
    with app.app_context():
        for t in tables:
            db.session.query(t).delete()
        db.session.commit()
    yield
    with app.app_context():
        for t in tables:
            db.session.query(t).delete()
        db.session.commit()


@pytest.fixture
def seed(app):
    with app.app_context():
        sup = Supplier(name="幂等测试供应商", is_active=True)
        prod = Product(sku="IDEM-001", name="幂等测试商品", cost_price=10, sale_price=20, stock_qty=5, safety_stock=10)
        db.session.add_all([sup, prod])
        db.session.commit()
        return {"supplier_id": sup.id, "product_id": prod.id}


def _make_po(supplier_id, product_id, suggestion_id=None, ext_order_no=None):
    return PurchaseOrder(
        order_no=ERPService()._gen_order_no("PO"),
        supplier_id=supplier_id,
        product_id=product_id,
        qty=10,
        unit_price=10,
        suggestion_id=suggestion_id,
        ext_order_no=ext_order_no,
    )


class TestPoIdempotencyKeys:
    def test_suggestion_id_unique_enforced(self, app, seed):
        """同 suggestion_id 第二张采购单被唯一约束拒绝"""
        with app.app_context():
            db.session.add(_make_po(seed["supplier_id"], seed["product_id"], suggestion_id=42))
            db.session.commit()

            db.session.add(_make_po(seed["supplier_id"], seed["product_id"], suggestion_id=42))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_ext_order_no_unique_enforced(self, app, seed):
        """同 ext_order_no 被拒；NULL（手工单）可多张"""
        with app.app_context():
            db.session.add(_make_po(seed["supplier_id"], seed["product_id"], ext_order_no="EXT-DUP-1"))
            db.session.commit()

            db.session.add(_make_po(seed["supplier_id"], seed["product_id"], ext_order_no="EXT-DUP-1"))
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

            db.session.add_all(
                [
                    _make_po(seed["supplier_id"], seed["product_id"]),
                    _make_po(seed["supplier_id"], seed["product_id"]),
                ]
            )
            db.session.commit()  # 两张 ext_order_no=NULL 的手工单共存
            assert PurchaseOrder.query.filter(PurchaseOrder.ext_order_no.is_(None)).count() == 2

    def test_step3_rerun_after_crash_skips_existing_po(self, app, seed, monkeypatch):
        """崩溃窗口（PO 已落库、建议仍 approved）重跑 step3：跳过 + 补推状态，不重复下单"""
        with app.app_context():
            sug = Suggestion(
                product_id=seed["product_id"], product_name="幂等测试商品", suggested_qty=20, status="approved"
            )
            db.session.add(sug)
            db.session.commit()
            db.session.add(_make_po(seed["supplier_id"], seed["product_id"], suggestion_id=sug.id))
            db.session.commit()

            called = []
            fake_rpa = type("FakeRpa", (), {"place_supplier_order": staticmethod(lambda s: called.append(1))})()
            monkeypatch.setattr(ClosedLoop, "_services", staticmethod(lambda: (None, lambda: fake_rpa, None, None)))
            monkeypatch.setattr(ClosedLoop, "get_current_run_id", staticmethod(lambda: 1))

            msg = ClosedLoop._step3_rpa_order()

            assert called == []  # 未再调用下单
            assert "跳过" in msg
            db.session.refresh(sug)
            assert sug.status == "ordered"
            assert PurchaseOrder.query.filter_by(suggestion_id=sug.id).count() == 1

    def test_step3_marks_ordered_immediately_per_order(self, app, seed, monkeypatch):
        """每单成功后立即推进状态（不等整批循环结束）"""
        with app.app_context():
            sug = Suggestion(
                product_id=seed["product_id"], product_name="幂等测试商品", suggested_qty=20, status="approved"
            )
            db.session.add(sug)
            db.session.commit()

            class FakeRpa:
                def place_supplier_order(self, s):
                    po = _make_po(seed["supplier_id"], seed["product_id"], suggestion_id=s["id"], ext_order_no="EXT-OK-1")
                    db.session.add(po)
                    db.session.commit()
                    return {"status": "ok", "supplier": "幂等测试供应商", "ext_order_no": "EXT-OK-1"}

            monkeypatch.setattr(ClosedLoop, "_services", staticmethod(lambda: (None, lambda: FakeRpa(), None, None)))
            monkeypatch.setattr(ClosedLoop, "get_current_run_id", staticmethod(lambda: 7))

            msg = ClosedLoop._step3_rpa_order()
            assert "幂等测试商品→幂等测试供应商" in msg
            db.session.refresh(sug)
            assert sug.status == "ordered"
            assert sug.run_id == 7


class TestDataAgentReadOnlySession:
    """P2-3: DataAgent 只读事务 —— 写操作在 DB 层被拒，不依赖关键字清单"""

    def test_execute_sql_rejects_write(self, app, seed):
        from services.data_agent import DataAgent

        with app.app_context():
            agent = DataAgent()
            result, count = agent._execute_sql(f"UPDATE erp_product SET stock_qty = 0 WHERE id = {seed['product_id']}")
            assert result is None and count == 0  # 被只读事务拒绝并吞为失败结果
            db.session.rollback()

            prod = db.session.get(Product, seed["product_id"])
            assert prod.stock_qty != 0  # 数据未被改动

    def test_execute_sql_select_still_works(self, app, seed):
        from services.data_agent import DataAgent

        with app.app_context():
            agent = DataAgent()
            result, count = agent._execute_sql("SELECT id, sku FROM erp_product")
            assert count >= 1 and result[0]["sku"] == "IDEM-001"


class TestAuthHardening:
    """P2-5: JWT 独立密钥 / IP 维度锁定 / refresh 旋转重放兜底"""

    def _make_user(self, app, username):
        import json
        import uuid

        from werkzeug.security import generate_password_hash

        from models.system import Role

        with app.app_context():
            role = Role(name=f"p2-role-{uuid.uuid4().hex[:8]}", permissions=json.dumps(["*:*"]))
            user = User(username=username, password_hash=generate_password_hash("GoodPass@1"), is_active=True)
            user.roles.append(role)
            db.session.add_all([role, user])
            db.session.commit()
            return user.id

    def test_jwt_uses_dedicated_secret(self, app):
        """设置独立 JWT_SECRET_KEY 后，用回退 SECRET_KEY 验签失败（密钥真正隔离）"""
        from services.auth import AuthService

        uid = self._make_user(app, "jwt-key-user")
        with app.app_context():
            app.config["SECRET_KEY"] = "a" * 48
            app.config["JWT_SECRET_KEY"] = "b" * 48
            user = db.session.get(User, uid)
            token, _, _ = AuthService._encode(AuthService.ACCESS_TOKEN_EXPIRES, "access", user)

            import jwt as pyjwt

            payload = pyjwt.decode(token, "b" * 48, algorithms=["HS256"])  # 新密钥可验
            assert payload["type"] == "access"
            with __import__("pytest").raises(pyjwt.InvalidTokenError):
                pyjwt.decode(token, "a" * 48, algorithms=["HS256"])  # 旧密钥拒验

    def test_ip_dimension_lockout(self, app, monkeypatch):
        """同 IP 跨用户名失败 20 次后，正确密码也被拒（AuditLog fallback 路径）"""
        from models.system import AuditLog
        from services.auth import AuthService

        with app.app_context():
            db.session.query(AuditLog).delete()
            db.session.commit()
            # 预置 20 条失败记录（绕过逐次登录，fallback 按 AuditLog 计数）。
            # 逐条提交：audit_log 的 before_insert 钩子在 SQLite 下按 MAX(id)+1 兜底分配 ID，
            # 同一 flush 批量插入会拿到重复 ID（单条插入的生产路径不受影响）
            ip = "203.0.113.9"
            for i in range(20):
                db.session.add(
                    AuditLog(actor=f"u{i}", action="login_failed", target_type="auth", detail="密码错误", ip=ip)
                )
                db.session.commit()

            self._make_user(app, "ip-lock-user")
            result, err = AuthService.login("ip-lock-user", "GoodPass@1", ip=ip)
            assert result is None and "登录失败次数过多" in err

    def test_refresh_rotation_replay_rejected(self, app):
        """同一 refresh_token 旋转后重放：被拒（旧 jti 已进黑名单）"""
        from services.auth import AuthService

        self._make_user(app, "refresh-replay-user")
        with app.app_context():
            result, err = AuthService.login("refresh-replay-user", "GoodPass@1", ip="127.0.0.1")
            assert err is None
            refresh_tok = result["refresh_token"]

            r1, err1 = AuthService.refresh_token(refresh_tok, ip="127.0.0.1")
            assert err1 is None and r1["refresh_token"] != refresh_tok

            r2, err2 = AuthService.refresh_token(refresh_tok, ip="127.0.0.1")
            assert r2 is None and "已被" in err2  # 黑名单拦截重放
