"""
Wallet ledger reconciliation service.

Computes: wallet.balance == SUM(wallet_transactions debits/credits)
for every tenant. Emits aggregate gauge — no per-tenant metric labels.
Does NOT automatically correct balances.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from app.db import db

logger = logging.getLogger("ccall.reconciliation")


def run_wallet_reconciliation() -> int:
    """Compare wallet.balance to ledger sum for every tenant.

    Returns count of tenants with a mismatch.
    Any mismatch is logged at WARNING — no automatic balance mutation.
    """
    try:
        rows = db.query(
            """
            SELECT
                w.tenant_id,
                w.balance AS wallet_balance,
                COALESCE(SUM(
                    CASE WHEN wt.type = 'credit' THEN wt.amount
                         ELSE -wt.amount
                    END
                ), 0) AS ledger_balance
            FROM wallets w
            LEFT JOIN wallet_transactions wt ON wt.tenant_id = w.tenant_id
            GROUP BY w.tenant_id, w.balance
            HAVING ABS(
                w.balance - COALESCE(SUM(
                    CASE WHEN wt.type = 'credit' THEN wt.amount
                         ELSE -wt.amount
                    END
                ), 0)
            ) > 0.001
            """
        )
    except Exception as exc:
        logger.error("Wallet reconciliation DB query failed: %s", exc)
        return 0

    mismatch_count = len(rows)

    for row in rows:
        tenant_id = row["tenant_id"]
        wallet_bal = Decimal(str(row["wallet_balance"]))
        ledger_bal = Decimal(str(row["ledger_balance"]))
        diff = wallet_bal - ledger_bal
        # Log tenant_id but NEVER log actual balance values in production.
        # Balance amounts must not appear in logs (PCI/privacy risk).
        logger.warning(
            "WALLET_LEDGER_MISMATCH tenant_id=%s delta_sign=%s "
            "action=investigate_do_not_auto_correct",
            tenant_id,
            "positive" if diff > 0 else "negative",
        )

    if mismatch_count > 0:
        logger.error(
            "WALLET_LEDGER_MISMATCH_SUMMARY total_mismatches=%d "
            "action=requires_manual_review runbook=RB-09",
            mismatch_count,
        )

    return mismatch_count


def wallet_reconcile_job() -> None:
    """APScheduler entry point. Updates Prometheus gauge."""
    from app.monitoring import WALLET_LEDGER_MISMATCH
    count = run_wallet_reconciliation()
    WALLET_LEDGER_MISMATCH.set(count)
