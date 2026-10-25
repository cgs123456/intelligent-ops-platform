"""P0 修复回归测试

覆盖本轮 P0/P1 修复的关键行为，防止回归：
1. Windows 文件锁（O_NOFOLLOW AttributeError 修复）—— 跨进程互斥真正生效
2. ERP 数量/单价正数校验 —— 负数 qty 不再能凭空增加库存
3. 审计 hash chain —— created_at 哈希口径一致，verify_chain 不再误报
4. 审计操作人防伪造 —— get_actor_name 认证身份优先
5. 幂等占位锁 —— cache.add() 原子 SETNX + 短 TTL
6. ADS 缓存 —— 固定 key 跨实例命中
7. 静态资源指纹 —— index.html 带 ?v=
"""

import os

import pytest

# ==================== 1. Windows 文件锁 ====================


class TestFileLock:
    def test_file_lock_created_and_mutually_exclusive(self, tmp_path, monkeypatch):
        """P0-2: _file_lock 必须能创建锁文件（Windows 无 os.O_NOFOLLOW，
        原实现 AttributeError 被静默吞掉，锁从未创建）且同 (run_id, step) 互斥"""
        from services import closed_loop

        monkeypatch.setenv("LOOP_LOCK_DIR", str(tmp_path))
        f1, p1 = closed_loop._file_lock(9999, 3)
        try:
            assert f1 is not None, "锁文件创建失败（Windows O_NOFOLLOW 回归？）"
            assert os.path.exists(p1)
            assert closed_loop._try_acquire_file_lock(f1) is True

            # 第二把锁：FileExistsError 分支重新打开，加锁必须失败
            f2, p2 = closed_loop._file_lock(9999, 3)
            try:
                assert f2 is not None
                assert closed_loop._try_acquire_file_lock(f2) is False, "同一锁被两个句柄同时持有"
            finally:
                if f2:
                    f2.close()
        finally:
            if f1:
                f1.close()
                try:
                    os.remove(p1)
                except OSError:
                    pass

    def test_file_lock_released_allows_reacquire(self, tmp_path, monkeypatch):
        """释放锁文件后应可重新创建并获取"""
        from services import closed_loop

        monkeypatch.setenv("LOOP_LOCK_DIR", str(tmp_path))
        f1, p1 = closed_loop._file_lock(8888, 4)
        assert f1 is not None
        assert closed_loop._try_acquire_file_lock(f1) is True
        f1.close()
        os.remove(p1)

        f2, p2 = closed_loop._file_lock(8888, 4)
        try:
            assert f2 is not None
            assert closed_loop._try_acquire_file_lock(f2) is True
        finally:
            if f2:
                f2.close()
                try:
                    os.remove(p2)
                except OSError:
                    pass


# ==================== 2. ERP 数量/单价校验 ====================


class TestQtyPriceValidation:
    @pytest.mark.parametrize(
        "qty,price",
        [(-5, 10), (0, 10), (5, 0), (5, -1), ("abc", 10), (2.5, 10), (None, 10), (5, None), (5, "x")],
    )
    def test_invalid_inputs_rejected(self, qty, price):
        from services.erp_service import ERPService

        with pytest.raises(ValueError):
            ERPService._validate_qty_price(qty, price)

    @pytest.mark.parametrize("qty,price", [(5, 10), (5, "12.5"), (5, 12.5)])
    def test_valid_inputs_accepted(self, qty, price):
        from services.erp_service import ERPService

        ERPService._validate_qty_price(qty, price)

    def test_string_qty_rejected(self):
        """qty 为 JSON 数字字段，字符串一律拒绝（防止 '5' 之类的隐式转换）"""
        from services.erp_service import ERPService

        with pytest.raises(ValueError):
            ERPService._validate_qty_price("5", 10)

    @pytest.mark.usefixtures("db_session")
    def test_negative_qty_sale_order_rejected(self, app):
        """负数 qty 创建销售单必须 400（负 qty 会 stock_qty -= (-5) 凭空 +5）"""
        from extensions import db as _db
        from models.erp import Product

        with app.app_context():
            p = Product(sku="TST-NEG", name="负数测试品", stock_qty=100, safety_stock=10, sale_price=10)
            _db.session.add(p)
            _db.session.commit()
            try:
                from services.erp_service import ERPService

                with pytest.raises(ValueError):
                    ERPService().create_sale_order(product_id=p.id, qty=-5, unit_price=10, customer="t")
                _db.session.expire_all()
                assert _db.session.get(Product, p.id).stock_qty == 100, "库存不应被负数 qty 改变"
            finally:
                _db.session.delete(p)
                _db.session.commit()


