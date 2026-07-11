from __future__ import annotations

import json
from typing import Any
from app.db import db


def create_log(data: dict[str, Any]) -> int:
    """Create a new webhook log entry from a data dictionary."""
    payload_val = data.get("payload")
    if isinstance(payload_val, dict):
        payload_val = json.dumps(payload_val)
    return db.insert_returning_id(
        """
        INSERT INTO webhook_logs (tenant_id, order_id, url, payload, status_code, response_body, attempt_no, success)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            data.get("tenant_id"),
            data.get("order_id"),
            data.get("url"),
            payload_val,
            data.get("status_code"),
            data.get("response_body"),
            data.get("attempt_no", 1),
            data.get("success", False)
        )
    )


def update_log(id: int, status_code: int | None, response_body: str | None, success: bool) -> None:
    """Update an existing webhook log entry with completion details."""
    db.execute(
        """
        UPDATE webhook_logs
        SET status_code = %s, response_body = %s, success = %s, sent_at = now()
        WHERE id = %s
        """,
        (status_code, response_body, success, id)
    )


def get_logs_for_order(order_id: int) -> list[dict[str, Any]]:
    """Retrieve all webhook logs for a specific order."""
    return db.query(
        "SELECT * FROM webhook_logs WHERE order_id = %s ORDER BY sent_at DESC",
        (order_id,)
    )


# --- Backward compatibility methods ---

def create_webhook_log(
    tenant_id: int,
    order_id: int,
    url: str,
    payload: dict[str, Any],
    status_code: int | None,
    response_body: str | None,
    attempt_no: int = 1,
    success: bool = False
) -> int:
    return db.insert_returning_id(
        """
        INSERT INTO webhook_logs (tenant_id, order_id, url, payload, status_code, response_body, attempt_no, success)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (tenant_id, order_id, url, json.dumps(payload), status_code, response_body, attempt_no, success)
    )


def get_webhook_logs(tenant_id: int, order_id: int | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    if order_id is not None:
        return db.query(
            """
            SELECT * FROM webhook_logs
            WHERE tenant_id = %s AND order_id = %s
            ORDER BY sent_at DESC LIMIT %s OFFSET %s
            """,
            (tenant_id, order_id, limit, offset)
        )
    return db.query(
        """
        SELECT * FROM webhook_logs
        WHERE tenant_id = %s
        ORDER BY sent_at DESC LIMIT %s OFFSET %s
        """,
        (tenant_id, limit, offset)
    )


def get_webhook_logs_count(tenant_id: int, order_id: int | None = None) -> int:
    if order_id is not None:
        row = db.query_one(
            "SELECT COUNT(*) FROM webhook_logs WHERE tenant_id = %s AND order_id = %s",
            (tenant_id, order_id)
        )
    else:
        row = db.query_one(
            "SELECT COUNT(*) FROM webhook_logs WHERE tenant_id = %s",
            (tenant_id,)
        )
    return int(row["count"]) if row else 0
