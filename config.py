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
    db_path = BASE_DIR / 'ops_platform.db'
    SQLALCHEMY_DATABASE_URI = os.getenv(
        'DATABASE_URL',
        f'sqlite:///{db_path}'
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,       # 连接前检查，避免用断连
        'pool_recycle': 1800,         # 30 分钟回收连接（比 3600 更积极）
        'pool_size': 10,              # 连接池大小
        'max_overflow': 20,           # 最大溢出
        'pool_timeout': 30,           # 获取连接超时 30 秒
    }

    # Flask
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    JSON_AS_ASCII = False  # 中文不转义

    # AIGC：LLM 配置（可选，不配则用规则引擎）
    LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'rule')  # rule / glm / qwen
    LLM_API_KEY = os.getenv('LLM_API_KEY', '')
    LLM_API_URL = os.getenv('LLM_API_URL', '')
    LLM_MODEL = os.getenv('LLM_MODEL', 'glm-4-flash')

    # RPA：调度配置
    RPA_SCHEDULE_ENABLED = os.getenv('RPA_SCHEDULE_ENABLED', '0') == '1'
    RPA_QUOTE_CRON = os.getenv('RPA_QUOTE_CRON', '0 9 * * *')       # 每日9点采集报价
    RPA_SYNC_CRON = os.getenv('RPA_SYNC_CRON', '*/30 * * * *')      # 每30分钟同步订单
    RPA_MAX_RETRY = int(os.getenv('RPA_MAX_RETRY', '3'))
    RPA_RETRY_BACKOFF = float(os.getenv('RPA_RETRY_BACKOFF', '2.0'))  # 指数退避基数

    # FDE：ETL 配置
    FDE_INCREMENTAL = os.getenv('FDE_INCREMENTAL', '1') == '1'  # 增量拉取开关
    FDE_DQ_STRICT = os.getenv('FDE_DQ_STRICT', '1') == '1'      # 数据质量强校验

    # 闭环
    LOOP_TIMEOUT = int(os.getenv('LOOP_TIMEOUT', '60'))  # 单步超时秒数
    LOOP_AUTO_TRIGGER = os.getenv('LOOP_AUTO_TRIGGER', '0') == '1'  # 低库存自动触发

    # 缓存（可选 Redis，不配则用内存）
    CACHE_TYPE = os.getenv('CACHE_TYPE', 'simple')  # simple / redis
    CACHE_REDIS_URL = os.getenv('CACHE_REDIS_URL', '')
    CACHE_DEFAULT_TIMEOUT = int(os.getenv('CACHE_DEFAULT_TIMEOUT', '3600'))

    # RBAC
    RBAC_ENABLED = os.getenv('RBAC_ENABLED', '0') == '1'  # Demo 默认关闭

    # CORS 跨域配置（生产环境: https://ops.example.com,https://admin.example.com）
    CORS_ORIGINS = os.getenv('CORS_ORIGINS', '*')

    # API 限流配置（生产用 redis://redis:6379/1）
    # 注意：Flask-Limiter 读取 RATELIMIT_STORAGE_URI（不是 URL），这里同时设两份以兼容老 env
    RATELIMIT_STORAGE_URI = os.getenv('RATELIMIT_STORAGE_URL', 'memory://')
    RATELIMIT_STORAGE_URL = RATELIMIT_STORAGE_URI  # 兼容字段
    RATELIMIT_DEFAULT = '200/hour'           # 全局默认：每小时200次
    RATELIMIT_AUTH = '10/minute'             # 登录接口：每分钟10次

    # 监控指标（Prometheus）
    METRICS_ENABLED = os.getenv('METRICS_ENABLED', '0') == '1'

    # API 文档（flask-smorest，P2-9）
    API_DOCS_ENABLED = os.getenv('API_DOCS_ENABLED', '0') == '1'


class DevConfig(Config):
    DEBUG = True
    ENV = 'development'


class ProdConfig(Config):
    DEBUG = False
    ENV = 'production'
    RBAC_ENABLED = True  # 生产环境强制开启 RBAC

    # HTTPS 安全配置
    SESSION_COOKIE_SECURE = True      # 仅 HTTPS 传输
    SESSION_COOKIE_HTTPONLY = True    # JS 不可读
    SESSION_COOKIE_SAMESITE = 'Lax'   # 防 CSRF
    PERMANENT_SESSION_LIFETIME = 3600  # Session 有效期 1 小时

    def __init__(self):
        super().__init__()
        # P0-1: 生产环境必须设置 SECRET_KEY
        if not os.getenv('SECRET_KEY'):
            raise RuntimeError(
                '生产环境必须设置 SECRET_KEY 环境变量！'
                '生成方式: python -c "import secrets; print(secrets.token_hex(32))"'
            )
        self.SECRET_KEY = os.getenv('SECRET_KEY')

        # P0-6: 生产环境必须使用 PostgreSQL/MySQL
        db_url = os.getenv('DATABASE_URL')
        if not db_url or db_url.startswith('sqlite'):
            raise RuntimeError(
                '生产环境必须设置 DATABASE_URL 环境变量并使用 PostgreSQL/MySQL，'
                '示例: postgresql://user:pass@host:5432/dbname'
            )
        self.SQLALCHEMY_DATABASE_URI = db_url


# 按环境变量选择
_config_map = {'development': DevConfig, 'production': ProdConfig}
config = _config_map.get(os.getenv('FLASK_ENV', 'development'), DevConfig)
