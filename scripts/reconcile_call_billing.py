"""Reconcile call billing anomalies.

Detects and (with --apply) repairs deterministic billing inconsistencies.

Default: dry-run only — never mutates anything without --apply.

Detection scope
---------------
A  terminal call not finalized (billing_finalized=FALSE)
B  call marked billed (billing_finalized=TRUE, charge>0) but no ledger debit
C  ledger debit exists but call not marked billed (billing_finalized=FALSE)
D  duplicate debits per call (more than one debit row with ref_id='call:{id}')
E  charged amount on call differs from ledger debit amount
F  wallet balance differs from ledger-derived balance (sum of credits - debits)
G  refund without matching original debit
H  duplicate refund per call
I  tenant mismatch (wallet_transaction.tenant_id != call.tenant_id)
J  dispatch-failed call incorrectly charged (dispatch_status='dispatch_failed', charge>0)
K  missing rate snapshot on calls that were finalized after migration 021

Anomaly classes B, C, D are financial integrity failures that must never occur
with the atomic finalize_call_billing() contract.  If they appear it means an
older non-atomic code path wrote records — reconciliation identifies them for
operator review before any repairs.

Usage
-----
  # Dry run (safe, no DB writes):
  python scripts/reconcile_call_billing.py --dry-run

  # Limit scope:
  python scripts/reconcile_call_billing.py --dry-run --tenant-id 42
  python scripts/reconcile_call_billing.py --dry-run --call-id 1337
  python scripts/reconcile_call_billing.py --dry-run --from-date 2026-01-01 --to-date 2026-07-15

  # Machine-readable output:
  python scripts/reconcile_call_billing.py --dry-run --json

  # Apply (deducts wallets — requires explicit authorization):
  python scripts/reconcile_call_billing.py --apply --tenant-id 42 --batch-size 50

Safety
------
--apply resets billing_finalized=FALSE on affected calls and calls
finalize_call_billing() for each.  Ambiguous cases (class D, E, F) are
reported but never auto-repaired — they require operator action.

Never prints phone numbers, customer names, SIP credentials, or API keys.
"""
from __future__ import annotations

import argparse
import json as _json
import os
import sys
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("FLASK_ENV", "production")

TERMINAL_STATUSES = {
    "completed", "confirmed", "cancelled", "canceled",
    "failed", "no_answer", "busy", "unreachable", "timeout",
}


@dataclass
class Anomaly:
    anomaly_class: str    # A–K
    call_id: int | None
    tenant_id: int | None
    detail: str
    auto_repairable: bool


def _build_app():
    from app import create_app
    return create_app(os.environ.get("FLASK_ENV", "production"))


# ── Detection functions ───────────────────────────────────────────────────────

def detect_unfinalized_terminal_calls(db, tenant_id=None, call_id=None, from_date=None, to_date=None):
    """Class A: terminal call with billing_finalized=FALSE."""
    conds = ["status = ANY(%s)", "billing_finalized = FALSE", "tenant_id IS NOT NULL"]
    params: list[Any] = [list(TERMINAL_STATUSES)]
    if tenant_id:
        conds.append("tenant_id = %s"); params.append(tenant_id)
    if call_id:
        conds.append("id = %s"); params.append(call_id)
    if from_date:
        conds.append("started_at >= %s"); params.append(from_date)
    if to_date:
        conds.append("started_at <= %s"); params.append(to_date + " 23:59:59")
    sql = "SELECT id, tenant_id, status, talk_seconds, charge, started_at FROM calls WHERE " + " AND ".join(conds) + " ORDER BY started_at"
    rows = db.query(sql, params)
    return [Anomaly("A", r["id"], r["tenant_id"],
                    f"status={r['status']} talk_s={r['talk_seconds']} charge={r['charge']}",
                    auto_repairable=True) for r in rows]


def detect_billed_no_ledger(db, tenant_id=None, call_id=None):
    """Class B: billing_finalized=TRUE, charge>0, but no matching wallet_transactions row."""
    conds = ["c.billing_finalized = TRUE", "c.charge > 0",
             "NOT EXISTS (SELECT 1 FROM wallet_transactions wt WHERE wt.tenant_id = c.tenant_id AND wt.ref_id = 'call:' || c.id::text AND wt.type = 'debit')"]
    params: list[Any] = []
    if tenant_id:
        conds.append("c.tenant_id = %s"); params.append(tenant_id)
    if call_id:
        conds.append("c.id = %s"); params.append(call_id)
    sql = "SELECT c.id, c.tenant_id, c.charge, c.wallet_transaction_id FROM calls c WHERE " + " AND ".join(conds)
    rows = db.query(sql, params)
    return [Anomaly("B", r["id"], r["tenant_id"],
                    f"billing_finalized=TRUE charge={r['charge']} but no debit row",
                    auto_repairable=False) for r in rows]


