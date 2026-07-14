"""
Gunicorn production configuration — C-Call IVR SaaS.

Worker formula:
  workers = 2 * CPU_CORES + 1  (sync/gthread blocking workload)
  Typical 2-vCPU VPS → 4 workers with 2 threads each = 8 concurrent requests
  SIP call execution is in a separate call_worker service — web workers
  never block on SIP.

preload_app MUST remain False because APScheduler is no longer started
inside create_app(). If scheduler is ever re-added to create_app() in
future, preload=True would fork a running scheduler into child processes
causing duplicate job execution. Leave False as a hard safety rail.
"""
import os
import multiprocessing

# --- Binding -----------------------------------------------------------------
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:5000")

# --- Workers -----------------------------------------------------------------
_cpu = multiprocessing.cpu_count()
workers = int(os.environ.get("WEB_WORKERS", min(2 * _cpu + 1, 8)))
threads = int(os.environ.get("WEB_THREADS", "2"))
worker_class = "gthread"   # thread-aware; compatible with psycopg3 + redis-py

# --- Timeouts ----------------------------------------------------------------
timeout = 60                # hard kill after 60s (webhooks < 30s, API < 10s)
graceful_timeout = 30       # SIGTERM → SIGKILL window
keepalive = 5               # HTTP/1.1 keepalive seconds

# --- Request limits ----------------------------------------------------------
max_requests = 1000         # recycle workers to guard memory leaks
max_requests_jitter = 200   # stagger recycling so not all die at once
limit_request_line = 8190   # guard against large-URL attacks
limit_request_fields = 100
limit_request_field_size = 8190

# --- Preload -----------------------------------------------------------------
# DO NOT enable — see module docstring.
preload_app = False

# --- Logging -----------------------------------------------------------------
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(f)s %(a)s %(D)sμs'

# --- Security ----------------------------------------------------------------
forwarded_allow_ips = os.environ.get("TRUSTED_PROXIES", "127.0.0.1")

# --- Temp dir (writable even with read-only root FS) -------------------------
worker_tmp_dir = "/dev/shm"   # Linux only; falls back silently on other OS


def on_starting(server):
    server.log.info("Gunicorn starting: %d workers × %d threads", workers, threads)


def post_fork(server, worker):
    server.log.info("Worker forked: pid=%d", worker.pid)


def worker_exit(server, worker):
    server.log.warning("Worker exited: pid=%d", worker.pid)
