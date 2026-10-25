"""pytest 公共 fixtures"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# 将项目根目录加入 sys.path，让 tests/ 可以 import 项目模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 测试环境变量：必须在 import app 之前设置
os.environ.setdefault("FLASK_ENV", "development")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("RBAC_ENABLED", "0")  # 测试默认关闭 RBAC（P0-7: 生产默认开启，测试显式关闭）
os.environ.setdefault("LLM_PROVIDER", "rule")  # 测试默认走规则引擎
os.environ.setdefault("CACHE_TYPE", "simple")


@pytest.fixture(scope="session")
def app():
    """会话级 Flask app fixture（使用临时 SQLite 数据库）。

    所有测试共享同一个 app 实例，但每个测试函数拿到独立的数据库事务。
    """
    from app import create_app
    from extensions import db

    # 使用临时文件 SQLite，避免污染开发的 ops_platform.db
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    app = create_app()
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{db_path}",
        RBAC_ENABLED=False,
        WTF_CSRF_ENABLED=False,
        LLM_PROVIDER="rule",
        CACHE_TYPE="simple",
        # 测试用更短的 token 过期
        JWT_ACCESS_EXPIRES=300,
        JWT_REFRESH_EXPIRES=3600,
    )

    with app.app_context():
        # P1-3: 测试使用 db.create_all() 从最新 model 定义建表（Flask 测试标准做法）。
        # 原因：测试用临时 SQLite，而部分迁移含 PostgreSQL 特定语法（如分区），
        #       跑完整迁移链在 SQLite 上会失败。db.create_all() 确保测试基于最新 model。
        # 生产环境 schema 变更必须用 flask db upgrade（硬约束），迁移链完整性由
        # tests/test_migrations.py 验证（head 单一、可被 alembic 解析）。
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

    # 清理临时数据库
    try:
        os.close(db_fd)
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture(scope="function")
def client(app):
    """测试客户端（每个测试函数独立）"""
    return app.test_client()


@pytest.fixture(autouse=True)
def _clear_auth_process_state():
    """清 AuthService 进程级缓存（token_version 缓存 / 内存黑名单 jti）。

    这些缓存按 uid/jti 键全局共享，跨测试/跨库场景（如 rbac 用独立临时库、
    用户 id 重新从 1 计数）会读到陈旧版本导致 verify 误判 401。生产单库
    uid 全局唯一无此问题，故在测试层统一免疫。
    """
    from flask import has_app_context

    from extensions import db as _db
    from services.auth import AuthService

    def _reset() -> None:
        AuthService._user_cache.clear()
        AuthService._revoked_jti_memory.clear()
        if has_app_context():
            # 长活外层 app context 使 session 跨测试存活，清陈旧 identity map
            # 防路由按 uid 取到前测试同主键旧对象（密码哈希串号 → 误 401）
            _db.session.expunge_all()

    _reset()
    yield
    _reset()


@pytest.fixture(scope="function")
def db_session(app):
    """函数级数据库 session，每个测试结束后回滚，互不污染。

    利用 SQLAlchemy 的 SAVEPOINT 机制：测试中的所有写操作在 teardown 时回滚。
    """
    from extensions import db

    connection = db.engine.connect()
    transaction = connection.begin()

    options = {"bind": connection, "binds": {}}
    session = db._make_scoped_session(options=options)

    # 替换 db.session 让业务代码使用带事务的 session
    old_session = db.session
    db.session = session

    yield session

    session.remove()
    transaction.rollback()
    connection.close()
    db.session = old_session


@pytest.fixture(scope="function")
def auth_headers(app, client):
    """获取测试用户的 JWT 认证头。

    RBAC_ENABLED=False 时返回空 dict（dev 模式放行）。
    需要认证的测试用例应显式开启 RBAC 并使用此 fixture。
    """
    import json

    from extensions import db
    from models.system import Role, User
    from services.auth import AuthService
    from services.password_policy import hash_password

    with app.app_context():
        # 确保 admin 角色和用户存在（密码已知）
        role = Role.query.filter_by(name="admin").first()
        if not role:
            role = Role(name="admin", permissions=json.dumps(["*:*"]), description="管理员")
            db.session.add(role)
            db.session.flush()

        user = User.query.filter_by(username="admin").first()
        if not user:
            user = User(
                username="admin",
                password_hash=hash_password("Test@123456"),
                must_change_password=False,
            )
            user.roles.append(role)
            db.session.add(user)
            db.session.commit()
        elif not user.roles:
            user.roles.append(role)
            db.session.commit()

        try:
            result = AuthService.login("admin", "Test@123456")
            token = result.get("access_token", "")
            return {"Authorization": f"Bearer {token}"} if token else {}
        except Exception:
            return {}


@pytest.fixture
def mock_llm(monkeypatch):
    """Mock LLM 调用，返回固定的假响应。

    用法：
        def test_xxx(self, mock_llm):
            ...
    """

    def _fake_call(*args, **kwargs):
        return "这是 LLM 测试响应"

    # patch AIGCService._call_llm
    try:
        from services.aigc_service import AIGCService

        monkeypatch.setattr(AIGCService, "_call_llm", _fake_call)
        monkeypatch.setattr(AIGCService, "_llm_available", lambda self: True)
    except ImportError:
        pass
    return _fake_call


@pytest.fixture
def disable_timeout(monkeypatch):
    """禁用闭环步骤的 with_timeout 装饰器，避免测试中超时。

    用法：在测试函数参数加 disable_timeout
    """
    try:
        from services import closed_loop

        # 让 with_timeout 变成直接调用
        def _no_timeout_decorator(seconds):
            def decorator(func):
                def wrapper(*args, **kwargs):
                    return func(*args, **kwargs)

                return wrapper

            return decorator

        monkeypatch.setattr(closed_loop, "with_timeout", _no_timeout_decorator)
    except ImportError:
        pass
