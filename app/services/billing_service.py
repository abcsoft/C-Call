# app/services/billing_service.py
"""Single source of truth for billing a finished call — order confirmation
calls and direct/manual calls used to bill through two different paths
(order_service.update_status_from_call() only fired for order-attempt
calls), so direct/Quick-Call-Composer calls never deducted wallet at all.
finalize_call_billing() is now the only place that touches calls.charge /
calls.wallet_transaction_id, called from call_runner.py for every call type."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.db import db
from app.services.rate_service import calculate_charge_and_duration
from app.services.wallet_service import deduct

logger = logging.getLogger("ccall")

TERMINAL_STATUSES = {"completed", "failed", "no_answer", "busy", "canceled"}


def finalize_call_billing(call_id: int) -> dict[str, Any]:
    """Bill a single completed call exactly once.

    Idempotency: the calls row is locked with SELECT FOR UPDATE inside a
    transaction before we check wallet_transaction_id. Two concurrent
    workers racing to bill the same call will serialize here — the second
    one sees the txn_id already set by the first and exits early.

    Zero-charge calls (failed/no_answer) set billing_finalized=TRUE and
    wallet_transaction_id=NULL so the idempotency check (`billing_finalized`)
    remains effective on subsequent invocations — unlike the previous design
    that checked `wallet_transaction_id IS NOT NULL`, which was always NULL
    for zero-charge calls and caused infinite re-billing attempts.
    """
    defaults = {"charge": Decimal("0.0000"), "billable_seconds": 0, "wallet_transaction_id": None}

    with db.transaction() as conn:
        # Lock the row first — serialize concurrent billing attempts for the same call.
        call = conn.execute(
            "SELECT * FROM calls WHERE id = %s FOR UPDATE",
            (call_id,)
        ).fetchone()
        if not call:
            return {"billed": False, "reason": "call_not_found", "call_id": call_id, **defaults}

        tenant_id = call["tenant_id"]
        if tenant_id is None:
            return {"billed": False, "reason": "no_tenant", "call_id": call_id, **defaults}

        # Idempotency: already fully billed (non-zero charge with txn_id, or
        # zero-charge call that was already finalized via billing_finalized flag).
        if call.get("billing_finalized"):
            return {
                "billed": False,
                "reason": "already_billed",
                "call_id": call_id,
                "charge": call["charge"] or Decimal("0"),
                "billable_seconds": call.get("billable_seconds") or 0,
                "wallet_transaction_id": call["wallet_transaction_id"],
            }

        status = call["status"]
        if status not in TERMINAL_STATUSES:
            return {"billed": False, "reason": "not_terminal", "call_id": call_id, "status": status, **defaults}

        talk_seconds = int(call["talk_seconds"] or 0)
        charge, billable_seconds = calculate_charge_and_duration(talk_seconds, tenant_id, status)

        txn_id = None
        if charge > 0:
            try:
                txn = deduct(
                    tenant_id=tenant_id,
                    amount=charge,
                    description=f"Call {call_id}",
                    ref_id=str(call_id),
                )
                txn_id = txn["id"]
            except Exception as exc:
                logger.error(
                    "finalize_call_billing: deduction failed for call %s tenant %s: %s",
                    call_id, tenant_id, exc
                )
                return {
                    "billed": False,
                    "reason": "deduction_failed",
                    "call_id": call_id,
                    "error": str(exc),
                    "charge": charge,
                    "billable_seconds": billable_seconds,
                    "wallet_transaction_id": None,
                }

        # Mark as finalized regardless of charge amount — prevents re-entry.
        conn.execute(
            "UPDATE calls SET charge = %s, billable_seconds = %s, wallet_transaction_id = %s,"
            " billing_finalized = TRUE WHERE id = %s",
            (charge, billable_seconds, txn_id, call_id),
        )
        conn.execute(
            "UPDATE order_call_attempts SET charge = %s, billable_seconds = %s,"
            " wallet_transaction_id = %s WHERE call_id = %s",
            (charge, billable_seconds, txn_id, call_id),
        )

    return {
        "billed": charge > 0,
        "call_id": call_id,
        "charge": charge,
        "billable_seconds": billable_seconds,
        "wallet_transaction_id": txn_id,
    }
