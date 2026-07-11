from __future__ import annotations

from typing import Any
import datetime
import json
from decimal import Decimal
from psycopg.types.json import Jsonb
from app.db import db

def create_order(
    tenant_id: int,
    external_order_id: str,
    phone: str,
    customer_name: str | None,
    amount: Decimal | None,
    webhook_url: str | None,
    max_attempts: int = 3,
    metadata: dict | None = None,
    custom_text: str | None = None,
    custom_audio_url: str | None = None
) -> int:
    metadata_val = Jsonb(metadata or {})
    return db.insert_returning_id(
        """
        INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, webhook_url, max_attempts,
                            metadata, custom_text, custom_audio_url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, external_order_id) DO UPDATE 
        SET phone = EXCLUDED.phone, customer_name = EXCLUDED.customer_name, 
            amount = EXCLUDED.amount, webhook_url = EXCLUDED.webhook_url,
            metadata = EXCLUDED.metadata, custom_text = EXCLUDED.custom_text,
            custom_audio_url = EXCLUDED.custom_audio_url,
            updated_at = now()
        RETURNING id
        """,
        (tenant_id, external_order_id, phone, customer_name, amount, webhook_url, max_attempts,
         metadata_val, custom_text, custom_audio_url)
    )

def get_order_by_id(tenant_id: int, order_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM orders WHERE tenant_id = %s AND id = %s", (tenant_id, order_id))

def get_order_by_id_system(order_id: int) -> dict[str, Any] | None:
    """System-level access (e.g. background threads) without tenant context."""
    return db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))