def detect_ledger_no_billing_flag(db, tenant_id=None):
    """Class C: debit row exists for call but call not marked billed."""
    conds = ["wt.type = 'debit'", "wt.ref_id LIKE 'call:%%'",
             "EXISTS (SELECT 1 FROM calls c WHERE c.id::text = substring(wt.ref_id from 6) AND c.billing_finalized = FALSE)"]
    params: list[Any] = []
    if tenant_id:
        conds.append("wt.tenant_id = %s"); params.append(tenant_id)
    sql = "SELECT wt.id, wt.tenant_id, wt.ref_id, wt.amount FROM wallet_transactions wt WHERE " + " AND ".join(conds)
    rows = db.query(sql, params)
    return [Anomaly("C", None, r["tenant_id"],
                    f"debit txn={r['id']} ref={r['ref_id']} amount={r['amount']} but call not finalized",
                    auto_repairable=False) for r in rows]


def detect_duplicate_debits(db, tenant_id=None):
    """Class D: more than one debit row for the same call."""
    conds = ["type = 'debit'", "ref_id LIKE 'call:%%'"]
    params: list[Any] = []
    if tenant_id:
        conds.append("tenant_id = %s"); params.append(tenant_id)
    sql = (
        "SELECT tenant_id, ref_id, COUNT(*) AS cnt, SUM(amount) AS total "
        "FROM wallet_transactions WHERE " + " AND ".join(conds) +
        " GROUP BY tenant_id, ref_id HAVING COUNT(*) > 1"
    )
    rows = db.query(sql, params)
    return [Anomaly("D", None, r["tenant_id"],
                    f"ref={r['ref_id']} has {r['cnt']} debit rows total={r['total']}",
                    auto_repairable=False) for r in rows]


def detect_charge_amount_mismatch(db, tenant_id=None, call_id=None):
    """Class E: calls.charge != wallet_transactions.amount for the matching debit."""
    conds = ["c.billing_finalized = TRUE", "c.charge > 0",
             "EXISTS (SELECT 1 FROM wallet_transactions wt WHERE wt.ref_id = 'call:' || c.id::text AND wt.type='debit' AND wt.amount != c.charge)"]
    params: list[Any] = []
    if tenant_id:
        conds.append("c.tenant_id = %s"); params.append(tenant_id)
    if call_id:
        conds.append("c.id = %s"); params.append(call_id)
    sql = "SELECT c.id, c.tenant_id, c.charge FROM calls c WHERE " + " AND ".join(conds)
    rows = db.query(sql, params)
    return [Anomaly("E", r["id"], r["tenant_id"],
                    f"calls.charge={r['charge']} differs from ledger debit",
                    auto_repairable=False) for r in rows]


def detect_wallet_balance_drift(db, tenant_id=None):
    """Class F: wallet.balance != sum(credits+refunds) - sum(debits) from ledger."""
    conds = []
    params: list[Any] = []
    if tenant_id:
        conds.append("w.tenant_id = %s"); params.append(tenant_id)
    where = " WHERE " + " AND ".join(conds) if conds else ""
    sql = f"""
        SELECT w.tenant_id, w.balance AS wallet_balance,
               COALESCE(SUM(CASE WHEN wt.type IN ('credit','refund') THEN wt.amount ELSE 0 END), 0)
             - COALESCE(SUM(CASE WHEN wt.type = 'debit' THEN wt.amount ELSE 0 END), 0)
               AS ledger_balance
        FROM wallets w
        LEFT JOIN wallet_transactions wt ON wt.tenant_id = w.tenant_id
        {where}
        GROUP BY w.tenant_id, w.balance
        HAVING ABS(w.balance - (
            COALESCE(SUM(CASE WHEN wt.type IN ('credit','refund') THEN wt.amount ELSE 0 END), 0)
          - COALESCE(SUM(CASE WHEN wt.type = 'debit' THEN wt.amount ELSE 0 END), 0)
        )) > 0.0001
    """
    rows = db.query(sql, params)
    return [Anomaly("F", None, r["tenant_id"],
                    f"wallet={r['wallet_balance']} ledger={r['ledger_balance']}",
                    auto_repairable=False) for r in rows]


