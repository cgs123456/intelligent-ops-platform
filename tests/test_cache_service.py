"""P2-14: 多级缓存服务单元测试

测试覆盖：
  - L1 命中、L2 命中、L3 回填
  - invalidate 清除
  - stats 计数
  - Redis 不可用降级（L2 抛异常时降级到 L1+L3）

测试可独立运行，使用自制的 FakeCache 替代 flask-caching，无需 Redis。
"""

import pytest

from services.cache_service import MultiLevelCache


pytestmark = pytest.mark.unit


class FakeCache:
    """模拟 flask-caching 的 Cache 接口（内存 dict 实现，便于测试）。

    支持模拟「Redis 不可用」：将 broken=True 后所有方法抛异常。
    """

    def __init__(self):
        self._store = {}
        self.broken = False  # True 时模拟 Redis 不可用
        self.call_count = {"get": 0, "set": 0, "delete": 0, "delete_many": 0}

    def _raise_if_broken(self):
        if self.broken:
            raise RuntimeError("Redis 不可用（模拟故障）")

    def get(self, key):
        self._raise_if_broken()
        self.call_count["get"] += 1
        return self._store.get(key)

    def set(self, key, value, timeout=None):
        self._raise_if_broken()
        self.call_count["set"] += 1
        self._store[key] = value

    def delete(self, key):
        self._raise_if_broken()
        self.call_count["delete"] += 1
        self._store.pop(key, None)

    def delete_many(self, *keys):
        self._raise_if_broken()
        self.call_count["delete_many"] += 1
        for k in keys:
            self._store.pop(k, None)


@pytest.fixture
def fake_cache():
    """提供内存版 flask-caching 替身。"""
    return FakeCache()


@pytest.fixture
def cache(fake_cache):
    """构造带 L2（FakeCache）的 MultiLevelCache，L1 容量小便于测试淘汰。"""
    return MultiLevelCache(cache=fake_cache, l1_maxsize=8, l1_ttl=300, l2_default_ttl=300)


class TestL1Hit:
    """L1 命中场景"""

    def test_l1_hit_returns_value_without_l2(self, cache, fake_cache):
        """首次 get 走 L3 回填 L1，第二次 get 应直接命中 L1（不访问 L2）。"""
        calls = {"loader": 0}

        def loader():
            calls["loader"] += 1
            return "v1"

        # 第一次：L1/L2 均未命中 → L3 回填
        assert cache.get("k1", loader=loader) == "v1"
        assert calls["loader"] == 1
        # L2 也被回填了
        assert fake_cache.call_count["set"] == 1

        # 第二次：应命中 L1
        fake_cache.call_count["get"] = 0  # 重置便于断言
        assert cache.get("k1", loader=loader) == "v1"
        assert calls["loader"] == 1  # loader 没再被调用
        assert fake_cache.call_count["get"] == 0  # L2 未被访问（L1 命中）

        stats = cache.stats()
        assert stats["l1_hit"] == 1
        assert stats["l1_miss"] == 1
        assert stats["l3_hit"] == 1

    def test_set_then_l1_hit(self, cache):
        """set 后立即 get 应命中 L1。"""
        cache.set("foo", "bar", ttl_l2=60)
        assert cache.get("foo", loader=lambda: "should_not_call") == "bar"
        stats = cache.stats()
        assert stats["l1_hit"] == 1
        assert stats["l1_miss"] == 0


class TestL2Hit:
    """L2 命中场景"""

    def test_l2_hit_after_l1_invalidate(self, cache, fake_cache):
        """set 后清空 L1，再次 get 应命中 L2 并回填 L1。"""
        cache.set("k", "v", ttl_l2=60)
        # 清空 L1 模拟 L1 失效
        cache.clear_l1()
        assert fake_cache.call_count["get"] == 0

        # get：L1 miss → L2 hit → 回填 L1
        result = cache.get("k", loader=lambda: "fallback")
        assert result == "v"
        stats = cache.stats()
        assert stats["l1_miss"] == 1
        assert stats["l2_hit"] == 1
        # 再次 get 应命中 L1（L2 已回填 L1）
        cache.get("k", loader=lambda: "fallback")
        assert cache.stats()["l1_hit"] == 1

    def test_l2_miss_l3_hit(self, cache, fake_cache):
        """L1/L2 均未命中 → L3 命中并回填两层。"""
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return "computed"

        result = cache.get("missing", loader=loader)
        assert result == "computed"
        assert calls["n"] == 1
        stats = cache.stats()
        assert stats["l1_miss"] == 1
        assert stats["l2_miss"] == 1
        assert stats["l3_hit"] == 1
        # L2 也被回填
        assert fake_cache.call_count["set"] == 1
        # L2 应有值
        assert fake_cache.get("missing") == "computed"


