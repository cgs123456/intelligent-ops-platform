"""Gunicorn 配置文件（P1-5）"""

import multiprocessing
import os

# P2-1: 标记当前运行在 gunicorn 下（master 进程加载本配置时设置，worker 经 fork 继承）。
# app.create_app() 据此跳过自定义 signal handler 注册，避免与 gunicorn 的 worker
# 生命周期管理冲突（preload_app 模式下 master 注册的信号会被 worker 继承）。
os.environ.setdefault("GUNICORN_SERVER", "1")

bind = "0.0.0.0:5000"
workers = int(os.getenv("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
worker_class = "sync"
timeout = 120
graceful_timeout = 30
max_requests = 1000
max_requests_jitter = 50
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOG_LEVEL", "info")
# P2-9: worker_tmp_dir 用内存文件系统（tmpfs）提升心跳文件读写性能，避免磁盘 I/O 抖动。
# 仅非 Windows 配置：Windows 无 /dev/shm，留空使用系统默认临时目录。
if os.name != "nt":
    worker_tmp_dir = "/dev/shm"
preload_app = True  # 预加载应用，减少 worker 内存占用
