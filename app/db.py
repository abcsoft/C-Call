# app/db.py
"""PostgreSQL query helpers (psycopg3 + psycopg-pool) for the multi-tenant SaaS application."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Generator
from flask import g, has_app_context
import psycopg
from psycopg_pool import ConnectionPool
from app.extensions import db_pool

logger = logging.getLogger("ccall")


def process_tenant_filter(sql: str, params: tuple | list | dict = ()) -> tuple[str, tuple | list | dict]:
    """Auto-inject tenant_id from g context if available and SQL contains {tenant_filter} placeholder.
    Supports both positional parameters (%s) and named placeholders (%(param)s).
    """
    if "{tenant_filter}" in sql:
        if has_app_context() and getattr(g, "tenant_id", None) is not None:
            if isinstance(params, dict):
                sql = sql.replace("{tenant_filter}", "tenant_id = %(tenant_id)s")
                params = {**params, "tenant_id": g.tenant_id}
            else:
                parts = sql.split("{tenant_filter}", 1)
                before_placeholder = parts[0]
                clean_before = before_placeholder.replace("%%", "")
                param_index = clean_before.count("%s")
                
                sql = sql.replace("{tenant_filter}", "tenant_id = %s")
                params_list = list(params)
                params_list.insert(param_index, g.tenant_id)
                params = tuple(params_list)
        else:
            # No tenant context: refuse silently expanding to 1=1 (would return all
            # tenants' data). Callers in scheduled jobs / background threads must either
            # set g.tenant_id explicitly inside an app_context, or rewrite the query to
            # use a concrete tenant_id parameter instead of {tenant_filter}.
            raise RuntimeError(
                "{tenant_filter} used outside an active tenant context — "
                "g.tenant_id is None. Pass tenant_id explicitly in the query "
                "or ensure g.tenant_id is set via app.app_context() before calling."
            )
    return sql, params


class Database:
    """Flask extension wrapping the global psycopg-pool ConnectionPool."""

    def init_app(self, app) -> None:
        pass

    def get_pool(self) -> ConnectionPool:
        """Returns psycopg-pool ConnectionPool."""
        return db_pool.pool

    def query(self, sql: str, params: tuple | list | dict = ()) -> list[dict[str, Any]]:
        """Execute a query and return all rows as dicts."""
        sql, params = process_tenant_filter(sql, params)
        with self.get_pool().connection() as conn:
            return conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | list | dict = ()) -> dict[str, Any] | None:
        """Execute a query and return the first row as a dict, or None."""
        sql, params = process_tenant_filter(sql, params)
        with self.get_pool().connection() as conn:
            return conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | list | dict = ()) -> None:
        """Execute a SQL statement."""
        sql, params = process_tenant_filter(sql, params)
        with self.get_pool().connection() as conn:
            conn.execute(sql, params)

    def insert_returning_id(self, sql: str, params: tuple | list | dict = ()) -> int:
        """Execute insert query and return the generated ID."""
        sql, params = process_tenant_filter(sql, params)
        tr = sql.rstrip().rstrip(";")
        if "returning id" not in tr.lower():
            tr += " RETURNING id"
        with self.get_pool().connection() as conn:
            row = conn.execute(tr, params).fetchone()
            if not row:
                raise ValueError("Insert statement did not return an ID.")
            return int(row["id"])

    @contextmanager
    def transaction(self) -> Generator[psycopg.Connection, None, None]:
        """Yield a connection inside a transaction block for atomic operations.
        Commits on clean exit, rolls back on exception.
        """
        with self.get_pool().connection() as conn:
            with conn.transaction():
                yield conn

    def get_setting(self, key: str, default: str = "") -> str:
        import os
        row = self.query_one("SELECT value FROM app_settings WHERE key = %s", (key,))
        return row["value"] if row else os.environ.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, value),
        )


db = Database()
