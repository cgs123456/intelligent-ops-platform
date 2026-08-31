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


# ==================== 7. 静态资源指纹 ====================


def test_index_html_carries_static_version(client):
    """P0-8: index.html 引用静态资源必须带 ?v= 内容指纹，
    否则 nginx immutable 缓存下发版后最长 7 天不更新"""
    rv = client.get("/")
    html = rv.get_data(as_text=True)
    assert "app.js?v=" in html, "app.js 缺少 ?v= 内容指纹"
    assert "style.css?v=" in html, "style.css 缺少 ?v= 内容指纹"
