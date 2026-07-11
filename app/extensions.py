# app/extensions.py
"""Unbound Flask extension instances — created here, wired to the app
inside create_app() via .init_app(app).
"""
from __future__ import annotations

import redis as redis_lib
from flask_apscheduler import APScheduler
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_mail import Mail
from flask_session import Session
from flask_babel import Babel
from flask_wtf.csrf import CSRFProtect
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool



class DatabasePool:
    """Lazy init wrapper for psycopg-pool ConnectionPool (min=2, max=20)."""

    def __init__(self) -> None:
        self._pool: ConnectionPool | None = None

    def init_app(self, app) -> None:
        database_url = app.config["DATABASE_URL"]
        # PostgreSQL Mode is required for connection pooling
        self._pool = ConnectionPool(
            database_url,
            min_size=2,
            max_size=20,
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
        )
        app.extensions["db_pool"] = self

    @property
    def pool(self) -> ConnectionPool:
        if self._pool is None:
            raise RuntimeError("Database Pool not initialised — call db_pool.init_app(app) first.")
        return self._pool


db_pool = DatabasePool()

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "error"

scheduler = APScheduler()

limiter = Limiter(key_func=get_remote_address)

mail = Mail()

server_session = Session()

babel = Babel()
csrf = CSRFProtect()



class RedisClient:
    """Thin init_app wrapper for Redis client extension."""

    def __init__(self) -> None:
        self._client: redis_lib.Redis | None = None

    def init_app(self, app) -> None:
        self._client = redis_lib.Redis.from_url(app.config["REDIS_URL"])
        app.extensions["redis"] = self
        app.config["SESSION_REDIS"] = self._client

    @property
    def client(self) -> redis_lib.Redis:
        if self._client is None:
            raise RuntimeError("Redis extension not initialised — call redis_ext.init_app(app) first.")
        return self._client


redis_ext = RedisClient()