# ==================== 3. 审计 hash chain ====================


@pytest.mark.usefixtures("db_session")
class TestAuditHashChain:
    def test_verify_chain_passes_without_explicit_created_at(self, app):
        """P0-10: created_at 由 before_insert 事件内显式赋值并参与哈希，
        未显式传 created_at 的记录 verify_chain 必须通过（原实现哈希与落库值差微秒全表误报）"""
        from extensions import db as _db
        from models.system import AuditLog

        with app.app_context():
            for i in range(3):
                _db.session.add(AuditLog(actor="chain-test", action=f"act{i}", target_type="t", detail=f"d{i}"))
                _db.session.commit()
            tampered = AuditLog.verify_chain()
            assert tampered is None, f"verify_chain 误报篡改: id={tampered}"

    def test_verify_chain_ranged_check_works(self, app):
        """P0-10: 范围校验起点信任窗口首条的 prev_hash（原实现固定期待空串，
        范围校验永远把首条判为篡改）"""
        from extensions import db as _db
        from models.system import AuditLog

        with app.app_context():
            for i in range(3):
                _db.session.add(AuditLog(actor="range-test", action=f"r{i}", target_type="t"))
                _db.session.commit()
            rows = _db.session.query(AuditLog).filter_by(actor="range-test").order_by(AuditLog.id).all()
            start_id = rows[0].id
            end_id = rows[-1].id
            # 从窗口中段开始校验（跳过首条）也不应误报
            tampered = AuditLog.verify_chain(start_id=start_id + 1, end_id=end_id)
            assert tampered is None, f"范围校验误报篡改: id={tampered}"


# ==================== 4. 审计操作人防伪造 ====================


class TestActorName:
    def test_authenticated_user_overrides_body_value(self, app):
        """P0-11: 通过真实 JWT 认证（sub 非空）时，body 传入的 actor 被忽略"""
        from flask import g

        from services.rbac import get_actor_name

        with app.test_request_context():
            g.current_user = {"sub": 1, "username": "alice"}
            assert get_actor_name("spoofed-attacker") == "alice"

    def test_dev_passthrough_falls_back_to_body(self, app):
        """dev 放行模式（sub=0）回退 body 值，保持旧行为"""
        from flask import g

        from services.rbac import get_actor_name

        with app.test_request_context():
            g.current_user = {"sub": 0, "username": "dev"}
            assert get_actor_name("bob") == "bob"

    def test_no_auth_defaults(self, app):
        from services.rbac import get_actor_name

        with app.test_request_context():
            assert get_actor_name(None) == "system"
            assert get_actor_name("carol") == "carol"


# ==================== 5. 幂等占位锁 ====================


class TestIdempotencyProcessingLock:
    def test_processing_marker_ttl_is_short(self):
        """P0/M5: 占位锁必须用独立短 TTL —— 原 24h TTL 在 worker 崩溃后
        会让所有重试永远 409"""
        from services import idempotency

        assert idempotency._PROCESSING_TTL <= 3600
        assert idempotency._PROCESSING_TTL < idempotency._TTL_SECONDS

    def test_concurrent_same_key_second_blocked(self, app, monkeypatch):
        """cache.add() 原子占位：同 key 第二个并发请求返回 409（SETNX 语义）"""
        from flask import jsonify

        from services import idempotency

        fake = self._FakeCache()
        monkeypatch.setattr(idempotency, "_is_redis_available", lambda: True)
        monkeypatch.setattr(idempotency, "cache", fake)

        @idempotency.idempotent()
        def slow_view():
            return jsonify({"ok": True}), 200

        with app.test_request_context("/api/x", method="POST", headers={"Idempotency-Key": "conc-1"}):
            assert slow_view()[1] == 200

        # 模拟：业务执行中（占位 marker 已写入）另一个请求进来
        key = idempotency._cache_key("conc-1")
        fake._store[key] = idempotency._PROCESSING_MARKER
        with app.test_request_context("/api/x", method="POST", headers={"Idempotency-Key": "conc-1"}):
            resp = slow_view()
            assert resp[1] == 409

    class _FakeCache:
        def __init__(self):
            self._store = {}

        def get(self, key):
            return self._store.get(key)

        def add(self, key, value, timeout=None):
            if key in self._store:
                return False
            self._store[key] = value
            return True

        def set(self, key, value, timeout=None):
            self._store[key] = value

        def delete(self, key):
            self._store.pop(key, None)


