# app/services/billing_service.py
"""Single authoritative path for billing a finished call.

All wallet deductions, ledger inserts, and call-state updates that result from
a call reaching a terminal status MUST flow through finalize_call_billing().
No other code path may independently debit a wallet, insert a ledger row, or
set calls.billing_finalized.

Atomicity guarantee
-------------------
finalize_call_billing() opens exactly one PostgreSQL transaction and commits:
  1. wallets.balance  -= charge
  2. wallet_transactions row (type='debit')
  3. calls.billing_finalized = TRUE, calls.charge, calls.wallet_transaction_id
  4. order_call_attempts.charge, .wallet_transaction_id

All four mutations commit together or none of them do.  A process crash at any
point leaves the call in an unfinalized state eligible for safe retry.

Idempotency
-----------
The call row is locked with SELECT FOR UPDATE before the billing_finalized check.
Two concurrent workers racing to bill the same call serialize here: the second
sees billing_finalized=TRUE and returns already_finalized=True with no writes.

A partial unique index on wallet_transactions (tenant_id, ref_id) WHERE
type='debit' AND ref_id LIKE 'call:%' provides belt-and-suspenders: even if the
FOR UPDATE guard is somehow bypassed, the second INSERT fails with a unique
violation and the transaction rolls back cleanly.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import os as _os

from app.db import db
from app.services.rate_service import calculate_charge_and_duration

logger = logging.getLogger("ccall")

# ── Testing-only fault injection ──────────────────────────────────────────────
# Set BILLING_FAULT_POINT=<name> in the environment to terminate the process
# abnormally at the named checkpoint.  Only honoured when FLASK_ENV=testing.
# Production use is rejected at startup.
#
# finalize_call_billing fault points:
#   after_call_lock         — after SELECT call FOR UPDATE
#   after_charge_calculation — after calculate_charge_and_duration()
#   after_wallet_lock       — after SELECT wallet FOR UPDATE
#   after_wallet_update     — after UPDATE wallets
#   after_ledger_insert     — after INSERT wallet_transactions
#   after_call_billing_update — after UPDATE calls billing_finalized=TRUE
#   before_transaction_commit — immediately before the with-block exits
#
# refund_call_charge fault points:
#   after_refund_wallet_lock   — after SELECT wallet FOR UPDATE
#   after_refund_wallet_update — after UPDATE wallets (refund credit)
#   after_refund_ledger_insert — after INSERT wallet_transactions (refund row)
#   after_refund_state_update  — after UPDATE calls refund_status='refunded'
#   before_refund_commit       — immediately before the with-block exits

_FAULT_POINT: str | None = None

def _init_fault_point() -> None:
    global _FAULT_POINT
    fp = _os.environ.get("BILLING_FAULT_POINT")
    if not fp:
        return
    env = _os.environ.get("FLASK_ENV", "production")
    if env != "testing":
        raise RuntimeError(
            f"BILLING_FAULT_POINT={fp!r} is set but FLASK_ENV={env!r}. "
            "Fault injection is only permitted in testing environments. "
            "This process will not start to prevent accidental production use."
        )
    _FAULT_POINT = fp
    logger.warning("BILLING FAULT POINT ENABLED: %s (testing only)", fp)

_init_fault_point()


def _maybe_fault(checkpoint: str) -> None:
    """If the current fault point matches checkpoint, crash the process abnormally.

    Uses os._exit(1) — bypasses all Python cleanup so no partial commit can sneak
    past the fault.  Only reachable when FLASK_ENV=testing.
    """
    if _FAULT_POINT and _FAULT_POINT == checkpoint:
        logger.critical("FAULT INJECTION: terminating at checkpoint %s", checkpoint)
        _os._exit(1)  # abnormal termination — no atexit, no finally

TERMINAL_STATUSES = {
    "completed",    # IVR flow ended naturally
    "confirmed",    # DTMF 1 — order confirmed
    "cancelled",    # DTMF 2 — order cancelled
    "canceled",     # Remote BYE mid-flow — answered then remote hung up
    "failed",       # Technical failure
    "no_answer",    # Never picked up
    "busy",         # Line busy
    "unreachable",  # Number unreachable
    "timeout",      # Call timed out
}

# Statuses that represent pre-connection failures with no carrier cost.
_PRE_CONNECTION_STATUSES = {"no_answer", "busy", "failed", "unreachable", "timeout"}


def finalize_call_billing(
    call_id: int,
    *,
    expected_tenant_id: int | None = None,
    source: str = "unknown",
) -> dict[str, Any]:
    """Bill a single completed call exactly once — atomic and idempotent.

    Returns a dict with keys:
      billed (bool)              — True if a new debit was created
      already_finalized (bool)   — True if the call was already billed
      call_id (int)
      tenant_id (int | None)
      charge (Decimal)
      billable_seconds (int)
      wallet_transaction_id (int | None)
      reason (str | None)        — error code when billed=False and not already_finalized
      error_code (str | None)    — alias for reason (forward-compat)

    Error codes:
      call_not_found        — call_id does not exist
      no_tenant             — call has no tenant_id
      tenant_mismatch       — call belongs to a different tenant than expected_tenant_id
      not_terminal          — call status is not in TERMINAL_STATUSES
      wallet_not_found      — tenant has no wallet row
      insufficient_balance  — balance < charge (call stays unfinalized for retry after top-up)
      db_error              — unexpected database error (propagated as exception)
    """
    _defaults: dict[str, Any] = {
        "charge": Decimal("0.0000"),
        "billable_seconds": 0,
        "wallet_transaction_id": None,
        "tenant_id": None,
        "already_finalized": False,
        "reason": None,
        "error_code": None,
    }

    with db.transaction() as conn:
        # ── 1. Lock the call row — serialize concurrent billing attempts ──────
        call = conn.execute(
            "SELECT * FROM calls WHERE id = %s FOR UPDATE",
            (call_id,),
        ).fetchone()

        if not call:
            return {**_defaults, "billed": False, "call_id": call_id, "reason": "call_not_found", "error_code": "call_not_found"}

        _maybe_fault("after_call_lock")
        tenant_id: int = call["tenant_id"]
        if tenant_id is None:
            return {**_defaults, "billed": False, "call_id": call_id, "reason": "no_tenant", "error_code": "no_tenant"}

        # ── 2. Tenant ownership validation ────────────────────────────────────
        if expected_tenant_id is not None and tenant_id != expected_tenant_id:
            logger.warning(
                "finalize_call_billing: tenant mismatch call=%s expected=%s actual=%s source=%s",
                call_id, expected_tenant_id, tenant_id, source,
            )
            return {
                "billed": False,
                "call_id": call_id,
                **_defaults,
                "tenant_id": tenant_id,
                "reason": "tenant_mismatch",
                "error_code": "tenant_mismatch",
            }

        # ── 3. Idempotency: already finalized ─────────────────────────────────
        if call.get("billing_finalized"):
            try:
                from app.monitoring import CALL_BILLING_ALREADY_FINALIZED
                CALL_BILLING_ALREADY_FINALIZED.inc()
            except Exception:
                pass
            return {
                "billed": False,
                "already_finalized": True,
                "call_id": call_id,
                "tenant_id": tenant_id,
                "charge": Decimal(str(call["charge"])) if call["charge"] is not None else Decimal("0"),
                "billable_seconds": call.get("billable_seconds") or 0,
                "wallet_transaction_id": call["wallet_transaction_id"],
                "reason": "already_billed",
                "error_code": "already_billed",
            }

        # ── 4. Terminal status check ──────────────────────────────────────────
        status = call["status"]
        if status not in TERMINAL_STATUSES:
            return {
                **_defaults,
                "billed": False,
                "call_id": call_id,
                "tenant_id": tenant_id,
                "reason": "not_terminal",
                "error_code": "not_terminal",
            }

        talk_seconds = int(call["talk_seconds"] or 0)

        # ── 5. Resolve rate (snapshot stored at dispatch time, or live fallback) ──
        snapshot = call.get("rate_snapshot")
        snapshot_rates: dict | None = None
        if snapshot:
            try:
                s = snapshot if isinstance(snapshot, dict) else json.loads(snapshot)
                snapshot_rates = {
                    "rate_per_min": Decimal(str(s["rate_per_min"])),
                    "rate_per_call": Decimal(str(s["rate_per_call"])),
                    "min_charge": Decimal(str(s["min_charge"])),
                    "billing_pulse_seconds": int(s["billing_pulse_seconds"]),
                    "minimum_billable_seconds": int(s["minimum_billable_seconds"]),
                    "charge_failed_calls": bool(s["charge_failed_calls"]),
                }
            except Exception as exc:
                logger.warning(
                    "finalize_call_billing: ignoring malformed rate_snapshot call=%s: %s",
                    call_id, exc,
                )

        # ── 6. Calculate charge ───────────────────────────────────────────────
        charge, billable_seconds = calculate_charge_and_duration(
            talk_seconds, tenant_id, status, rates=snapshot_rates
        )
        _maybe_fault("after_charge_calculation")

        txn_id: int | None = None

        if charge > 0:
            # ── 7. Lock wallet row — IN THE SAME TRANSACTION as the call lock ──
            wallet = conn.execute(
                "SELECT id, balance FROM wallets WHERE tenant_id = %s FOR UPDATE",
                (tenant_id,),
            ).fetchone()

            _maybe_fault("after_wallet_lock")
            if not wallet:
                logger.error(
                    "finalize_call_billing: wallet not found for tenant=%s call=%s source=%s",
                    tenant_id, call_id, source,
                )
                try:
                    from app.monitoring import CALL_BILLING_FAILED
                    CALL_BILLING_FAILED.labels(error_code="wallet_not_found").inc()
                except Exception:
                    pass
                return {
                    **_defaults,
                    "billed": False,
                    "call_id": call_id,
                    "tenant_id": tenant_id,
                    "reason": "wallet_not_found",
                    "error_code": "wallet_not_found",
                }

            current_balance = Decimal(str(wallet["balance"]))
            if current_balance < charge:
                # Do not mark billing_finalized — allow retry after wallet top-up.
                logger.warning(
                    "finalize_call_billing: insufficient balance tenant=%s call=%s "
                    "balance=%s charge=%s source=%s",
                    tenant_id, call_id, current_balance, charge, source,
                )
                try:
                    from app.monitoring import CALL_BILLING_FAILED, WALLET_INSUFFICIENT
                    CALL_BILLING_FAILED.labels(error_code="insufficient_balance").inc()
                    WALLET_INSUFFICIENT.inc()
                except Exception:
                    pass
                return {
                    "billed": False,
                    "call_id": call_id,
                    "tenant_id": tenant_id,
                    "charge": charge,
                    "billable_seconds": billable_seconds,
                    "wallet_transaction_id": None,
                    "already_finalized": False,
                    "reason": "insufficient_balance",
                    "error_code": "insufficient_balance",
                }

            new_balance = current_balance - charge

            # ── 8. Debit wallet (same transaction) ────────────────────────────
            conn.execute(
                "UPDATE wallets SET balance = %s, updated_at = now() WHERE tenant_id = %s",
                (new_balance, tenant_id),
            )
            _maybe_fault("after_wallet_update")

            # ── 9. Insert ledger row (same transaction) ───────────────────────
            # ref_id format 'call:{call_id}' matches the partial unique index
            # wallet_transactions_call_debit_uniq from migration 025, which prevents
            # a duplicate INSERT even if two concurrent transactions reach this point.
            txn_row = conn.execute(
                """INSERT INTO wallet_transactions
                       (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
                   VALUES (%s, %s, 'debit', %s, %s, %s, %s)
                   RETURNING id""",
                (
                    wallet["id"],
                    tenant_id,
                    charge,
                    new_balance,
                    f"Call {call_id}",
                    f"call:{call_id}",
                ),
            ).fetchone()
            txn_id = int(txn_row["id"])
            _maybe_fault("after_ledger_insert")

        # ── 10. Mark call finalized (same transaction — commits atomically) ───
        conn.execute(
            "UPDATE calls SET charge = %s, billable_seconds = %s,"
            " wallet_transaction_id = %s, billing_finalized = TRUE"
            " WHERE id = %s",
            (charge, billable_seconds, txn_id, call_id),
        )

        _maybe_fault("after_call_billing_update")

        # ── 11. Update attempt record (same transaction) ──────────────────────
        conn.execute(
            "UPDATE order_call_attempts SET charge = %s, billable_seconds = %s,"
            " wallet_transaction_id = %s WHERE call_id = %s",
            (charge, billable_seconds, txn_id, call_id),
        )

        _maybe_fault("before_transaction_commit")

    # ── Post-commit: metrics, cache invalidation, low balance alert ──────────
    try:
        from app.monitoring import CALL_BILLING_FINALIZED, WALLET_CALL_DEBIT
        CALL_BILLING_FINALIZED.inc()
        if charge > 0:
            WALLET_CALL_DEBIT.inc()
    except Exception:
        pass

    if charge > 0:
        try:
            from app.models.wallet import invalidate_wallet_cache
            invalidate_wallet_cache(tenant_id)
        except Exception:
            pass
        try:
            _trigger_low_balance_check(tenant_id)
        except Exception:
            pass

    logger.info(
        "finalize_call_billing: call=%s tenant=%s charge=%s txn=%s source=%s",
        call_id, tenant_id, charge, txn_id, source,
    )
    return {
        "billed": charge > 0,
        "already_finalized": False,
        "call_id": call_id,
        "tenant_id": tenant_id,
        "charge": charge,
        "billable_seconds": billable_seconds,
        "wallet_transaction_id": txn_id,
        "reason": None,
        "error_code": None,
    }


def refund_call_charge(
    call_id: int,
    *,
    reason_code: str,
    source: str,
) -> dict[str, Any]:
    """Idempotent refund of a finalized call charge — atomic and durable.

    Single transaction commits:
      1. wallet credit (UPDATE wallets)
      2. refund ledger row (INSERT wallet_transactions type='refund')
      3. durable state on calls (refund_status, refund_transaction_id, refunded_at, ...)

    All three commit together or none do.  A process crash at any point leaves
    the call with refund_status='none', enabling safe retry via the durable state
    check (step 4 below) rather than relying solely on the ledger scan.

    The partial unique index wallet_transactions_call_refund_uniq provides
    belt-and-suspenders: even if two concurrent refund attempts race past the
    durable state check, only one INSERT succeeds.

    Returns a dict with keys:
      refunded (bool)
      already_refunded (bool)
      call_id (int)
      tenant_id (int | None)
      refund_amount (Decimal)
      wallet_transaction_id (int | None)   — original debit
      refund_transaction_id (int | None)   — new credit/refund row
      reason (str | None)

    Error codes:
      call_not_found      — call_id does not exist
      no_original_debit   — call not billed (billing_finalized=FALSE or charge<=0)
      already_refunded    — durable state shows refund already committed
      wallet_not_found    — tenant has no wallet row
    """
    _defaults: dict[str, Any] = {
        "refund_amount": Decimal("0.0000"),
        "wallet_transaction_id": None,
        "refund_transaction_id": None,
        "tenant_id": None,
        "already_refunded": False,
        "reason": None,
    }

    with db.transaction() as conn:
        # ── 1. Lock call row — serializes concurrent refund attempts ──────────
        call = conn.execute(
            "SELECT * FROM calls WHERE id = %s FOR UPDATE",
            (call_id,),
        ).fetchone()

        if not call:
            return {**_defaults, "refunded": False, "call_id": call_id, "reason": "call_not_found"}

        tenant_id: int = call["tenant_id"]

        # ── 2. Validate original billing ──────────────────────────────────────
        if not call.get("billing_finalized") or not call.get("charge") or Decimal(str(call["charge"])) <= 0:
            return {
                **_defaults,
                "refunded": False,
                "call_id": call_id,
                "tenant_id": tenant_id,
                "reason": "no_original_debit",
            }

        # ── 3. Verify original debit exists in ledger ─────────────────────────
        debit_ref = f"call:{call_id}"
        original_debit = conn.execute(
            "SELECT id FROM wallet_transactions WHERE tenant_id = %s AND ref_id = %s AND type = 'debit'",
            (tenant_id, debit_ref),
        ).fetchone()
        if not original_debit:
            logger.error(
                "refund_call_charge: billing_finalized=TRUE but no debit row call=%s tenant=%s",
                call_id, tenant_id,
            )
            return {
                **_defaults,
                "refunded": False,
                "call_id": call_id,
                "tenant_id": tenant_id,
                "reason": "no_debit_row",
            }

        # ── 4. Check durable refund state (primary idempotency sentinel) ──────
        # The durable state column is the authoritative check. It is set atomically
        # with the ledger insert in step 8, so if refund_status='refunded' the
        # entire refund transaction committed successfully.
        has_durable_refund = call.get("refund_status") == "refunded"
        if has_durable_refund:
            return {
                **_defaults,
                "refunded": False,
                "already_refunded": True,
                "call_id": call_id,
                "tenant_id": tenant_id,
                "refund_amount": Decimal(str(call["charge"])),
                "wallet_transaction_id": call["wallet_transaction_id"],
                "refund_transaction_id": call.get("refund_transaction_id"),
                "reason": "already_refunded",
            }

        # ── 5. Belt-and-suspenders: also check ledger for existing refund row ─
        # Handles the edge case where durable state is 'none' but a ledger row
        # exists (crash after ledger insert, before state update in an older code
        # version, or manual DB repair).
        refund_ref = f"refund:call:{call_id}"
        existing_refund = conn.execute(
            "SELECT id FROM wallet_transactions WHERE tenant_id = %s AND ref_id = %s AND type = 'refund'",
            (tenant_id, refund_ref),
        ).fetchone()
        if existing_refund:
            logger.warning(
                "refund_call_charge: ledger refund row exists but durable state missing "
                "call=%s tenant=%s — returning already_refunded, not double-crediting",
                call_id, tenant_id,
            )
            return {
                **_defaults,
                "refunded": False,
                "already_refunded": True,
                "call_id": call_id,
                "tenant_id": tenant_id,
                "refund_amount": Decimal(str(call["charge"])),
                "wallet_transaction_id": call["wallet_transaction_id"],
                "refund_transaction_id": int(existing_refund["id"]),
                "reason": "already_refunded",
            }

        refund_amount = Decimal(str(call["charge"]))

        # ── 6. Lock wallet FOR UPDATE (same transaction) ───────────────────────
        wallet = conn.execute(
            "SELECT id, balance FROM wallets WHERE tenant_id = %s FOR UPDATE",
            (tenant_id,),
        ).fetchone()
        if not wallet:
            return {**_defaults, "refunded": False, "call_id": call_id, "tenant_id": tenant_id, "reason": "wallet_not_found"}

        _maybe_fault("after_refund_wallet_lock")
        new_balance = Decimal(str(wallet["balance"])) + refund_amount

        # ── 7. Credit wallet (same transaction) ───────────────────────────────
        conn.execute(
            "UPDATE wallets SET balance = %s, updated_at = now() WHERE tenant_id = %s",
            (new_balance, tenant_id),
        )
        _maybe_fault("after_refund_wallet_update")

        # ── 8. Insert refund ledger row (same transaction) ────────────────────
        # partial unique index wallet_transactions_call_refund_uniq (migration 025)
        # provides belt-and-suspenders against concurrent double-refund.
        refund_row = conn.execute(
            """INSERT INTO wallet_transactions
                   (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
               VALUES (%s, %s, 'refund', %s, %s, %s, %s)
               RETURNING id""",
            (
                wallet["id"],
                tenant_id,
                refund_amount,
                new_balance,
                f"Refund call {call_id}: {reason_code}",
                refund_ref,
            ),
        ).fetchone()
        refund_txn_id = int(refund_row["id"])
        _maybe_fault("after_refund_ledger_insert")

        # ── 9. Persist durable refund state on call row (same transaction) ────
        # This is the authoritative idempotency sentinel for future retry attempts.
        conn.execute(
            """UPDATE calls
               SET refund_status = 'refunded',
                   refund_transaction_id = %s,
                   refunded_at = now(),
                   refund_reason_code = %s,
                   refund_source = %s
               WHERE id = %s""",
            (refund_txn_id, reason_code, source, call_id),
        )
        _maybe_fault("after_refund_state_update")
        _maybe_fault("before_refund_commit")

    # ── Post-commit: metrics and cache invalidation ───────────────────────────
    try:
        from app.monitoring import WALLET_CALL_REFUND
        WALLET_CALL_REFUND.inc()
    except Exception:
        pass

    try:
        from app.models.wallet import invalidate_wallet_cache
        invalidate_wallet_cache(tenant_id)
    except Exception:
        pass

    logger.info(
        "refund_call_charge: call=%s tenant=%s amount=%s refund_txn=%s reason=%s source=%s",
        call_id, tenant_id, refund_amount, refund_txn_id, reason_code, source,
    )
    return {
        "refunded": True,
        "already_refunded": False,
        "call_id": call_id,
        "tenant_id": tenant_id,
        "refund_amount": refund_amount,
        "wallet_transaction_id": call["wallet_transaction_id"],
        "refund_transaction_id": refund_txn_id,
        "reason": None,
    }


def _trigger_low_balance_check(tenant_id: int) -> None:
    """Non-critical post-commit hook — check and send low balance alert."""
    from app.services.wallet_service import check_low_balance_alert
    try:
        check_low_balance_alert(tenant_id)
    except Exception as exc:
        logger.debug("finalize_call_billing: low balance check failed tenant=%s: %s", tenant_id, exc)
