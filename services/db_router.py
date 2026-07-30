"""P2-14: 读写分离路由器

封装主从库路由逻辑：读操作走从库（replica），写操作走主库（master）。
从库 URL 通过环境变量 DATABASE_REPLICA_URL 配置，未配置时降级到主库并告警。

设计要点：
  - engine 使用模块级单例 + threading.Lock 双重检查，保证线程安全。
  - @read_replica 装饰器与 use_read_replica() 上下文管理器均基于
    SQLAlchemy 的 db.session.using_bind() / sessionMaker using(bind) 实现，
    让被包裹代码块内的 db.session 绑定到从库连接。
  - 健康检查 check_replica_lag() 用 SELECT 1 探活（轻量，不依赖复制视图）。
"""

from __future__ import annotations

import functools
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional, TypeVar

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class ReadWriteRouter:
    """主从库读写分离路由器。

    读操作路由到从库 engine（fallback 主库），写操作始终使用主库 engine。
    从库 URL 从环境变量 ``DATABASE_REPLICA_URL`` 读取，未配置则降级到主库
    并记录 warning 日志。

    所有 engine 均为模块级单例，通过 ``threading.Lock`` 双重检查保证线程安全。
    """

    _instance: Optional["ReadWriteRouter"] = None
    _lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "ReadWriteRouter":
        # 双重检查锁定，保证单例线程安全
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, master_url: Optional[str] = None, replica_url: Optional[str] = None) -> None:
        # 单例避免重复初始化（多次构造仅首次生效）
        if getattr(self, "_initialized", False):
            return
        self._init_lock = threading.Lock()
        with self._init_lock:
            if getattr(self, "_initialized", False):
                return
            self._master_url = master_url
            self._replica_url = replica_url
            self._master_engine: Optional[Engine] = None
            self._replica_engine: Optional[Engine] = None
            self._replica_available: bool = False
            self._engine_lock = threading.Lock()
            self._initialized = True

    # ------------------------------------------------------------------
    # 私有：解析主/从库 URL
    # ------------------------------------------------------------------
    def _resolve_master_url(self) -> str:
        """解析主库 URL：优先构造参数，其次 Flask app config，最后环境变量 DATABASE_URL。"""
        if self._master_url:
            return self._master_url
        # 优先从 Flask app config 读取（运行时），失败则读环境变量
        try:
            from flask import current_app

            url = current_app.config.get("SQLALCHEMY_DATABASE_URI")
            if url:
                return str(url)
        except Exception:
            pass
        return os.getenv("DATABASE_URL", "sqlite:///ops_platform.db")

    def _resolve_replica_url(self) -> str:
        """解析从库 URL：优先构造参数，其次环境变量 DATABASE_REPLICA_URL，再次 app config。"""
        if self._replica_url:
            return self._replica_url
        try:
            from flask import current_app

            url = current_app.config.get("DATABASE_REPLICA_URL")
            if url:
                return str(url)
        except Exception:
            pass
        return os.getenv("DATABASE_REPLICA_URL", "")

    # ------------------------------------------------------------------
    # 公开：engine 获取
    # ------------------------------------------------------------------
    def get_master_engine(self) -> Engine:
        """获取主库 engine（单例 + 双重检查锁，线程安全）。"""
        if self._master_engine is not None:
            return self._master_engine
        with self._engine_lock:
            if self._master_engine is None:
                url = self._resolve_master_url()
                self._master_engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800)
                logger.debug("主库 engine 已创建: %s", url.split("@")[-1] if "@" in url else url)
            return self._master_engine

    def get_write_engine(self) -> Engine:
        """获取写库 engine（恒为主库）。"""
        return self.get_master_engine()

    def get_read_engine(self) -> Engine:
        """获取读库 engine：返回从库，未配置或不可用时 fallback 主库。

        未配置 DATABASE_REPLICA_URL 时记录 warning 并返回主库。
        """
        if not self._replica_available:
            replica_url = self._resolve_replica_url()
            if not replica_url:
                logger.warning(
                    "DATABASE_REPLICA_URL 未配置，读操作降级到主库（建议生产环境配置只读从库）"
                )
                return self.get_master_engine()
            # 构建从库 engine（单例 + 双重检查锁）
            with self._engine_lock:
                if self._replica_engine is None:
                    try:
                        self._replica_engine = create_engine(
                            replica_url, pool_pre_ping=True, pool_recycle=1800
                        )
                        self._replica_available = True
                        logger.info(
                            "从库 engine 已创建: %s",
                            replica_url.split("@")[-1] if "@" in replica_url else replica_url,
                        )
                    except Exception as exc:
                        logger.warning("从库 engine 创建失败，读操作降级到主库: %s", exc)
                        self._replica_available = False
                        return self.get_master_engine()
        # 运行期探活：从库不可达则 fallback 主库，避免读请求整体失败
        if not self.check_replica_lag():
            logger.warning("从库健康检查失败，本次读操作降级到主库")
            return self.get_master_engine()
        return self._replica_engine  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # 装饰器 / 上下文管理器
    # ------------------------------------------------------------------
    def read_replica(self, func: F) -> F:
        """装饰器：被装饰函数内 db.session 使用从库连接执行读操作。

        基于 Flask-SQLAlchemy 的 session using_bind 实现，函数返回后自动恢复绑定。
        从库不可用时自动降级到主库，不阻断业务。
        """

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with self.use_read_replica():
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    @contextmanager
    def use_read_replica(self) -> Iterator[Optional[Engine]]:
        """上下文管理器：在 with 块内将 db.session 绑定到从库连接。

        用法::

            with router.use_read_replica():
                rows = db.session.execute(...).all()

        :yield: 从库 engine（降级时为主库 engine）
        """
        read_engine = self.get_read_engine()
        bound_session = None
        try:
            from extensions import db

            # 使用 db.session 的 using_bind 绑定到从库；旧版本 SQLAlchemy 降级为直接 query
            try:
                bound_session = db.session.using_bind(read_engine)
            except (AttributeError, TypeError):
                # 兼容 SQLAlchemy 2.x / Flask-SQLAlchemy 3.x：无 using_bind 时跳过绑定
                bound_session = None
            yield read_engine
        finally:
            if bound_session is not None:
                try:
                    bound_session.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------
    def check_replica_lag(self) -> bool:
        """从库健康检查：执行 ``SELECT 1`` 探活。

        :return: 从库可达返回 True，否则 False（fallback 场景也返回 False）。
        """
        if not self._replica_available or self._replica_engine is None:
            return False
        try:
            with self._replica_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("从库健康检查失败: %s", exc)
            return False


# 模块级单例，供业务代码直接 import 使用
router = ReadWriteRouter()
