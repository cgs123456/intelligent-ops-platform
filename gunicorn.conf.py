"""Gunicorn 配置文件（P1-5）"""
import multiprocessing
import os

bind = '0.0.0.0:5000'
workers = int(os.getenv('GUNICORN_WORKERS', multiprocessing.cpu_count() * 2 + 1))
worker_class = 'sync'
timeout = 120
graceful_timeout = 30
max_requests = 1000
max_requests_jitter = 50
keepalive = 5
accesslog = '-'
errorlog = '-'
loglevel = os.getenv('GUNICORN_LOG_LEVEL', 'info')
worker_tmp_dir = '/dev/shm'
preload_app = True  # 预加载应用，减少 worker 内存占用