# ==================== 6. ADS 缓存 ====================


class TestAdsCache:
    def test_cache_key_stable_across_instances(self, app):
        """P0/M3: ADS 缓存 key 不含实例信息，跨请求一致"""
        with app.app_context():
            from services.warehouse_service import WarehouseService, _ads_cache_key

            assert _ads_cache_key() == _ads_cache_key()
            assert WarehouseService().get_ads_data.__self__ is not None

    @pytest.mark.usefixtures("db_session")
    def test_ads_cache_hit_across_new_instances(self, app, monkeypatch):
        """新实例第二次调用必须命中缓存（原 memoize key 含 self 永不命中）"""
        with app.app_context():
            from extensions import cache
            from services.warehouse_service import WarehouseService, _ads_cache_key

            cache.delete(_ads_cache_key())
            calls = {"n": 0}

            def fake_load(self):
                calls["n"] += 1
                return {"suggestions": [], "report": None}

            monkeypatch.setattr(WarehouseService, "_load_ads_data", fake_load)
            try:
                WarehouseService().get_ads_data()
                WarehouseService().get_ads_data()  # 新实例
                WarehouseService().get_ads_data()  # 再来一个
                assert calls["n"] == 1, "ADS 缓存未命中（memoize 实例 key 回归？）"
            finally:
                cache.delete(_ads_cache_key())


# ==================== 8. 退货/调拨数量校验（P0-1）====================


class TestReturnTransferQtyValidation:
    def test_validate_positive_qty_rejects(self):
        """_validate_positive_qty：0/负数/小数/字符串/bool/None 一律拒绝"""
        import pytest as _pytest

        from services.erp_service import ERPService

        for qty in [0, -1, 2.5, "3", True, None]:
            with _pytest.raises(ValueError):
                ERPService._validate_positive_qty(qty)

    def test_validate_positive_qty_accepts(self):
        from services.erp_service import ERPService

        ERPService._validate_positive_qty(1)
        ERPService._validate_positive_qty(99999)

    def test_negative_return_qty_rejected(self, app):
        """P0-1: 负数退货数量必须拒绝（负 qty 会 stock_qty += 负数，凭空扣减库存）"""
        with app.app_context():
            from services.erp_service import ERPService

            with pytest.raises(ValueError, match="正整数"):
                ERPService().create_return_order("SO-NOT-EXIST", 1, qty=-3, refund_amount=30)

    def test_nonpositive_return_refund_rejected(self, app):
        """P0-1: 退款金额 0/负数/非数字必须拒绝（负红字凭证）"""
        with app.app_context():
            from services.erp_service import ERPService

            for refund in [0, -5, "abc"]:
                with pytest.raises(ValueError):
                    ERPService().create_return_order("SO-NOT-EXIST", 1, qty=1, refund_amount=refund)

    def test_negative_transfer_qty_rejected(self, app):
        """P0-1: 负数调拨数量必须拒绝（负 qty 会同时凭空增减两个仓库库存）"""
        with app.app_context():
            from services.erp_service import ERPService

            with pytest.raises(ValueError, match="正整数"):
                ERPService().transfer_stock(999, qty=-2, from_warehouse_id=1, to_warehouse_id=2)


# ==================== 9. MFA 禁用二次验证（P0 安全）====================


