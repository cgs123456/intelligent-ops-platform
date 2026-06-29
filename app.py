"""
Flask 主应用 · 中型企业智能运营平台（重构版）
启动：python app.py → http://127.0.0.1:5000
"""
import os
import signal
import atexit
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, jsonify
from config import config
from extensions import db, cache, migrate, limiter

logger = logging.getLogger(__name__)


def setup_logging(app):
    """P1-3: 配置日志：stdout + 文件轮转 + request_id 贯穿（便于 ELK 采集）"""
    log_level = logging.INFO if app.config.get('ENV') == 'production' else logging.DEBUG
    log_dir = os.path.join(app.instance_path, 'logs')
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        pass

    # P0-4: 日志格式带 request_id，便于全链路排查
    fmt = '%(asctime)s [%(levelname)s] %(name)s [req=%(request_id)s] [%(filename)s:%(lineno)d]: %(message)s'

    handlers = [logging.StreamHandler()]  # stdout

    # 文件轮转：每个文件最大 10MB，保留 10 个
    try:
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, 'app.log'),
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
            encoding='utf-8',
        )
        file_handler.setFormatter(logging.Formatter(fmt))
        handlers.append(file_handler)
    except Exception as e:
        logger.warning(f'文件日志初始化失败：{e}')

    logging.basicConfig(level=log_level, format=fmt, handlers=handlers)
    # P0-4: 给所有 handler 注入 request_id 过滤器
    from middleware import configure_request_id_logging
    configure_request_id_logging()


