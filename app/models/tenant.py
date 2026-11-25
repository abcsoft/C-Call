# app/models/tenant.py
"""Tenant database models and helpers."""
from __future__ import annotations

from typing import Any
import json
import datetime
from app.db import db


def create_tenant(business_name: str | dict[str, Any], email: str | None = None, plan_name: str = "free", status: str = "trial") -> int:
    """Create a new tenant business account and initialize its wallet/retry rules.
    Supports both keyword arguments and a single dictionary parameter.
    """
    if isinstance(business_name, dict):
        email = business_name.get("email")
        plan_name = business_name.get("plan_name", "free")
        status = business_name.get("status", "trial")
        business_name = business_name["business_name"]

    # Find plan_id
    plan_row = db.query_one("SELECT id FROM plans WHERE name = %s", (plan_name,))
    plan_id = plan_row["id"] if plan_row else None

    # Insert tenant and default configurations in a transaction block
    with db.transaction() as conn:
        # Insert tenant
        cursor = conn.execute(
            """
            INSERT INTO tenants (business_name, email, plan_id, status, settings)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (business_name, email, plan_id, status, json.dumps({
                "tts_provider": "edge",
                "tts_voice": "bn-BD-NabanitaNeural",
                "dnd_start": "21:00",
                "dnd_end": "09:00",
                "dial_format": "local_bd"
            }))
        )
        tenant_id = cursor.fetchone()["id"]
        
        # Create wallet
        conn.execute(
            """
            INSERT INTO wallets (tenant_id, balance, currency, low_balance_threshold)
            VALUES (%s, 0.00, 'BDT', 50.00)
            """,
            (tenant_id,)
        )
        
        # Create default retry rules (attempt 1, 2, 3)
        default_rules = [
            (1, 15),
            (2, 30),
            (3, 60)
        ]
        for attempt_no, delay in default_rules:
            conn.execute(
                """
                INSERT INTO retry_rules (tenant_id, attempt_no, delay_minutes, retry_on, dnd_start, dnd_end, max_attempts, is_enabled)
                VALUES (%s, %s, %s, ARRAY['no_answer', 'busy'], '21:00', '09:00', 3, true)
                """,
                (tenant_id, attempt_no, delay)
            )

    return tenant_id


import logging
import uuid

logger = logging.getLogger("ccall")

class TenantJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return {"__datetime__": obj.isoformat()}
        if isinstance(obj, uuid.UUID):
            return {"__uuid__": str(obj)}
        return super().default(obj)

def tenant_json_decoder(dct):
    if "__datetime__" in dct:
        return datetime.datetime.fromisoformat(dct["__datetime__"])
    if "__uuid__" in dct:
        return uuid.UUID(dct["__uuid__"])
    return dct

def invalidate_tenant_caches(tenant_id: int) -> None:
    from app.extensions import redis_ext
    try:
        redis_ext.client.delete(f"tenant:settings:{tenant_id}")
        redis_ext.client.delete(f"tenant:rate:{tenant_id}")
        redis_ext.client.delete(f"tenant:stats:{tenant_id}")
        redis_ext.client.delete(f"wallet:{tenant_id}")
    except Exception as e:
        logger.error(f"Failed to invalidate caches for tenant {tenant_id}: {e}")

def get_tenant_by_id(tenant_id: int) -> dict[str, Any] | None:
    """Retrieve tenant details by internal ID, cached in Redis for 5 minutes."""
    from app.extensions import redis_ext
    
    cache_key = f"tenant:settings:{tenant_id}"
    try:
        cached = redis_ext.client.get(cache_key)
        if cached:
            val = cached.decode("utf-8") if isinstance(cached, bytes) else cached
            return json.loads(val, object_hook=tenant_json_decoder)
    except Exception as e:
        logger.error(f"Redis get tenant cache failed: {e}")

    tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
    if tenant:
        try:
            redis_ext.client.setex(
                cache_key,
                300,
                json.dumps(tenant, cls=TenantJSONEncoder)
            )
        except Exception as e:
            logger.error(f"Redis set tenant cache failed: {e}")
    return tenant


def get_tenant_by_uuid(tenant_uuid: str) -> dict[str, Any] | None:
    """Retrieve tenant details by UUID."""
    return db.query_one("SELECT * FROM tenants WHERE uuid = %s", (tenant_uuid,))


def get_tenant_by_email(email: str) -> dict[str, Any] | None:
    """Retrieve tenant details by email address."""
    return db.query_one("SELECT * FROM tenants WHERE email = %s", (email,))


def get_tenant_by_subdomain(subdomain: str) -> dict[str, Any] | None:
    """Retrieve tenant details by subdomain."""
    return db.query_one("SELECT * FROM tenants WHERE subdomain = %s", (subdomain,))


def update_tenant(tenant_id: int, data: dict[str, Any]) -> None:
    """Update tenant fields dynamically."""
    fields = []
    params = []
    for k, v in data.items():
        if k in ("business_name", "email", "subdomain", "plan_id", "status", "settings"):
            if k == "settings" and isinstance(v, dict):
                v = json.dumps(v)
            fields.append(f"{k} = %s")
            params.append(v)
    if fields:
        params.append(tenant_id)
        db.execute(f"UPDATE tenants SET {', '.join(fields)}, updated_at = now() WHERE id = %s", tuple(params))
        invalidate_tenant_caches(tenant_id)


def update_tenant_status(tenant_id: int, status: str) -> None:
    """Update tenant status directly."""
    db.execute("UPDATE tenants SET status = %s, updated_at = now() WHERE id = %s", (status, tenant_id))
    invalidate_tenant_caches(tenant_id)


def suspend_tenant(tenant_id: int) -> None:
    """Suspend a tenant account."""
    db.execute("UPDATE tenants SET status = 'suspended', updated_at = now() WHERE id = %s", (tenant_id,))
    invalidate_tenant_caches(tenant_id)


def update_tenant_settings(tenant_id: int, settings: dict[str, Any]) -> None:
    """Merge and update tenant configuration settings."""
    tenant = get_tenant_by_id(tenant_id)
    existing = tenant["settings"] if tenant and tenant.get("settings") else {}
    if isinstance(existing, str):
        try:
            existing = json.loads(existing)
        except Exception:
            existing = {}
    existing.update(settings)
    db.execute(
        "UPDATE tenants SET settings = %s, updated_at = now() WHERE id = %s",
        (json.dumps(existing), tenant_id)
    )
    invalidate_tenant_caches(tenant_id)


def get_all_tenants(
    page: int | None = None,
    per_page: int = 20,
    search: str | None = None,
    status: str | None = None,
    plan_id: int | str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Retrieve tenants, supporting both full list retrieval (backward-compatible) and paginated search."""
    if page is None:
        # Full list mode (backward-compatible)
        return db.query(
            "SELECT t.*, p.name as plan_name FROM tenants t LEFT JOIN plans p ON t.plan_id = p.id ORDER BY t.created_at DESC"
        )

    # Paginated filtered search mode
    sql = "SELECT t.*, p.name as plan_name FROM tenants t LEFT JOIN plans p ON t.plan_id = p.id"
    where_clauses: list[str] = []
    params: list[Any] = []

    if search and search.strip():
        where_clauses.append("(t.business_name ILIKE %s OR t.email ILIKE %s)")
        search_param = f"%{search.strip()}%"
        params.extend([search_param, search_param])

    if status == "archived":
        where_clauses.append("COALESCE(t.settings->>'archived_at', '') <> ''")
    else:
        if not include_archived:
            where_clauses.append("COALESCE(t.settings->>'archived_at', '') = ''")
        if status:
            where_clauses.append("t.status = %s")
            params.append(status)

    if plan_id not in (None, ""):
        where_clauses.append("t.plan_id = %s")
        params.append(int(plan_id))

    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)

    count_sql = f"SELECT COUNT(*) FROM ({sql}) as tmp"
    total_row = db.query_one(count_sql, tuple(params))
    total = int(total_row["count"]) if total_row else 0

    sql += " ORDER BY t.created_at DESC LIMIT %s OFFSET %s"
    offset = (page - 1) * per_page
    params.extend([per_page, offset])

    data = db.query(sql, tuple(params))
    return {
        "data": data,
        "total": total,
    }
