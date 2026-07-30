"""P2-14: 多级缓存服务

三级缓存：L1 进程内 LRU（TTLCache）→ L2 Redis（flask-caching cache）→ L3 数据库/计算。
读请求依次查询 L1→L2→L3，命中即返回并回填上层；写请求同时写 L1+L2。

设计要点：
  - L1 默认 maxsize=1024，可通过构造参数 ``l1_maxsize`` 配置；
  - L1 使用 cachetools.TTLCache；若 cachetools 未安装则降级为内置简易 TTLCache，
    保证开发/测试环境无需额外安装依赖即可运行；
  - 线程安全：L1 读写加 ``threading.RLock``；
  - 兼容 Redis 不可用：L2 操作失败时捕获异常并降级到 L1+L3，不影响主流程；
  - stats() 返回各级命中率（hit/miss 计数器），便于监控缓存效果。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# L1：TTLCache（优先 cachetools，缺失则降级为内置实现）
# ------------------------------------------------------------------
try:
    from cachetools import TTLCache as _TTLCache  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - 降级路径，cachetools 未安装时启用
    _TTLCache = None  # type: ignore[assignment]


class _FallbackTTLCache:
    """cachetools 未安装时的内置降级 TTLCache（LRU + TTL）。

    仅实现 MultiLevelCache 需要的最小接口（get/set/__contains__/pop/clear），
    生产环境推荐安装 cachetools 获得更完善的实现。
    """

    def __init__(self, maxsize: int, ttl: float) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: "OrderedDict[Any, tuple]" = OrderedDict()  # key -> (value, expire_at)
        self._lock = threading.RLock()

    def _expired(self, expire_at: float) -> bool:
        return expire_at <= time.monotonic()

    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            value, expire_at = entry
            if self._expired(expire_at):
                self._data.pop(key, None)
                return default
            # LRU：命中后移到末尾
            self._data.move_to_end(key)
            return value

    def set(self, key: Any, value: Any, ttl: Optional[float] = None) -> None:
        with self._lock:
            # 容量淘汰：超出 maxsize 删除最旧
            while len(self._data) >= self.maxsize:
                self._data.popitem(last=False)
            expire_at = time.monotonic() + (ttl if ttl is not None else self.ttl)
            self._data[key] = (value, expire_at)
            self._data.move_to_end(key)

    def __contains__(self, key: Any) -> bool:  # pragma: no cover - 简单代理
        return self.get(key, _SENTINEL) is not _SENTINEL

    def pop(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            entry = self._data.pop(key, None)
            if entry is None:
                return default
            value, expire_at = entry
            if self._expired(expire_at):
                return default
            return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:  # pragma: no cover - 调试用
        return len(self._data)


_SENTINEL = object()


def _make_l1_cache(maxsize: int, ttl: float):
    """创建 L1 缓存实例：优先 cachetools.TTLCache，否则降级为内置实现。"""
    if _TTLCache is not None:
        return _TTLCache(maxsize=maxsize, ttl=ttl)
    return _FallbackTTLCache(maxsize=maxsize, ttl=ttl)


class MultiLevelCache:
    """多级缓存服务（L1 进程内 → L2 Redis → L3 数据库/计算）。

    :param cache: flask-caching 的 Cache 实例（L2），传 None 表示无 L2。
    :param l1_maxsize: L1 LRU 最大条目数，默认 1024。
    :param l1_ttl: L1 默认 TTL（秒），默认 300。
    :param l2_default_ttl: L2 默认 TTL（秒），默认 300。
    """

    def __init__(
        self,
        cache: Optional[Any] = None,
        l1_maxsize: int = 1024,
        l1_ttl: float = 300.0,
        l2_default_ttl: float = 300.0,
    ) -> None:
        self._cache = cache  # flask-caching Cache 实例（可能为 None）
        self._l1 = _make_l1_cache(maxsize=l1_maxsize, ttl=l1_ttl)
        self._l1_maxsize = l1_maxsize
        self._l1_lock = threading.RLock()
        self._l2_default_ttl = l2_default_ttl

        # 命中计数器（线程安全用 GIL 即可，但显式加锁更稳妥）
        self._stats: Dict[str, int] = {
            "l1_hit": 0,
            "l1_miss": 0,
            "l2_hit": 0,
            "l2_miss": 0,
            "l3_hit": 0,  # loader 命中（即穿透到 L3）
            "l2_error": 0,  # L2 操作异常次数
        }
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 计数器
    # ------------------------------------------------------------------
    def _incr(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + n

    # ------------------------------------------------------------------
    # L2 包装：捕获异常，Redis 不可用时降级
    # ------------------------------------------------------------------
    def _l2_get(self, key: str) -> Any:
        """从 L2 读取，Redis 不可用时返回哨兵值（不抛异常）。"""
        if self._cache is None:
            return _SENTINEL
        try:
            return self._cache.get(key)
        except Exception as exc:
            self._incr("l2_error")
            logger.warning("L2 缓存读取失败，降级到 L1+L3: %s", exc)
            return _SENTINEL

    def _l2_set(self, key: str, value: Any, ttl: float) -> bool:
        """写入 L2，Redis 不可用时返回 False（不抛异常）。"""
        if self._cache is None:
            return False
        try:
            self._cache.set(key, value, timeout=ttl)
            return True
        except Exception as exc:
            self._incr("l2_error")
            logger.warning("L2 缓存写入失败，降级到 L1: %s", exc)
            return False

    def _l2_delete(self, key: str) -> bool:
        """删除 L2 中的 key，Redis 不可用时返回 False。"""
        if self._cache is None:
            return False
        try:
            self._cache.delete(key)
            return True
        except Exception as exc:
            self._incr("l2_error")
            logger.warning("L2 缓存删除失败: %s", exc)
            return False

    def _l2_delete_many(self, keys) -> int:
        """批量删除 L2 中的 keys，返回成功删除数。"""
        if self._cache is None or not keys:
            return 0
        try:
            self._cache.delete_many(*keys)
            return len(keys)
        except Exception as exc:
            self._incr("l2_error")
            logger.warning("L2 缓存批量删除失败: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def get(
        self,
        key: str,
        loader: Optional[Callable[[], Any]] = None,
        ttl_l1: float = 60.0,
        ttl_l2: float = 300.0,
    ) -> Any:
        """读取缓存：依次查 L1→L2→L3（loader），命中即返回并回填上层。

        :param key: 缓存键。
        :param loader: L3 加载函数（穿透时调用），为 None 时不回源。
        :param ttl_l1: 回填 L1 的 TTL（秒），默认 60。
        :param ttl_l2: 回填 L2 的 TTL（秒），默认 300。
        :return: 缓存值；未命中且无 loader 返回 None。
        """
        # ---------- L1 ----------
        with self._l1_lock:
            value = self._l1.get(key, _SENTINEL)
        if value is not _SENTINEL:
            self._incr("l1_hit")
            return value
        self._incr("l1_miss")

        # ---------- L2 ----------
        value = self._l2_get(key)
        if value is not None and value is not _SENTINEL:
            self._incr("l2_hit")
            # 回填 L1
            with self._l1_lock:
                self._l1.set(key, value, ttl=ttl_l1)
            return value
        self._incr("l2_miss")

        # ---------- L3 ----------
        if loader is None:
            return None
        try:
            value = loader()
        except Exception as exc:
            logger.exception("L3 loader 执行失败 key=%s: %s", key, exc)
            raise
        self._incr("l3_hit")

        if value is None:
            return None

        # 回填 L2（先填 L2，避免 L2 故障时丢失）
        self._l2_set(key, value, ttl_l2)
        # 回填 L1
        with self._l1_lock:
            self._l1.set(key, value, ttl=ttl_l1)
        return value

    def set(self, key: str, value: Any, ttl_l2: float = 300.0) -> None:
        """写入缓存：同时写 L1 和 L2。

        :param key: 缓存键。
        :param value: 缓存值。
        :param ttl_l2: L2 的 TTL（秒），默认 300。L1 使用构造时默认 TTL。
        """
        # 写 L1
        with self._l1_lock:
            self._l1.set(key, value)
        # 写 L2（Redis 不可用时降级，仅 L1 生效）
        self._l2_set(key, value, ttl_l2)

    def invalidate(self, key: str) -> None:
        """使缓存失效：同时清除 L1 和 L2 中对应 key。"""
        # 清 L1
        with self._l1_lock:
            self._l1.pop(key, None)
        # 清 L2
        self._l2_delete(key)

    def invalidate_pattern(self, pattern: str) -> int:
        """清除匹配模式的所有 key（L1 遍历删除 + L2 Redis scan）。

        :param pattern: key 匹配模式（支持 ``*`` 通配符，如 ``user:*``）。
        :return: 清除的 key 数量（L2 删除数；L1 始终清空匹配项）。
        """
        import fnmatch

        # ---------- L1：遍历匹配并删除 ----------
        with self._l1_lock:
            keys_to_del = [k for k in list(self._l1._data.keys()) if fnmatch.fnmatch(str(k), pattern)]  # type: ignore[attr-defined]
            for k in keys_to_del:
                self._l1.pop(k, None)

        # ---------- L2：Redis SCAN 匹配并删除 ----------
        if self._cache is None:
            return len(keys_to_del)
        try:
            return self._scan_delete_l2(pattern)
        except Exception as exc:
            self._incr("l2_error")
            logger.warning("L2 invalidate_pattern 失败: %s", exc)
            return len(keys_to_del)

    def _scan_delete_l2(self, pattern: str) -> int:
        """通过 Redis SCAN 删除匹配 key（兼容 flask-caching 后端）。"""
        # 尝试获取底层 redis client（flask-caching RedisCache 暴露 _client）
        client = getattr(self._cache, "_client", None) or getattr(self._cache, "cache", None)
        if client is None:
            # 无法拿到底层 client，退化为 delete_many（无 key 列表则返回 0）
            return 0
        redis_pattern = pattern.replace("*", "*")  # 保持通配符语义
        deleted = 0
        pipe = getattr(client, "pipeline", None)
        # SCAN 迭代匹配 key
        scan_iter = getattr(client, "scan_iter", None)
        if scan_iter is not None:
            batch = []
            for key in scan_iter(redis_pattern):
                batch.append(key)
                if len(batch) >= 100:
                    deleted += self._l2_delete_many(batch)
                    batch = []
            if batch:
                deleted += self._l2_delete_many(batch)
        else:
            # 无 scan_iter（如 SimpleCache），尝试 keys
            keys_fn = getattr(client, "keys", None)
            if keys_fn is not None:
                keys = keys_fn(redis_pattern)
                if keys:
                    deleted = self._l2_delete_many(keys)
        return deleted

    def stats(self) -> Dict[str, Any]:
        """返回各级缓存命中统计。

        :return: 包含 hit/miss 计数与命中率的字典。
        """
        with self._stats_lock:
            stats = dict(self._stats)
        # 计算命中率
        l1_total = stats["l1_hit"] + stats["l1_miss"]
        l2_total = stats["l2_hit"] + stats["l2_miss"]
        stats["l1_hit_rate"] = round(stats["l1_hit"] / l1_total, 4) if l1_total else 0.0
        stats["l2_hit_rate"] = round(stats["l2_hit"] / l2_total, 4) if l2_total else 0.0
        stats["l1_size"] = len(self._l1) if hasattr(self._l1, "__len__") else -1
        return stats

    def clear_l1(self) -> None:
        """清空 L1 缓存（仅进程内，不影响 L2）。"""
        with self._l1_lock:
            self._l1.clear()