def create_app(config_class=None):
    """构建 Flask app。
    config_class: 可选，传入自定义配置类（测试用）。不传则按 FLASK_ENV 选择。
    """
    app = Flask(__name__)
    # 关键：必须在初始化扩展前应用配置，否则 seed / RBAC / 限流等依赖 config 的逻辑会读到默认值
    app.config.from_object(config_class if config_class is not None else config)

    # P0: ProxyFix — 反向代理后还原真实客户端 IP，让限流按真实 IP 生效
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # P1-3: 日志配置
    setup_logging(app)

    # P0-4: request_id 中间件（必须在 logging 之后，让 before_request 能写带 req_id 的日志）
    from middleware import init_request_id
    init_request_id(app)

    # 初始化扩展
    db.init_app(app)
    migrate.init_app(app, db)  # P2-1: Flask-Migrate

    # 缓存：未配 Redis 用 simple
    cache_config = {'CACHE_TYPE': app.config['CACHE_TYPE']}
    if app.config['CACHE_TYPE'] == 'redis' and app.config['CACHE_REDIS_URL']:
        cache_config['CACHE_REDIS_URL'] = app.config['CACHE_REDIS_URL']
    cache.init_app(app, config=cache_config)

    # P1-1: API 限流
    limiter.init_app(app)

    # P0-3: Celery 配置（生产用 Redis broker）
    try:
        from extensions import celery_app as _celery
        if _celery is not None:
            broker_url = app.config.get('CELERY_BROKER_URL', 'memory://')
            result_backend = app.config.get('CELERY_RESULT_BACKEND', 'cache+memory://')
            _celery.conf.update(
                broker_url=broker_url,
                result_backend=result_backend,
            )
            # 让 Celery 任务在 Flask app context 内执行
            class ContextTask(_celery.Task):
                def __call__(self, *args, **kwargs):
                    with app.app_context():
                        return self.run(*args, **kwargs)
            _celery.Task = ContextTask
            logger.info('Celery 已配置 broker=%s', broker_url)
    except ImportError:
        logger.warning('celery 未安装，跳过异步任务配置')
    except Exception as e:
        logger.warning('Celery 配置失败：%s', e)

    # P0-4: CORS 配置
    from flask_cors import CORS
    cors_origins = app.config.get('CORS_ORIGINS', '*')
    if cors_origins == '*':
        CORS(app, supports_credentials=False)
    else:
        origins = [o.strip() for o in cors_origins.split(',')]
        CORS(app, origins=origins, supports_credentials=True)

    # P1-4: Prometheus 监控指标
    if app.config.get('METRICS_ENABLED'):
        try:
            from prometheus_flask_exporter import PrometheusMetrics
            metrics = PrometheusMetrics(app, path='/metrics')
            metrics.info('app_info', 'Application info', version='1.0.0', env=app.config['ENV'])
        except ImportError:
            logger.warning('prometheus-flask-exporter 未安装，跳过指标采集')

    # 注册 Blueprint + 兼容旧路由
    from routes import register_blueprints
    register_blueprints(app)

    # P2-9: API 文档（flask-smorest，可选）
    if app.config.get('API_DOCS_ENABLED'):
        try:
            from flask_smorest import Api
            app.config['API_TITLE'] = '智能运营平台 API'
            app.config['API_VERSION'] = 'v1'
            app.config['OPENAPI_VERSION'] = '3.0.3'
            app.config['OPENAPI_URL_PREFIX'] = '/docs'
            app.config['OPENAPI_SWAGGER_UI_PATH'] = '/swagger'
            app.config['OPENAPI_SWAGGER_UI_URL'] = 'https://cdn.jsdelivr.net/npm/swagger-ui-dist/'
            api = Api(app)
            logger.info('API 文档已启用: /docs/swagger')
        except ImportError:
            logger.warning('flask-smorest 未安装，跳过 API 文档')

    # 首页
    @app.route('/')
    def index():
        return render_template('index.html')

    # P1-2: 深度健康检查
    @app.route('/health')
    def health():
        """深度健康检查：检查数据库 + Redis 连通性。不暴露内部异常详情，仅返回 ok/fail。"""
        checks = {'status': 'ok'}
        # 数据库检查
        try:
            db.session.execute(db.text('SELECT 1'))
            checks['database'] = 'ok'
        except Exception:
            checks['database'] = 'fail'
            checks['status'] = 'degraded'
            logger.warning('健康检查：数据库连接失败', exc_info=True)

        # Redis 检查（仅 Redis 模式）
        if app.config.get('CACHE_TYPE') == 'redis':
            try:
                cache.set('_health_check', '1', timeout=5)
                assert cache.get('_health_check') == b'1' or cache.get('_health_check') == '1'
                checks['redis'] = 'ok'
            except Exception:
                checks['redis'] = 'fail'
                checks['status'] = 'degraded'
                logger.warning('健康检查：Redis 连接失败', exc_info=True)

        # 磁盘空间检查
        try:
            import shutil
            total, used, free = shutil.disk_usage('/')
            free_pct = free / total * 100
            checks['disk_free_pct'] = round(free_pct, 1)
            if free_pct < 10:
                checks['status'] = 'degraded'
                checks['disk'] = f'warning: only {free_pct:.1f}% free'
        except Exception:
            pass

        code = 200 if checks['status'] == 'ok' else 503
        return jsonify(checks), code

    @app.route('/health/live')
    def health_live():
        """存活探针：轻量级，不检查依赖"""
        return jsonify({'status': 'ok'}), 200

    @app.route('/health/ready')
    def health_ready():
        """就绪探针：检查数据库 + Redis 连通性。不暴露异常详情。"""
        try:
            db.session.execute(db.text('SELECT 1'))
        except Exception:
            logger.warning('就绪检查：数据库不可用', exc_info=True)
            return jsonify({'status': 'not_ready', 'database': 'fail'}), 503
        # 同时检查 Redis（生产依赖缓存）
        if app.config.get('CACHE_TYPE') == 'redis':
            try:
                cache.set('_hc', '1', timeout=5)
                assert cache.get('_hc') in (b'1', '1')
            except Exception:
                logger.warning('就绪检查：Redis 不可用', exc_info=True)
                return jsonify({'status': 'not_ready', 'redis': 'fail'}), 503
        return jsonify({'status': 'ready'}), 200

    # P1-10: 统一错误处理（不暴露内部异常详情）
    @app.errorhandler(400)
    def bad_request(e):
        # 仅返回通用消息，不返回 str(e) 防止泄漏内部字段
        # 业务层如需带具体错误信息，应自行 jsonify({'error': 'xxx'}), 400
        return jsonify({'error': '请求参数错误'}), 400

    @app.errorhandler(401)
    def unauthorized(e):
        return jsonify({'error': '未认证，请先登录'}), 401

    @app.errorhandler(403)
    def forbidden(e):
        return jsonify({'error': '无权限访问'}), 403

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'error': '接口不存在'}), 404

    @app.errorhandler(429)
    def rate_limited(e):
        return jsonify({'error': '请求过于频繁，请稍后再试'}), 429

    @app.errorhandler(500)
    def server_error(e):
        logger.exception('服务器内部错误: %s', e)
        return jsonify({'error': '服务器内部错误'}), 500

    @app.errorhandler(Exception)
    def handle_exception(e):
        logger.exception('未处理异常: %s', e)
        return jsonify({'error': '服务器内部错误'}), 500

    # 初始化数据库 + 种子数据
    with app.app_context():
        db.create_all()

        # RBAC 初始化（所有环境都执行，幂等）
        try:
            from services.rbac import init_rbac_data
            if app.config.get('RBAC_ENABLED'):
                init_rbac_data(app)
        except Exception as e:
            logger.warning(f'RBAC 初始化跳过：{e}')

        # P0-5: 种子数据仅开发环境执行
        if app.config.get('ENV') == 'development' and not app.config.get('TESTING'):
            from seed import seed_all
            seeded = seed_all(app)
            logger.info(f'开发环境种子数据{"已加载" if seeded else "已存在"}')
        else:
            logger.info('非开发环境跳过种子数据')

    # P1-7: 优雅关闭
    def graceful_shutdown(signum, frame):
        logger.info('收到关闭信号 %s，开始优雅关闭...', signum)
        try:
            # 检查是否有可用的 app context（atexit 触发时可能已无 context）
            from flask import current_app
            if not current_app:
                return
            db.session.remove()
            logger.info('数据库连接已关闭')
        except Exception:
            # 进程退出时无 app context 属正常情况，静默处理
            pass
        logger.info('优雅关闭完成')

    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    atexit.register(graceful_shutdown, None, None)

    return app


app = create_app()

if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug, port=5000, threaded=True)
