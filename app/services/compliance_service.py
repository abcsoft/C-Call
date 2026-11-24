# app/services/compliance_service.py
"""Outbound calling compliance: destination restrictions, opt-out/DND, consent, and daily spend limits."""
from __future__ import annotations

import datetime
import logging
import os
from decimal import Decimal
from typing import Any

from app.db import db
from app.middleware.security import mask_phone

logger = logging.getLogger("ccall.compliance")

# Default allowed prefixes: Bangladesh mobile (+88013..+88019)
_DEFAULT_ALLOWED_PREFIXES = ("+88013", "+88014", "+88015", "+88016", "+88017", "+88018", "+88019")


def check_destination_allowed(tenant_id: int, normalized_phone: str) -> None:
    """Enforce outbound destination prefix restrictions. Raises ValueError if blocked."""
    env_prefixes = os.environ.get("ALLOWED_DIAL_PREFIXES", "").strip()
    prefixes: tuple[str, ...] = (
        tuple(p.strip() for p in env_prefixes.split(",") if p.strip())
        if env_prefixes
        else _DEFAULT_ALLOWED_PREFIXES
    )
    tenant = db.query_one("SELECT allowed_dial_prefixes FROM tenants WHERE id = %s", (tenant_id,))
    if tenant and tenant.get("allowed_dial_prefixes"):
        prefixes = tuple(tenant["allowed_dial_prefixes"])

    if prefixes and not any(normalized_phone.startswith(p) for p in prefixes):
        logger.warning(
            "Outbound destination blocked by prefix policy: tenant=%s phone=%s",
            tenant_id, mask_phone(normalized_phone),
        )
        raise ValueError("Destination prefix is not permitted by outbound dialing policy.")


def is_opted_out(tenant_id: int, normalized_phone: str) -> bool:
    """Return True if the phone number is on the tenant's opt-out / DND blocklist."""
    row = db.query_one(
        "SELECT id FROM opt_out_numbers WHERE tenant_id = %s AND phone = %s",
        (tenant_id, normalized_phone),
    )
    return row is not None


def record_opt_out(tenant_id: int, normalized_phone: str, reason: str = "customer_opt_out", source: str = "api") -> None:
    """Add a phone number to the tenant's opt-out blocklist idempotently."""
    db.execute(
        """
        INSERT INTO opt_out_numbers (tenant_id, phone, reason, source)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (tenant_id, phone) DO UPDATE
            SET reason = EXCLUDED.reason,
                source = EXCLUDED.source
        """,
        (tenant_id, normalized_phone, reason, source),
    )
    logger.info("Recorded opt-out: tenant=%s phone=%s source=%s", tenant_id, mask_phone(normalized_phone), source)


def remove_opt_out(tenant_id: int, normalized_phone: str) -> None:
    """Remove a phone number from the tenant's opt-out blocklist."""
    db.execute(
        "DELETE FROM opt_out_numbers WHERE tenant_id = %s AND phone = %s",
        (tenant_id, normalized_phone),
    )


def check_daily_spending_limit(tenant_id: int, estimated_charge: Decimal) -> None:
    """Enforce tenant daily_spend_limit if configured. Raises ValueError if exceeded."""
    tenant = db.query_one("SELECT daily_spend_limit FROM tenants WHERE id = %s", (tenant_id,))
    if not tenant or tenant.get("daily_spend_limit") is None:
        return
    limit = Decimal(str(tenant["daily_spend_limit"]))
    if limit <= 0:
        return
    row = db.query_one(
        """
        SELECT COALESCE(SUM(amount), 0) AS spent_today
        FROM wallet_transactions
        WHERE tenant_id = %s
          AND type = 'debit'
          AND created_at >= CURRENT_DATE
        """,
        (tenant_id,),
    )
    spent = Decimal(str(row["spent_today"] if row else 0))
    if spent + estimated_charge > limit:
        logger.warning(
            "Daily spend limit exceeded: tenant=%s spent=%s limit=%s",
            tenant_id, spent, limit,
        )
        raise ValueError("Tenant daily spending limit exceeded.")


def validate_outbound_call(tenant_id: int, normalized_phone: str, estimated_charge: Decimal = Decimal("0")) -> None:
    """Run all outbound compliance checks before queuing or dialing."""
    check_destination_allowed(tenant_id, normalized_phone)
    if is_opted_out(tenant_id, normalized_phone):
        raise ValueError("Recipient has opted out of automated calls.")
    check_daily_spending_limit(tenant_id, estimated_charge)
