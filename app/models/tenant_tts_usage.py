"""Tenant TTS usage tracking.

All counters use atomic PostgreSQL UPSERT so concurrent Flask workers
increment the same row without losing updates.

NOTE: Strict pre-call quota reservation (SELECT FOR UPDATE before API call)
is NOT yet implemented. Counters are post-success/failure increments only.
Under very high concurrency a brief over-quota window is possible.
The quota check in the entitlement resolver reads the latest committed value
and is safe for typical single-tenant burst patterns.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.db import db

logger = logging.getLogger("ccall")


def _billing_month(for_date: date | None = None) -> date:
    """Return the first day of the calendar month for ``for_date`` (default: today)."""
    if for_date is None:
        from datetime import datetime, timezone
        for_date = datetime.now(timezone.utc).date()
    return for_date.replace(day=1)


# ── Atomic increment helpers ─────────────────────────────────────────────────

def record_preview_success(
    tenant_id: int,
    provider: str,
    credential_mode: str,
    characters: int,
    audio_seconds: float = 0.0,
) -> None:
    """Atomically increment preview counters after a successful preview generation."""
    month = _billing_month()
    db.execute(
        """
        INSERT INTO tenant_tts_usage
            (tenant_id, billing_month, provider, credential_mode,
             characters_used, audio_seconds, preview_requests, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, 1, now())
        ON CONFLICT (tenant_id, billing_month, provider, credential_mode) DO UPDATE SET
            characters_used  = tenant_tts_usage.characters_used  + EXCLUDED.characters_used,
            audio_seconds    = tenant_tts_usage.audio_seconds    + EXCLUDED.audio_seconds,
            preview_requests = tenant_tts_usage.preview_requests + 1,
            updated_at       = now()
        """,
        (tenant_id, month, provider, credential_mode, max(0, characters), max(0.0, audio_seconds)),
    )


def record_production_success(
    tenant_id: int,
    provider: str,
    credential_mode: str,
    characters: int,
    audio_seconds: float = 0.0,
) -> None:
    """Atomically increment production counters after successful order-call TTS."""
    month = _billing_month()
    db.execute(
        """
        INSERT INTO tenant_tts_usage
            (tenant_id, billing_month, provider, credential_mode,
             characters_used, audio_seconds, production_requests, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, 1, now())
        ON CONFLICT (tenant_id, billing_month, provider, credential_mode) DO UPDATE SET
            characters_used      = tenant_tts_usage.characters_used      + EXCLUDED.characters_used,
            audio_seconds        = tenant_tts_usage.audio_seconds        + EXCLUDED.audio_seconds,
            production_requests  = tenant_tts_usage.production_requests  + 1,
            updated_at           = now()
        """,
        (tenant_id, month, provider, credential_mode, max(0, characters), max(0.0, audio_seconds)),
    )


def record_failed_request(
    tenant_id: int,
    provider: str,
    credential_mode: str,
) -> None:
    """Atomically increment failed_requests. Does NOT increment success counters."""
    month = _billing_month()
    db.execute(
        """
        INSERT INTO tenant_tts_usage
            (tenant_id, billing_month, provider, credential_mode, failed_requests, updated_at)
        VALUES (%s, %s, %s, %s, 1, now())
        ON CONFLICT (tenant_id, billing_month, provider, credential_mode) DO UPDATE SET
            failed_requests = tenant_tts_usage.failed_requests + 1,
            updated_at      = now()
        """,
        (tenant_id, month, provider, credential_mode),
    )


# ── Read helpers ─────────────────────────────────────────────────────────────

def get_monthly_usage(
    tenant_id: int,
    provider: str,
    credential_mode: str,
    for_month: date | None = None,
) -> dict[str, Any]:
    """Return current month usage counters for one tenant+provider+mode combination."""
    month = _billing_month(for_month)
    row = db.query_one(
        """
        SELECT characters_used, audio_seconds, preview_requests,
               production_requests, failed_requests, estimated_provider_cost
        FROM tenant_tts_usage
        WHERE tenant_id = %s AND billing_month = %s
          AND provider = %s AND credential_mode = %s
        """,
        (tenant_id, month, provider, credential_mode),
    )
    if not row:
        return {
            "characters_used": 0,
            "audio_seconds": 0.0,
            "preview_requests": 0,
            "production_requests": 0,
            "failed_requests": 0,
            "estimated_provider_cost": 0.0,
        }
    return dict(row)


def get_total_monthly_characters(
    tenant_id: int,
    provider: str,
    credential_mode: str,
    for_month: date | None = None,
) -> int:
    """Return total characters_used this month across all rows for tenant+provider+mode."""
    month = _billing_month(for_month)
    row = db.query_one(
        """
        SELECT COALESCE(SUM(characters_used), 0) AS total
        FROM tenant_tts_usage
        WHERE tenant_id = %s AND billing_month = %s
          AND provider = %s AND credential_mode = %s
        """,
        (tenant_id, month, provider, credential_mode),
    )
    return int(row["total"]) if row else 0


def get_all_monthly_usage(tenant_id: int, for_month: date | None = None) -> list[dict[str, Any]]:
    """Return all usage rows for a tenant this month."""
    month = _billing_month(for_month)
    rows = db.query(
        """
        SELECT provider, credential_mode, characters_used, audio_seconds,
               preview_requests, production_requests, failed_requests,
               estimated_provider_cost, updated_at
        FROM tenant_tts_usage
        WHERE tenant_id = %s AND billing_month = %s
        ORDER BY provider, credential_mode
        """,
        (tenant_id, month),
    )
    return [dict(r) for r in rows]
