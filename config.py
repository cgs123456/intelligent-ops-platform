"""
配置管理
通过环境变量切换开发/生产环境，敏感配置不入库。
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Config:
    """基础配置"""

    # 数据库：默认 SQLite，生产环境用 DATABASE_URL 切 PostgreSQL
    db_path = BASE_DIR / "ops_platform.db"
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{db_path}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,  # 连接前检查，避免用断连
        "pool_recycle": 1800,  # 30 分钟回收连接（比 3600 更积极）
        "pool_size": 10,  # 连接池大小
        "max_overflow": 20,  # 最大溢出
        "pool_timeout": 30,  # 获取连接超时 30 秒
    }

    # Flask
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
    # P1-10: JWT 签名算法，默认 HS256（对称密钥）。
    # 生产环境应迁移到 RS256（非对称密钥）：设置 JWT_ALGORITHM=RS256，
    # 并配置 RSA 公私钥对（私钥签名，公钥验签），实现密钥轮换与安全分发。
    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
    JSON_AS_ASCII = False  # 中文不转义

    # AIGC：LLM 配置（可选，不配则用规则引擎）
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "rule")  # rule / glm / qwen
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_API_URL = os.getenv("LLM_API_URL", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "glm-4-flash")

    # RPA：调度配置
    RPA_SCHEDULE_ENABLED = os.getenv("RPA_SCHEDULE_ENABLED", "0") == "1"
    RPA_QUOTE_CRON = os.getenv("RPA_QUOTE_CRON", "0 9 * * *")  # 每日9点采集报价
    RPA_SYNC_CRON = os.getenv("RPA_SYNC_CRON", "*/30 * * * *")  # 每30分钟同步订单
    RPA_MAX_RETRY = int(os.getenv("RPA_MAX_RETRY", "3"))
    RPA_RETRY_BACKOFF = float(os.getenv("RPA_RETRY_BACKOFF", "2.0"))  # 指数退避基数

    # FDE：ETL 配置
    FDE_INCREMENTAL = os.getenv("FDE_INCREMENTAL", "1") == "1"  # 增量拉取开关
    FDE_DQ_STRICT = os.getenv("FDE_DQ_STRICT", "1") == "1"  # 数据质量强校验

    # 闭环
    LOOP_TIMEOUT = int(os.getenv("LOOP_TIMEOUT", "60"))  # 单步超时秒数
    LOOP_AUTO_TRIGGER = os.getenv("LOOP_AUTO_TRIGGER", "0") == "1"  # 低库存自动触发

    # 缓存（可选 Redis，不配则用内存）
    CACHE_TYPE = os.getenv("CACHE_TYPE", "simple")  # simple / redis
    CACHE_REDIS_URL = os.getenv("CACHE_REDIS_URL", "")
    CACHE_DEFAULT_TIMEOUT = int(os.getenv("CACHE_DEFAULT_TIMEOUT", "3600"))

    # RBAC（P0-7: 默认开启，dev 环境可通过 RBAC_ENABLED=0 关闭）
    RBAC_ENABLED = os.getenv("RBAC_ENABLED", "1") == "1"

    # CORS 跨域配置（生产环境: https://ops.example.com,https://admin.example.com）
    # P1-4: 默认不再为 "*"，改为本地开发常用源，DevConfig/ProdConfig 均校验不为 "*"
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5000")

    # API 限流配置（生产用 redis://redis:6379/1）
    # 注意：Flask-Limiter 读取 RATELIMIT_STORAGE_URI（不是 URL），这里同时设两份以兼容老 env
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URL", "memory://")
    RATELIMIT_STORAGE_URL = RATELIMIT_STORAGE_URI  # 兼容字段
    RATELIMIT_DEFAULT = "200/hour"  # 全局默认：每小时200次
    RATELIMIT_AUTH = "10/minute"  # 登录接口：每分钟10次

    # 监控指标（Prometheus）
    METRICS_ENABLED = os.getenv("METRICS_ENABLED", "0") == "1"

    # API 文档（flask-smorest，P0-3: 默认开启）
    API_DOCS_ENABLED = os.getenv("API_DOCS_ENABLED", "1") == "1"

    # P2-4: Agent 评测框架路径配置（可由环境变量覆盖，避免硬编码）
    # gold SQL 用例文件路径（默认 tests/fixtures/gold_sql.json）
    EVALUATOR_GOLD_CASES_FILE = os.getenv(
        "EVALUATOR_GOLD_CASES_FILE", str(BASE_DIR / "tests" / "fixtures" / "gold_sql.json")
    )
    # 评测报告保存目录（默认 instance/eval_reports/，Flask instance 目录）
    EVALUATOR_REPORTS_DIR = os.getenv("EVALUATOR_REPORTS_DIR", str(BASE_DIR / "instance" / "eval_reports"))


class DevConfig(Config):
    DEBUG = True
    ENV = "development"
    # P2-14: 读写分离从库 URL（dev 默认空，降级主库）+ L1 缓存容量
    DATABASE_REPLICA_URL = os.getenv("DATABASE_REPLICA_URL", "")
    CACHE_L1_MAXSIZE = int(os.getenv("CACHE_L1_MAXSIZE", "1024"))

    def __init__(self):
        super().__init__()
        # P1-5: SECRET_KEY 仍为默认值时自动生成随机密钥并记录 warning
        if self.SECRET_KEY == "dev-secret-key-change-in-production":
            import logging
            import secrets

            self.SECRET_KEY = secrets.token_hex(32)
            logging.getLogger(__name__).warning(
                "DevConfig SECRET_KEY 仍为默认值，已自动生成随机密钥。"
                "生产环境请通过 SECRET_KEY 环境变量显式指定。"
            )
        # P1-4: DevConfig 也校验 CORS_ORIGINS 不为 "*"，避免开发环境误开放跨域
        if self.CORS_ORIGINS == "*":
            import logging

            logging.getLogger(__name__).warning(
                'DevConfig CORS_ORIGINS 为 "*"，已降级为 localhost 本地源。'
                "请通过 CORS_ORIGINS 环境变量显式指定允许的源。"
            )
            self.CORS_ORIGINS = "http://localhost:3000,http://localhost:5000"


class ProdConfig(Config):
    DEBUG = False
    ENV = "production"
    RBAC_ENABLED = True  # 生产环境强制开启 RBAC

    # HTTPS 安全配置
    SESSION_COOKIE_SECURE = True  # 仅 HTTPS 传输
    SESSION_COOKIE_HTTPONLY = True  # JS 不可读
    SESSION_COOKIE_SAMESITE = "Lax"  # 防 CSRF
    PERMANENT_SESSION_LIFETIME = 3600  # Session 有效期 1 小时

    # P2-14: 读写分离从库 URL（未配置则降级到主库）+ L1 缓存容量
    DATABASE_REPLICA_URL = os.getenv("DATABASE_REPLICA_URL", "")
    CACHE_L1_MAXSIZE = int(os.getenv("CACHE_L1_MAXSIZE", "1024"))

    def __init__(self):
        super().__init__()
        # P2-4: 生产环境不允许 TESTING=True，防止测试模式跳过认证/RBAC 误投产
        if os.getenv("TESTING", "0") == "1":
            raise RuntimeError("生产环境不允许 TESTING=True")
        # P0-1: 生产环境必须设置 SECRET_KEY
        if not os.getenv("SECRET_KEY"):
            raise RuntimeError(
                "生产环境必须设置 SECRET_KEY 环境变量！"
                '生成方式: python -c "import secrets; print(secrets.token_hex(32))"'
            )
        # P1-11: SECRET_KEY 强度校验 —— 至少 32 字符，防止弱密钥
        if len(os.getenv("SECRET_KEY", "")) < 32:
            raise RuntimeError("SECRET_KEY 至少 32 字符，请使用强随机密钥")
        self.SECRET_KEY = os.getenv("SECRET_KEY")

        # P0-6: 生产环境必须使用 PostgreSQL/MySQL
        db_url = os.getenv("DATABASE_URL")
        if not db_url or db_url.startswith("sqlite"):
            raise RuntimeError(
                "生产环境必须设置 DATABASE_URL 环境变量并使用 PostgreSQL/MySQL，"
                "示例: postgresql://user:pass@host:5432/dbname"
            )
        self.SQLALCHEMY_DATABASE_URI = db_url

        # P1-8: 生产环境 Celery broker 不可用 memory://（用 raise 而非 assert，避免 python -O 失效）
        celery_broker = os.getenv("CELERY_BROKER_URL", "memory://")
        if celery_broker == "memory://":
            raise RuntimeError("生产环境必须配置 CELERY_BROKER_URL（不可用 memory://）")
        self.CELERY_BROKER_URL = celery_broker

        # P1-9: 生产环境限流存储不可用 memory://
        if self.RATELIMIT_STORAGE_URI == "memory://":
            raise RuntimeError("生产环境必须配置 RATELIMIT_STORAGE_URL（不可用 memory://）")

        # P2-8: 生产环境 CORS 不可为 *
        if self.CORS_ORIGINS == "*":
            raise RuntimeError("生产环境必须指定 CORS_ORIGINS（不可为 *）")

        # P0-1: 生产环境必须设置 FIELD_ENCRYPTION_KEY（字段级加密密钥，不可降级用 SECRET_KEY 派生）
        if not os.getenv("FIELD_ENCRYPTION_KEY"):
            raise RuntimeError(
                "生产环境必须设置 FIELD_ENCRYPTION_KEY 环境变量！"
                '生成方式: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )


# 按环境变量选择
_config_map = {"development": DevConfig, "production": ProdConfig}
# P1-4/P1-5: 实例化配置类以触发 __init__ 中的安全校验（CORS_ORIGINS / SECRET_KEY 等）
config = _config_map.get(os.getenv("FLASK_ENV", "development"), DevConfig)()
