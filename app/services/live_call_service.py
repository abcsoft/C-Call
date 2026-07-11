# app/services/live_call_service.py
"""Tenant-aware wrapper around call_engine.active_calls — the in-memory
live-call registry. Enforces the tenant_id filter/ownership boundary so
blueprints never touch the registry directly."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from call_engine import active_calls as registry


def _serialize(entry: dict[str, Any]) -> dict[str, Any]:
    out = dict(entry)
    started_at = out.get("started_at")
    answered_at = out.get("answered_at")
    out["started_at"] = started_at.isoformat() if started_at else None
    out["answered_at"] = answered_at.isoformat() if answered_at else None
    return out


def list_live_calls(tenant_id: int | None) -> list[dict[str, Any]]:
    """tenant_id=None -> all tenants (super admin). Otherwise scoped."""
    return [_serialize(e) for e in registry.list_active_calls(tenant_id)]


def hangup_call(call_id: int, tenant_id: int | None) -> bool:
    """tenant_id=None -> super admin, can hang up any tenant's call.
    Otherwise scoped — returns False (not 403) if the call belongs to a
    different tenant, same as "not found", to avoid leaking existence."""
    return registry.request_cancel(call_id, tenant_id=tenant_id)


def get_summary(tenant_id: int | None) -> dict[str, Any]:
    """Live counts for the summary cards. Today's confirmed/cancelled/
    no_answer/failed counts come from the calls table (durable), not the
    in-memory registry (which only holds calls still in progress)."""
    from app.db import db

    calls = registry.list_active_calls(tenant_id)
    active = len(calls)
    ringing = sum(1 for c in calls if c["status"] == "ringing")
    answered = sum(1 for c in calls if c["status"] in ("answered", "playing_tts", "waiting_dtmf"))

    where = "tenant_id = %s AND" if tenant_id is not None else ""
    params: tuple = (tenant_id,) if tenant_id is not None else ()

    today_row = db.query_one(
        f"""
        SELECT
            COUNT(*) FILTER (WHERE status = 'completed') AS confirmed_today,
            COUNT(*) FILTER (WHERE status = 'canceled') AS cancelled_today,
            COUNT(*) FILTER (WHERE status = 'no_answer') AS no_answer_today,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed_today,
            COUNT(*) AS calls_today
        FROM calls
        WHERE {where} started_at >= CURRENT_DATE
        """,
        params,
    )

    return {
        "active_calls": active,
        "ringing": ringing,
        "answered": answered,
        "calls_today": today_row["calls_today"] if today_row else 0,
        "confirmed_today": today_row["confirmed_today"] if today_row else 0,
        "cancelled_today": today_row["cancelled_today"] if today_row else 0,
        "no_answer_today": today_row["no_answer_today"] if today_row else 0,
        "failed_today": today_row["failed_today"] if today_row else 0,
    }