class TestInvalidate:
    """invalidate 清除场景"""

    def test_invalidate_clears_l1_and_l2(self, cache, fake_cache):
        """invalidate 同时清 L1 + L2，下次 get 应穿透到 L3。"""
        cache.set("key", "val", ttl_l2=60)
        # 确认两层都有
        assert cache.get("key") == "val"
        cache.clear_l1()
        assert fake_cache.get("key") == "val"  # L2 仍有

        cache.invalidate("key")
        # L2 已清
        assert fake_cache.get("key") is None
        # L1 也已清（get 会 miss L1）
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return "recomputed"

        result = cache.get("key", loader=loader)
        assert result == "recomputed"
        assert calls["n"] == 1

    def test_invalidate_nonexistent_key_no_error(self, cache):
        """invalidate 不存在的 key 不应抛异常。"""
        cache.invalidate("does_not_exist")  # 不应抛异常


class TestStats:
    """stats 计数场景"""

    def test_stats_initial_zero(self, cache):
        stats = cache.stats()
        assert stats["l1_hit"] == 0
        assert stats["l1_miss"] == 0
        assert stats["l2_hit"] == 0
        assert stats["l2_miss"] == 0
        assert stats["l3_hit"] == 0
        assert stats["l1_hit_rate"] == 0.0
        assert stats["l2_hit_rate"] == 0.0

    def test_stats_hit_rate_calculation(self, cache):
        """多次访问后命中率计算正确。"""
        cache.set("a", 1)
        cache.set("b", 2)
        # a 命中 L1，b 命中 L1
        cache.get("a")
        cache.get("b")
        # c 未命中（L1 miss，无 loader 返回 None）
        cache.get("c")
        stats = cache.stats()
        # L1: 2 hit / 3 total
        assert stats["l1_hit"] == 2
        assert stats["l1_miss"] == 1
        assert stats["l1_hit_rate"] == round(2 / 3, 4)


class TestRedisUnavailable:
    """Redis 不可用降级场景"""

    def test_l2_broken_degrades_to_l1_l3(self, fake_cache):
        """L2 抛异常时应降级到 L1+L3，不影响读取。"""
        cache = MultiLevelCache(cache=fake_cache, l1_maxsize=8)
        fake_cache.broken = True  # 模拟 Redis 故障

        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return "value"

        # 首次：L1 miss → L2 故障（降级）→ L3 命中
        result = cache.get("k", loader=loader)
        assert result == "value"
        assert calls["n"] == 1
        # L3 命中计数
        stats = cache.stats()
        assert stats["l3_hit"] == 1
        assert stats["l2_error"] >= 1  # 至少一次 L2 异常

        # 第二次：应命中 L1（即使 L2 故障也不影响）
        result = cache.get("k", loader=loader)
        assert result == "value"
        assert calls["n"] == 1  # loader 未再调用
        stats = cache.stats()
        assert stats["l1_hit"] == 1

    def test_set_l2_broken_still_writes_l1(self, fake_cache):
        """L2 故障时 set 仍应写入 L1。"""
        cache = MultiLevelCache(cache=fake_cache, l1_maxsize=8)
        fake_cache.broken = True

        cache.set("key", "val", ttl_l2=60)
        # L1 有值（get 不走 L2，命中 L1）
        assert cache.get("key") == "val"
        assert cache.stats()["l1_hit"] == 1

    def test_invalidate_l2_broken_no_raise(self, fake_cache):
        """L2 故障时 invalidate 不应抛异常。"""
        cache = MultiLevelCache(cache=fake_cache, l1_maxsize=8)
        cache.set("key", "val", ttl_l2=60)
        fake_cache.broken = True
        cache.invalidate("key")  # 不应抛异常
        # L1 已清空
        assert cache.get("key") is None
        stats = cache.stats()
        assert stats["l2_error"] >= 1

    def test_no_l2_cache_works(self):
        """未传入 L2 cache 时，仅 L1+L3 工作。"""
        cache = MultiLevelCache(cache=None, l1_maxsize=8)
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return "only_l1"

        result = cache.get("k", loader=loader)
        assert result == "only_l1"
        assert calls["n"] == 1
        stats = cache.stats()
        assert stats["l2_miss"] == 1
        assert stats["l3_hit"] == 1
        # 第二次命中 L1
        cache.get("k", loader=loader)
        assert calls["n"] == 1
        assert cache.stats()["l1_hit"] == 1


class TestL1Maxsize:
    """L1 容量与配置"""

    def test_custom_l1_maxsize(self, fake_cache):
        """构造参数 l1_maxsize 可配置。"""
        cache = MultiLevelCache(cache=fake_cache, l1_maxsize=2)
        # 写入 3 个 key，超出 maxsize=2 应淘汰最旧
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        # a 应被淘汰（L1 miss），但 L2 仍有
        stats = cache.get("a", loader=lambda: "recompute")
        # a 在 L2 命中（fallback None loader 不会调），这里 loader 不应被调用因为 L2 有
        assert stats == 1  # L2 命中回填
        s = cache.stats()
        assert s["l2_hit"] >= 1
