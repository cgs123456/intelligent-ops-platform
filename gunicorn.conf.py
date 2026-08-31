"""Gunicorn 配置文件（P1-5）"""

import multiprocessing
import os

# P2-1: 标记当前运行在 gunicorn 下（master 进程加载本配置时设置，worker 经 fork 继承）。
# app.create_app() 据此跳过自定义 signal handler 注册，避免与 gunicorn 的 worker
# 生命周期管理冲突（preload_app 模式下 master 注册的信号会被 worker 继承）。
os.environ.setdefault("GUNICORN_SERVER", "1")

bind = "0.0.0.0:5000"
workers = int(os.getenv("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1))
# P0-5: sync worker 下每个 SSE 连接 / 慢 LLM 调用独占一个 worker，几个并发请求即可
# 耗尽全部 worker 使整站不可用；且 loop/stream 最长 120s 恰好撞上 timeout=120 被 master 强杀。
# gthread 主线程负责心跳，worker 不会因长请求被杀，线程数可按 IO 并发放大。
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
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
# P0-5: 关闭 preload_app。master 进程 import app 时会执行模块级 create_app()，
# 建立 SQLAlchemy 连接池并访问数据库，fork 后多个 worker 共享同一批 TCP 连接，
# 导致协议错乱/随机 500。改为各 worker 自行初始化（内存开销换取正确性）。
preload_app = False
