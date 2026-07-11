# app/middleware/tenant_context.py
"""Tenant Context Middleware & Query Isolation."""
from __future__ import annotations

import hashlib
from typing import Any
from flask import Flask, g, request
from flask_login import current_user
from app.db import db
from app.models.tenant import get_tenant_by_id


class TenantQuery:
    """Wraps database query executions to enforce and validate tenant isolation filters.
    Prevents queries to tenant-scoped tables that lack proper tenant_id or {tenant_filter} constraints.
    """

    def __init__(self, tenant_id: int) -> None:
        self.tenant_id = tenant_id

    def _enforce_tenant_id(self, sql: str, params: tuple | list | dict) -> tuple[str, tuple | list | dict]:
        sql_lower = sql.lower()
        scoped_tables = {
            "orders", "order_call_attempts", "wallets", "wallet_transactions", "payment_intents",
            "customer_number_assignments", "customer_rates", "retry_rules", "api_keys", "webhook_logs",
            "sessions", "email_verifications", "users", "calls", "flows", "contacts", "tts_cache", "cdr_records"
        }

        # Check if the query targets any tenant-scoped tables
        has_scoped_table = False
        for table in scoped_tables:
            if f" {table} " in f" {sql_lower} " or f"({table}" in sql_lower or sql_lower.startswith(table):
                has_scoped_table = True
                break

        if has_scoped_table:
            # Prevent queries on scoped tables without explicit tenant filtering
            if "tenant_id" not in sql_lower and "{tenant_filter}" not in sql_lower:
                raise ValueError(
                    f"Security Violation: Query on tenant-scoped table lacks tenant isolation filter: {sql}"
                )

        # Enforce current tenant context
        from flask import g, has_app_context
        if has_app_context():
            g.tenant_id = self.tenant_id

        return sql, params

    def query(self, sql: str, params: tuple | list | dict = ()) -> list[dict[str, Any]]:
        """Run a query restricted to the current tenant."""
        sql, params = self._enforce_tenant_id(sql, params)
        return db.query(sql, params)

    def query_one(self, sql: str, params: tuple | list | dict = ()) -> dict[str, Any] | None:
        """Run a query returning a single row, restricted to the current tenant."""
        sql, params = self._enforce_tenant_id(sql, params)
        return db.query_one(sql, params)

    def execute(self, sql: str, params: tuple | list | dict = ()) -> None:
        """Execute a SQL statement restricted to the current tenant."""
        sql, params = self._enforce_tenant_id(sql, params)
        db.execute(sql, params)

    def insert_returning_id(self, sql: str, params: tuple | list | dict = ()) -> int:
        """Insert a row and return the generated ID, restricted to the current tenant."""
        sql, params = self._enforce_tenant_id(sql, params)
        return db.insert_returning_id(sql, params)


def get_tenant_db() -> TenantQuery:
    """Returns a TenantQuery instance scoped to the current request's tenant_id."""
    tenant_id = getattr(g, "tenant_id", None)
    if tenant_id is None:
        raise RuntimeError("No active tenant context found for this request.")
    return TenantQuery(tenant_id)


def register_tenant_context(app: Flask) -> None:
    """Register the single before_request hook that resolves tenant
    context for every request. This is the ONLY place X-API-Key gets
    hashed and looked up against api_keys — require_api_key (see
    app/blueprints/auth/decorators.py) reads g.tenant_id / g.api_key_error
    instead of re-querying, so each request does exactly one api_keys
    lookup and one tenants-status lookup, not two."""
    @app.before_request
    def _set_tenant_context():
        g.tenant_id = None
        g.tenant = None
        g.user_role = None
        g.current_user = current_user
        g.api_key_error = None

        # 1. Resolve from X-API-Key header (Public API requests)
        api_key = request.headers.get("X-API-Key")
        if api_key:
            key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            row = db.query_one(
                "SELECT tenant_id FROM api_keys WHERE key_hash = %s AND is_active = true",
                (key_hash,)
            )
            if not row:
                g.api_key_error = "UNAUTHORIZED"  # key missing/invalid/inactive
            else:
                tenant_id = int(row["tenant_id"])
                tenant = db.query_one("SELECT status FROM tenants WHERE id = %s", (tenant_id,))
                if not tenant or tenant["status"] not in ("active", "trial"):
                    g.api_key_error = "TENANT_INACTIVE"  # suspended-tenant check — happens here, once
                else:
                    g.tenant_id = tenant_id
                    db.execute("UPDATE api_keys SET last_used_at = now() WHERE key_hash = %s", (key_hash,))

        # 2. Resolve from Session (Web panel requests)
        elif current_user and current_user.is_authenticated:
            g.tenant_id = current_user.tenant_id
            g.user_role = current_user.role

        # If tenant context resolved, fetch and inject details
        if g.tenant_id:
            g.tenant = get_tenant_by_id(g.tenant_id)

        # 3. structlog context (request_id, tenant_id, user_id) — bound
        # once per request here, cleared in app/__init__.py's
        # teardown_request hook.
        import uuid
        import structlog
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        g.request_id = request_id
        user_id = current_user.get_id() if current_user and current_user.is_authenticated else None
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            tenant_id=g.tenant_id,
            request_id=request_id,
            user_id=user_id,
        )