def detect_refund_without_debit(db, tenant_id=None):
    """Class G: refund row with ref_id='refund:call:X' but no debit for call X."""
    conds = ["type = 'refund'", "ref_id LIKE 'refund:call:%%'",
             "NOT EXISTS (SELECT 1 FROM wallet_transactions d WHERE d.tenant_id = wt.tenant_id AND d.ref_id = substring(wt.ref_id from 8) AND d.type = 'debit')"]
    params: list[Any] = []
    if tenant_id:
        conds.append("wt.tenant_id = %s"); params.append(tenant_id)
    sql = "SELECT wt.id, wt.tenant_id, wt.ref_id, wt.amount FROM wallet_transactions wt WHERE " + " AND ".join(conds)
    rows = db.query(sql, params)
    return [Anomaly("G", None, r["tenant_id"],
                    f"refund txn={r['id']} ref={r['ref_id']} has no matching debit",
                    auto_repairable=False) for r in rows]


def detect_duplicate_refunds(db, tenant_id=None):
    """Class H: more than one refund for the same call."""
    conds = ["type = 'refund'", "ref_id LIKE 'refund:call:%%'"]
    params: list[Any] = []
    if tenant_id:
        conds.append("tenant_id = %s"); params.append(tenant_id)
    sql = (
        "SELECT tenant_id, ref_id, COUNT(*) AS cnt FROM wallet_transactions WHERE " + " AND ".join(conds) +
        " GROUP BY tenant_id, ref_id HAVING COUNT(*) > 1"
    )
    rows = db.query(sql, params)
    return [Anomaly("H", None, r["tenant_id"],
                    f"ref={r['ref_id']} has {r['cnt']} refund rows",
                    auto_repairable=False) for r in rows]


def detect_tenant_mismatch(db, tenant_id=None):
    """Class I: wallet_transactions.tenant_id != calls.tenant_id for call debit."""
    conds = ["wt.type = 'debit'", "wt.ref_id LIKE 'call:%%'",
             "EXISTS (SELECT 1 FROM calls c WHERE c.id::text = substring(wt.ref_id from 6) AND c.tenant_id != wt.tenant_id)"]
    params: list[Any] = []
    if tenant_id:
        conds.append("wt.tenant_id = %s"); params.append(tenant_id)
    sql = "SELECT wt.id, wt.tenant_id, wt.ref_id FROM wallet_transactions wt WHERE " + " AND ".join(conds)
    rows = db.query(sql, params)
    return [Anomaly("I", None, r["tenant_id"],
                    f"txn={r['id']} ref={r['ref_id']} tenant mismatch",
                    auto_repairable=False) for r in rows]


def detect_dispatch_failed_charged(db, tenant_id=None):
    """Class J: dispatch_failed attempt linked to a charged call."""
    conds = ["oca.dispatch_status = 'dispatch_failed'",
             "c.charge > 0", "c.billing_finalized = TRUE"]
    params: list[Any] = []
    if tenant_id:
        conds.append("oca.tenant_id = %s"); params.append(tenant_id)
    sql = ("SELECT oca.id, oca.tenant_id, oca.call_id, c.charge "
           "FROM order_call_attempts oca JOIN calls c ON c.id = oca.call_id "
           "WHERE " + " AND ".join(conds))
    try:
        rows = db.query(sql, params)
    except Exception:
        rows = []
    return [Anomaly("J", r["call_id"], r["tenant_id"],
                    f"attempt={r['id']} dispatch_failed but charge={r['charge']}",
                    auto_repairable=False) for r in rows]


def detect_missing_rate_snapshot(db, tenant_id=None):
    """Class K: billed call missing rate_snapshot (best-effort warning)."""
    conds = ["billing_finalized = TRUE", "charge > 0", "rate_snapshot IS NULL"]
    params: list[Any] = []
    if tenant_id:
        conds.append("tenant_id = %s"); params.append(tenant_id)
    try:
        rows = db.query("SELECT id, tenant_id, charge FROM calls WHERE " + " AND ".join(conds) + " LIMIT 100", params)
    except Exception:
        rows = []
    return [Anomaly("K", r["id"], r["tenant_id"],
                    f"billed charge={r['charge']} but rate_snapshot IS NULL",
                    auto_repairable=False) for r in rows]


def _run_all_detectors(db, *, tenant_id=None, call_id=None, from_date=None, to_date=None) -> list[Anomaly]:
    anomalies: list[Anomaly] = []
    anomalies += detect_unfinalized_terminal_calls(db, tenant_id, call_id, from_date, to_date)
    anomalies += detect_billed_no_ledger(db, tenant_id, call_id)
    anomalies += detect_ledger_no_billing_flag(db, tenant_id)
    anomalies += detect_duplicate_debits(db, tenant_id)
    anomalies += detect_charge_amount_mismatch(db, tenant_id, call_id)
    anomalies += detect_wallet_balance_drift(db, tenant_id)
    anomalies += detect_refund_without_debit(db, tenant_id)
    anomalies += detect_duplicate_refunds(db, tenant_id)
    anomalies += detect_tenant_mismatch(db, tenant_id)
    anomalies += detect_dispatch_failed_charged(db, tenant_id)
    anomalies += detect_missing_rate_snapshot(db, tenant_id)
    return anomalies