class TestMfaDisableRequiresReauth:
    """禁用 MFA 必须复核密码 + TOTP/备用码 —— 凭被盗 access token 不得直接拆除第二因素"""

    PASSWORD = "{{SECRET_9n4xuwbu}}"

    def _make_user(self, app):
        import json
        from uuid import uuid4

        from extensions import db as _db
        from models.system import Role, User
        from services.password_policy import hash_password

        role = Role(name=f"mfa-role-{uuid4().hex[:8]}", permissions=json.dumps(["*:*"]))
        user = User(
            username=f"mfa_user_{uuid4().hex[:8]}",
            password_hash=hash_password(self.PASSWORD),
            must_change_password=False,
        )
        user.roles.append(role)
        _db.session.add(user)
        _db.session.commit()
        return user

    def test_mfa_disable_full_flow(self, app, client, monkeypatch):
        import pyotp

        from extensions import db as _db
        from models.system import User
        from services.auth import AuthService

        # 路由从 g.current_user（JWT sub）取身份：TESTING 模式下装饰器直接放行且不设置
        # g.current_user，必须关掉 TESTING、开启 RBAC 才走真实 JWT 解码链路
        monkeypatch.setitem(app.config, "TESTING", False)
        monkeypatch.setitem(app.config, "RBAC_ENABLED", True)

        with app.app_context():
            user = self._make_user(app)
            # 先在未启用 MFA 状态下登录拿 token（MFA 启用后 login 只返回 mfa_required）
            login_result = AuthService.login(user.username, self.PASSWORD)
            if isinstance(login_result, tuple):
                login_result = login_result[0]
            token = login_result["access_token"]
            # 拿到 token 后再启用 MFA（模拟用户已开启 MFA 的常态）
            secret = pyotp.random_base32()
            user.mfa_secret = secret
            user.mfa_enabled = True
            _db.session.commit()
            user_id = user.id
            code = pyotp.TOTP(secret).now()

        headers = {"Authorization": f"Bearer {token}"}
        url = "/api/v1/auth/mfa/disable"

        # 1) 缺密码 → 401
        rv = client.post(url, json={"mfa_code": code}, headers=headers)
        assert rv.status_code == 401, rv.get_data(as_text=True)
        # 2) 密码错误 → 401
        rv = client.post(url, json={"password": "WrongPass1", "mfa_code": code}, headers=headers)
        assert rv.status_code == 401
        # 3) 密码正确但验证码错误 → 401，MFA 仍启用
        rv = client.post(url, json={"password": self.PASSWORD, "mfa_code": "000000"}, headers=headers)
        assert rv.status_code == 401
        # 4) 密码 + 有效 TOTP → 200，MFA 关闭且密钥清空
        rv = client.post(url, json={"password": self.PASSWORD, "mfa_code": code}, headers=headers)
        assert rv.status_code == 200
        with app.app_context():
            u = _db.session.get(User, user_id)
            assert u.mfa_enabled is False
            assert not u.mfa_secret

    def test_mfa_disable_without_mfa_enabled_needs_only_password(self, app, client, monkeypatch):
        """MFA 本就未启用的用户：密码复核即可禁用（幂等操作）"""
        from services.auth import AuthService

        monkeypatch.setitem(app.config, "TESTING", False)
        monkeypatch.setitem(app.config, "RBAC_ENABLED", True)

        with app.app_context():
            user = self._make_user(app)
            login_result = AuthService.login(user.username, self.PASSWORD)
            if isinstance(login_result, tuple):
                login_result = login_result[0]
            token = login_result["access_token"]

        rv = client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": self.PASSWORD},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert rv.status_code == 200


# ==================== 7. 静态资源指纹 ====================


def test_index_html_carries_hashed_assets(client):
    """P0-8（演进）：index.html 由 Vite 构建产物生成，静态资源文件名自带内容 hash
    （/static/dist/assets/index-<hash>.js），缓存语义等价于旧的 ?v= 查询参数方案。
    构建产物未提交时（仅后端测试环境）跳过断言。"""
    rv = client.get("/")
    html = rv.get_data(as_text=True)
    if "/static/dist/assets/" not in html:
        import pytest

        pytest.skip("templates/index.html 仍为占位（前端构建产物未生成）")
    assert 'src="/static/dist/assets/' in html
    assert '<div id="root">' in html
