# app/models/subscription.py
"""Raw SQL query helpers for subscriptions, subscription_events, subscription_payments."""
from __future__ import annotations

import datetime
from typing import Any

from app.db import db


def get_subscription_by_id(sub_id: int) -> dict | None:
    return db.query_one("SELECT * FROM subscriptions WHERE id = %s", (sub_id,))


def get_current_subscription(tenant_id: int) -> dict | None:
    """Return the tenant's current (linked) subscription, or latest non-expired."""
    row = db.query_one(
        """
        SELECT s.* FROM subscriptions s
        JOIN tenants t ON t.current_subscription_id = s.id
        WHERE t.id = %s
        """,
        (tenant_id,),
    )
    if row:
        return row
    # Fallback: latest non-cancelled/expired
    return db.query_one(
        """
        SELECT * FROM subscriptions
        WHERE tenant_id = %s
          AND status NOT IN ('cancelled','expired')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (tenant_id,),
    )


def create_subscription(
    tenant_id: int,
    plan_id: int,
    billing_cycle: str,
    status: str,
    started_at: datetime.datetime,
    current_period_start: datetime.datetime | None = None,
    current_period_end: datetime.datetime | None = None,
) -> int:
    return db.insert_returning_id(
        """
        INSERT INTO subscriptions
            (tenant_id, plan_id, billing_cycle, status, started_at,
             current_period_start, current_period_end)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (tenant_id, plan_id, billing_cycle, status, started_at,
         current_period_start, current_period_end),
    )


def update_subscription(sub_id: int, **fields) -> None:
    allowed = {
        "status", "current_period_start", "current_period_end",
        "grace_ends_at", "past_due_ends_at", "cancelled_at", "cancel_at_period_end",
        "cancellation_reason", "renewal_count",
        "suspended_at", "suspension_reason", "suspension_type",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k} = %s" for k in updates)
    vals = list(updates.values()) + [sub_id]
    db.execute(
        f"UPDATE subscriptions SET {sets}, updated_at = now() WHERE id = %s",
        vals,
    )


def record_subscription_event(
    tenant_id: int,
    event_type: str,
    from_status: str | None = None,
    to_status: str | None = None,
    subscription_id: int | None = None,
    actor_type: str = "system",
    actor_id: int | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
) -> None:
    import json
    db.execute(
        """
        INSERT INTO subscription_events
            (subscription_id, tenant_id, event_type, from_status, to_status,
             actor_type, actor_id, reason, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            subscription_id, tenant_id, event_type, from_status, to_status,
            actor_type, actor_id, reason,
            json.dumps(metadata or {}),
        ),
    )


def get_subscription_events(tenant_id: int, limit: int = 20) -> list[dict]:
    return db.query(
        """
        SELECT * FROM subscription_events
        WHERE tenant_id = %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (tenant_id, limit),
    )


def create_subscription_payment(
    subscription_id: int,
    tenant_id: int,
    amount,
    billing_period_start: datetime.datetime | None,
    billing_period_end: datetime.datetime | None,
    idempotency_key: str,
    payment_intent_id: int | None = None,
    status: str = "pending",
) -> int:
    return db.insert_returning_id(
        """
        INSERT INTO subscription_payments
            (subscription_id, tenant_id, payment_intent_id, amount, currency,
             billing_period_start, billing_period_end, status, idempotency_key)
        VALUES (%s, %s, %s, %s, 'BDT', %s, %s, %s, %s)
        RETURNING id
        """,
        (
            subscription_id, tenant_id, payment_intent_id, amount,
            billing_period_start, billing_period_end, status, idempotency_key,
        ),
    )


def get_subscription_payment_by_idempotency(key: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM subscription_payments WHERE idempotency_key = %s",
        (key,),
    )


def update_subscription_payment(pay_id: int, status: str, payment_intent_id: int | None = None) -> None:
    db.execute(
        """
        UPDATE subscription_payments
        SET status = %s, payment_intent_id = COALESCE(%s, payment_intent_id), updated_at = now()
        WHERE id = %s
        """,
        (status, payment_intent_id, pay_id),
    )


def get_tenant_subscription_history(tenant_id: int) -> list[dict]:
    return db.query(
        """
        SELECT s.*, p.name AS plan_name
        FROM subscriptions s
        JOIN plans p ON p.id = s.plan_id
        WHERE s.tenant_id = %s
        ORDER BY s.created_at DESC
        """,
        (tenant_id,),
    )