def get_order_by_external_id(tenant_id: int, external_order_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM orders WHERE tenant_id = %s AND external_order_id = %s", (tenant_id, external_order_id))

def update_order_status(tenant_id: int, order_id: int, status: str) -> None:
    db.execute(
        "UPDATE orders SET order_status = %s, updated_at = now() WHERE tenant_id = %s AND id = %s",
        (status, tenant_id, order_id)
    )

def update_order_status_system(order_id: int, status: str) -> None:
    db.execute(
        "UPDATE orders SET order_status = %s, updated_at = now() WHERE id = %s",
        (status, order_id)
    )

def increment_order_attempts(tenant_id: int, order_id: int) -> None:
    db.execute(
        "UPDATE orders SET attempt_count = attempt_count + 1, updated_at = now() WHERE tenant_id = %s AND id = %s",
        (tenant_id, order_id)
    )

def set_order_next_retry(tenant_id: int, order_id: int, next_retry_at: datetime.datetime | None) -> None:
    db.execute(
        "UPDATE orders SET next_retry_at = %s, updated_at = now() WHERE tenant_id = %s AND id = %s",
        (next_retry_at, tenant_id, order_id)
    )

def set_order_caller_id(tenant_id: int, order_id: int, caller_id: str) -> None:
    db.execute(
        "UPDATE orders SET caller_id = %s, updated_at = now() WHERE tenant_id = %s AND id = %s",
        (caller_id, tenant_id, order_id)
    )

def set_order_confirmed(tenant_id: int, order_id: int) -> None:
    db.execute(
        "UPDATE orders SET order_status = 'CONFIRMED', confirmed_at = now(), updated_at = now() WHERE tenant_id = %s AND id = %s",
        (tenant_id, order_id)
    )

def update_order_webhook_status(tenant_id: int, order_id: int, status: str) -> None:
    db.execute(
        "UPDATE orders SET webhook_status = %s, updated_at = now() WHERE tenant_id = %s AND id = %s",
        (status, tenant_id, order_id)
    )

def get_orders(
    tenant_id: int,
    status: str | None = None,
    search: str | None = None,
    date_from: str | datetime.datetime | None = None,
    date_to: str | datetime.datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    after_id: int | None = None,
    before_id: int | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM orders WHERE tenant_id = %s"
    params: list[Any] = [tenant_id]
    if status:
        sql += " AND order_status = %s"
        params.append(status)
    if search:
        sql += " AND (phone LIKE %s OR customer_name ILIKE %s OR external_order_id ILIKE %s)"
        search_param = f"%{search}%"
        params.extend([search_param, search_param, search_param])
    if date_from:
        sql += " AND created_at >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND created_at <= %s"
        params.append(date_to)

    if after_id:
        sql += " AND (created_at, id) < (SELECT created_at, id FROM orders WHERE tenant_id = %s AND id = %s)"
        params.extend([tenant_id, after_id])
        sql += " ORDER BY created_at DESC, id DESC LIMIT %s"
        params.append(limit)
    elif before_id:
        sql += " AND (created_at, id) > (SELECT created_at, id FROM orders WHERE tenant_id = %s AND id = %s)"
        params.extend([tenant_id, before_id])
        sql += " ORDER BY created_at ASC, id ASC LIMIT %s"
        params.append(limit)
    else:
        sql += " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

    rows = db.query(sql, tuple(params))
    if before_id:
        rows.reverse()
    return rows

def get_orders_count(
    tenant_id: int,
    status: str | None = None,
    search: str | None = None,
    date_from: str | datetime.datetime | None = None,
    date_to: str | datetime.datetime | None = None
) -> int:
    sql = "SELECT COUNT(*) FROM orders WHERE tenant_id = %s"
    params: list[Any] = [tenant_id]
    if status:
        sql += " AND order_status = %s"
        params.append(status)
    if search:
        sql += " AND (phone LIKE %s OR customer_name ILIKE %s OR external_order_id ILIKE %s)"
        search_param = f"%{search}%"
        params.extend([search_param, search_param, search_param])
    if date_from:
        sql += " AND created_at >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND created_at <= %s"
        params.append(date_to)
    row = db.query_one(sql, tuple(params))
    return int(row["count"]) if row else 0

def list_orders(
    tenant_id: int,
    filters: dict | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict[str, Any]:
    """Paginated order list with optional filters dict (status, search, date_from, date_to)."""
    f = filters or {}
    offset = (page - 1) * per_page
    data = get_orders(
        tenant_id,
        status=f.get("status"),
        search=f.get("search"),
        date_from=f.get("date_from"),
        date_to=f.get("date_to"),
        limit=per_page,
        offset=offset,
    )
    total = get_orders_count(
        tenant_id,
        status=f.get("status"),
        search=f.get("search"),
        date_from=f.get("date_from"),
        date_to=f.get("date_to"),
    )
    return {"data": data, "total": total, "page": page, "per_page": per_page}


def get_due_retry_orders(limit: int = 50) -> list[dict[str, Any]]:
    """Fetch orders across the platform that are due for retry calls, oldest due
    first. Capped at `limit` per call to avoid a thundering herd when many
    orders become due in the same scheduler tick."""
    return db.query(
        "SELECT * FROM orders WHERE order_status = 'RETRY_SCHEDULED' AND next_retry_at <= now() "
        "ORDER BY next_retry_at ASC LIMIT %s",
        (limit,)
    )


# --- Order Call Attempts CRUD ---

def create_call_attempt(
    order_id: int,
    tenant_id: int,
    attempt_no: int,
    call_id: int | None = None,
    started_at: datetime.datetime | None = None
) -> int:
    started_at = started_at or datetime.datetime.now(datetime.timezone.utc)
    return db.insert_returning_id(
        """
        INSERT INTO order_call_attempts (order_id, tenant_id, attempt_no, call_id, started_at)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (order_id, tenant_id, attempt_no, call_id, started_at)
    )

def update_call_attempt(
    attempt_id: int,
    tenant_id: int,
    result: str,
    dtmf_digit: str | None,
    talk_seconds: int,
    charge: Decimal,
    ended_at: datetime.datetime | None = None
) -> None:
    ended_at = ended_at or datetime.datetime.now(datetime.timezone.utc)
    db.execute(
        """
        UPDATE order_call_attempts
        SET result = %s, dtmf_digit = %s, talk_seconds = %s, charge = %s, ended_at = %s
        WHERE tenant_id = %s AND id = %s
        """,
        (result, dtmf_digit, talk_seconds, charge, ended_at, tenant_id, attempt_id)
    )

def update_call_attempt_by_call_id_system(
    call_id: int,
    result: str,
    dtmf_digit: str | None,
    talk_seconds: int,
    charge: Decimal,
    ended_at: datetime.datetime | None = None,
    billable_seconds: int = 0,
    wallet_transaction_id: int | None = None
) -> None:
    ended_at = ended_at or datetime.datetime.now(datetime.timezone.utc)
    db.execute(
        """
        UPDATE order_call_attempts
        SET result = %s, dtmf_digit = %s, talk_seconds = %s, charge = %s, ended_at = %s,
            billable_seconds = %s, wallet_transaction_id = %s
        WHERE call_id = %s
        """,
        (result, dtmf_digit, talk_seconds, charge, ended_at, billable_seconds, wallet_transaction_id, call_id)
    )

def get_call_attempts_for_order(tenant_id: int, order_id: int) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM order_call_attempts WHERE tenant_id = %s AND order_id = %s ORDER BY attempt_no ASC",
        (tenant_id, order_id)
    )
