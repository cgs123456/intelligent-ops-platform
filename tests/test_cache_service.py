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


class TestNullValueCaching:
    """P2-25: None 值缓存测试

    None 用 _NULL_SENTINEL 包装后存储，区分"未命中"和"命中但值为空"，
    避免缓存穿透到 L3。
    """

    def test_none_value_cached_in_l1(self, cache):
        """set None 后 get 应命中 L1 返回 None，不调用 loader"""
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return "should_not_call"

        cache.set("null_key", None, ttl_l2=60)
        # 第一次 get 应命中 L1（None 已用哨兵包装）
        result = cache.get("null_key", loader=loader)
        assert result is None
        assert calls["n"] == 0  # loader 未调用
        stats = cache.stats()
        assert stats["l1_hit"] == 1

    def test_none_value_cached_in_l2(self, cache, fake_cache):
        """set None 后清空 L1，get 应命中 L2 返回 None"""
        cache.set("null_key", None, ttl_l2=60)
        cache.clear_l1()  # 强制走 L2

        result = cache.get("null_key", loader=lambda: "fallback")
        assert result is None
        stats = cache.stats()
        assert stats["l2_hit"] == 1

    def test_none_value_from_loader_cached(self, cache):
        """loader 返回 None 时应缓存 None，避免缓存穿透"""
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return None  # 模拟 L3 返回空

        # 首次：L1/L2 miss → L3 返回 None → 应缓存
        result = cache.get("empty_key", loader=loader)
        assert result is None
        assert calls["n"] == 1
        stats = cache.stats()
        assert stats["l3_hit"] == 1

        # 第二次：应命中 L1（None 已缓存），不再调 loader
        result = cache.get("empty_key", loader=loader)
        assert result is None
        assert calls["n"] == 1  # loader 未再调用
        stats = cache.stats()
        assert stats["l1_hit"] == 1

    def test_none_value_invalidate(self, cache):
        """invalidate None 缓存后，下次 get 应穿透到 L3"""
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return None

        cache.set("null_key", None, ttl_l2=60)
        cache.invalidate("null_key")
        result = cache.get("null_key", loader=loader)
        assert result is None
        assert calls["n"] == 1  # loader 被调用（缓存已失效）


class TestInvalidatePattern:
    """invalidate_pattern 批量清除测试"""

    def test_invalidate_pattern_l1_match(self, fake_cache):
        """invalidate_pattern 清除 L1 中匹配的 key（使用无 L2 的 cache 避免 L2 回填）"""
        # 使用无 L2 的 cache，确保 invalidate 后 get 不从 L2 回填
        cache = MultiLevelCache(cache=None, l1_maxsize=8)
        cache.set("user:1", "a")
        cache.set("user:2", "b")
        cache.set("product:1", "p")  # 不匹配，应保留

        deleted = cache.invalidate_pattern("user:*")
        # user:1 和 user:2 被清除
        assert deleted == 2
        # user:* 已清空，get 应 miss
        assert cache.get("user:1") is None
        assert cache.get("user:2") is None
        # product:1 仍在
        assert cache.get("product:1") == "p"

    def test_invalidate_pattern_no_match(self, cache):
        """invalidate_pattern 无匹配时不报错"""
        cache.set("a", 1)
        result = cache.invalidate_pattern("nonexistent:*")
        # 无匹配，返回 0
        assert result == 0
        # a 仍在
        assert cache.get("a") == 1

    def test_invalidate_pattern_no_l2(self, fake_cache):
        """无 L2 时 invalidate_pattern 仅清 L1"""
        cache = MultiLevelCache(cache=None, l1_maxsize=8)
        cache.set("k1", 1)
        cache.set("k2", 2)
        deleted = cache.invalidate_pattern("k*")
        assert deleted == 2
        assert cache.get("k1") is None
        assert cache.get("k2") is None

    def test_invalidate_pattern_l2_broken(self, fake_cache):
        """L2 故障时 invalidate_pattern 不抛异常，仅清 L1"""
        cache = MultiLevelCache(cache=fake_cache, l1_maxsize=8)
        cache.set("k1", 1)
        fake_cache.broken = True
        # 不应抛异常
        cache.invalidate_pattern("k*")
        # L1 已清
        assert cache.get("k1") is None
        stats = cache.stats()
        assert stats["l2_error"] >= 1

    def test_invalidate_pattern_specific_pattern(self, fake_cache):
        """精确匹配特定模式（使用无 L2 的 cache 避免 L2 回填）"""
        cache = MultiLevelCache(cache=None, l1_maxsize=8)
        cache.set("order:100", 1)
        cache.set("order:200", 2)
        cache.set("order:abc", 3)
        cache.set("product:100", 4)

        # 匹配 order:1 开头
        cache.invalidate_pattern("order:1*")
        # order:100 被清，order:200 和 order:abc 保留
        assert cache.get("order:100") is None
        assert cache.get("order:200") == 2
        assert cache.get("order:abc") == 3
        assert cache.get("product:100") == 4


class TestCacheSetWithTTL:
    """set 与 TTL 行为测试"""

    def test_set_overwrites_existing(self, cache):
        """set 覆盖已有 key 的新值"""
        cache.set("k", "old", ttl_l2=60)
        cache.set("k", "new", ttl_l2=60)
        assert cache.get("k") == "new"

    def test_get_no_loader_returns_none(self, cache):
        """无 loader 且未命中时返回 None"""
        result = cache.get("missing_key")
        assert result is None
        stats = cache.stats()
        assert stats["l1_miss"] == 1
        assert stats["l2_miss"] == 1
        # l3_hit 不增加（无 loader）
        assert stats["l3_hit"] == 0

    def test_get_loader_raises(self, cache):
        """loader 抛异常时 get 重新抛出"""

        def broken_loader():
            raise RuntimeError("DB 故障")

        with pytest.raises(RuntimeError, match="DB 故障"):
            cache.get("broken_key", loader=broken_loader)

    def test_l1_l2_both_hit_l1_first(self, cache, fake_cache):
        """L1 和 L2 都有值时优先命中 L1"""
        cache.set("k", "v", ttl_l2=60)
        # 重置 L2 计数器
        fake_cache.call_count["get"] = 0
        result = cache.get("k")
        assert result == "v"
        # L2 未被访问（L1 命中）
        assert fake_cache.call_count["get"] == 0
        stats = cache.stats()
        assert stats["l1_hit"] == 1
