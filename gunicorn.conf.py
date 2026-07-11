# gunicorn.conf.py
"""Gunicorn WSGI server configuration."""

workers = 4
worker_class = 'sync'
worker_connections = 1000
timeout = 120
keepalive = 5
# preload_app=False is required because create_app() starts an
# APScheduler background thread. With preload=True the scheduler
# would start once in the master process before fork, causing
# duplicate/orphaned scheduler threads across workers.
preload_app = False
accesslog = '-'
errorlog = '-'
loglevel = 'info'
bind = '0.0.0.0:5000'