# ── Apply path ────────────────────────────────────────────────────────────────

def _apply_class_a(db, anomaly: Anomaly, dry_run: bool) -> str:
    """Re-run finalize_call_billing() for unfinalized terminal calls."""
    if dry_run:
        return "dry-run: would call finalize_call_billing"
    from app.services.billing_service import finalize_call_billing
    result = finalize_call_billing(anomaly.call_id, source="reconcile_apply")
    if result["billed"]:
        return f"billed charge={result['charge']}"
    if result.get("already_finalized"):
        return "already_finalized (concurrent finalizer beat us)"
    return f"not_billed reason={result.get('reason')}"


# ── Output ────────────────────────────────────────────────────────────────────

def _print_text(anomalies: list[Anomaly], dry_run: bool, apply_results: dict[int, str]) -> None:
    mode = "DRY RUN" if dry_run else "APPLYING"
    print(f"\n{mode} — {len(anomalies)} anomaly(ies)\n")
    if not anomalies:
        print("No anomalies detected.")
        return
    print(f"{'CLASS':<6} {'CALL_ID':>8} {'TENANT':>7} {'AUTO':>5}  DETAIL")
    print("-" * 90)
    for i, a in enumerate(anomalies):
        auto = "yes" if a.auto_repairable else "no"
        call_str = str(a.call_id) if a.call_id is not None else "-"
        tenant_str = str(a.tenant_id) if a.tenant_id is not None else "-"
        result = apply_results.get(i, "")
        result_sfx = f"  → {result}" if result else ""
        print(f"{a.anomaly_class:<6} {call_str:>8} {tenant_str:>7} {auto:>5}  {a.detail}{result_sfx}")
    print("-" * 90)
    non_repairable = sum(1 for a in anomalies if not a.auto_repairable)
    if non_repairable:
        print(f"\n{non_repairable} anomaly(ies) require manual operator review (auto=no).")


def _print_json(anomalies: list[Anomaly], dry_run: bool, apply_results: dict[int, str]) -> None:
    output = {
        "dry_run": dry_run,
        "anomaly_count": len(anomalies),
        "anomalies": [
            {**asdict(a), "apply_result": apply_results.get(i)}
            for i, a in enumerate(anomalies)
        ],
    }
    print(_json.dumps(output, default=str, indent=2))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile call billing anomalies.")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true", default=False,
                        help="Apply repairs. Requires explicit flag. Deducts wallets.")
    parser.add_argument("--call-id", type=int)
    parser.add_argument("--tenant-id", type=int)
    parser.add_argument("--from-date", help="YYYY-MM-DD inclusive")
    parser.add_argument("--to-date", help="YYYY-MM-DD inclusive")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="Max repairs per --apply run (default 100).")
    parser.add_argument("--json", dest="output_json", action="store_true",
                        help="Machine-readable JSON output.")
    args = parser.parse_args()

    if args.apply:
        args.dry_run = False

    app = _build_app()
    with app.app_context():
        from app.db import db

        anomalies = _run_all_detectors(
            db,
            tenant_id=args.tenant_id,
            call_id=args.call_id,
            from_date=args.from_date,
            to_date=args.to_date,
        )

        apply_results: dict[int, str] = {}

        if not args.dry_run:
            # Apply only class-A (unfinalized terminal calls) — the only auto-repairable class.
            repairable = [(i, a) for i, a in enumerate(anomalies) if a.auto_repairable]
            if len(repairable) > args.batch_size:
                print(f"Batch size limit: applying first {args.batch_size} of {len(repairable)} repairable anomalies.")
                repairable = repairable[: args.batch_size]

            for i, anomaly in repairable:
                try:
                    apply_results[i] = _apply_class_a(db, anomaly, dry_run=False)
                except Exception as exc:
                    apply_results[i] = f"ERROR: {type(exc).__name__}"

        if args.output_json:
            _print_json(anomalies, args.dry_run, apply_results)
        else:
            _print_text(anomalies, args.dry_run, apply_results)

        if not args.dry_run:
            n_applied = sum(1 for r in apply_results.values() if r and not r.startswith("ERROR"))
            n_errors = sum(1 for r in apply_results.values() if r and r.startswith("ERROR"))
            print(f"\nApply complete — repaired: {n_applied}, errors: {n_errors}")


if __name__ == "__main__":
    main()
