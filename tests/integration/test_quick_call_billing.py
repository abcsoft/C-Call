# tests/integration/test_quick_call_billing.py
"""
Quick Call wallet billing tests.

Tests the atomic finalize_call_billing() contract:
- Wallet deduction, ledger insert, and calls.billing_finalized all commit
  in one transaction.
- Idempotency: duplicate calls return already_finalized, zero extra debit.
- Billable outcome matrix: canceled-with-talk, no-answer, busy, failed,
  dispatch-failed.
- Rate snapshot: snapshot at call time is used, not live rate.
- Tenant isolation.
- Concurrent thread race: exactly one debit when two threads race.
- Insufficient balance: no partial state.
- Refund: idempotent, one credit.
"""
from __future__ import annotations

import secrets as _s
import threading
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.services.billing_service import finalize_call_billing, refund_call_charge
from app.services.rate_service import calculate_charge_and_duration
from app.services.wallet_service import get_balance


# ── helpers ──────────────────────────────────────────────────────────────────

def _tenant(db, *, balance="100.00", rate_per_min="0.80", pulse=60):
    """Create minimal tenant+wallet, return tenant_id."""
    from app.services.auth_service import register_tenant
    tenant, user = register_tenant({
        "business_name": f"BillingTest_{_s.token_hex(3)}",
        "email": f"bill_{_s.token_hex(3)}@test.example",
        "password": "Password123!",
        "phone": f"0171{_s.token_hex(3)[:7]}",
    })
    tid = tenant["id"]
    db.execute("UPDATE tenants SET status='active' WHERE id=%s", (tid,))
    db.execute("UPDATE wallets SET balance=%s WHERE tenant_id=%s", (balance, tid))
    db.execute(
        "INSERT INTO customer_rates (tenant_id, rate_per_min, rate_per_call, min_charge,"
        " billing_pulse_seconds, minimum_billable_seconds, charge_failed_calls)"
        " VALUES (%s, %s, 0.00, %s, %s, %s, false)"
        " ON CONFLICT (tenant_id) DO UPDATE SET rate_per_min=%s, rate_per_call=0.00,"
        " min_charge=%s, billing_pulse_seconds=%s, minimum_billable_seconds=%s, charge_failed_calls=false",
        (tid, rate_per_min, rate_per_min, pulse, pulse, rate_per_min, rate_per_min, pulse, pulse),
    )
    try:
        from app.extensions import redis_ext
        redis_ext._client.delete(f"tenant:rate:{tid}")
    except Exception:
        pass
    return tid


def _call(db, tenant_id, *, talk_seconds=0, status="completed", dispatch_status=None):
    """Insert a calls row and return call_id."""
    call_id = db.insert_returning_id(
        "INSERT INTO calls (tenant_id, number, status, talk_seconds, total_seconds,"
        " started_at, ended_at, call_type)"
        " VALUES (%s, '01700000000', %s, %s, %s, '2026-07-20 12:00:00+00', '2026-07-20 12:01:00+00', 'ivr')"
        " RETURNING id",
        (tenant_id, status, talk_seconds, talk_seconds + 5),
    )
    # Insert a matching order_call_attempts row so the UPDATE in finalize
    # has something to find (doesn't need to exist for the billing contract,
    # but mirrors real usage).
    if dispatch_status:
        db.execute(
            "INSERT INTO orders (tenant_id, external_order_id, phone, order_status)"
            " VALUES (%s, %s, '01700000001', 'CALLING') ON CONFLICT DO NOTHING",
            (tenant_id, f"dispatch-test-{call_id}"),
        )
        order_row = db.query_one(
            "SELECT id FROM orders WHERE tenant_id=%s AND external_order_id=%s",
            (tenant_id, f"dispatch-test-{call_id}"),
        )
        if order_row:
            db.execute(
                "INSERT INTO order_call_attempts (order_id, tenant_id, call_id, attempt_no, dispatch_status)"
                " VALUES (%s, %s, %s, 1, %s) ON CONFLICT DO NOTHING",
                (order_row["id"], tenant_id, call_id, dispatch_status),
            )
    return call_id


def _ledger_rows(db, tenant_id):
    return db.query(
        "SELECT * FROM wallet_transactions WHERE tenant_id=%s ORDER BY id", (tenant_id,)
    )


# ── Rate logic unit tests ─────────────────────────────────────────────────────

class TestCalculateChargeAndDuration:

    def test_27s_canceled_is_billable(self, app, db_conn):
        """27s talk + status=canceled → billed (connection evidence)."""
        from app.db import db
        tid = _tenant(db, rate_per_min="1.20", pulse=60)
        charge, billable_s = calculate_charge_and_duration(27, tid, "canceled")
        assert charge > 0, f"27s canceled must be billable, got {charge}"
        assert billable_s >= 27

    def test_42s_canceled_charge_correct(self, app, db_conn):
        """42s @ 1.20/min, 60s pulse → ceil(42/60)*60=60s → ৳1.20."""
        from app.db import db
        tid = _tenant(db, rate_per_min="1.20", pulse=60)
        charge, billable_s = calculate_charge_and_duration(42, tid, "canceled")
        assert charge == Decimal("1.2000"), f"Expected 1.2000 got {charge}"
        assert billable_s == 60

    def test_27s_completed_still_billable(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, rate_per_min="0.80", pulse=60)
        charge, _ = calculate_charge_and_duration(27, tid, "completed")
        assert charge > 0

    def test_no_answer_zero_charge(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, rate_per_min="0.80", pulse=60)
        charge, s = calculate_charge_and_duration(0, tid, "no_answer")
        assert charge == Decimal("0.0000")
        assert s == 0

    def test_busy_zero_charge(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, rate_per_min="0.80", pulse=60)
        charge, s = calculate_charge_and_duration(0, tid, "busy")
        assert charge == Decimal("0.0000")

    def test_failed_zero_charge(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, rate_per_min="0.80", pulse=60)
        charge, s = calculate_charge_and_duration(0, tid, "failed")
        assert charge == Decimal("0.0000")

    def test_canceled_zero_talk_zero_charge(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, rate_per_min="0.80", pulse=60)
        charge, s = calculate_charge_and_duration(0, tid, "canceled")
        assert charge == Decimal("0.0000")

    def test_decimal_rounding_correct(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, rate_per_min="0.8333", pulse=1)
        charge, _ = calculate_charge_and_duration(7, tid, "canceled")
        assert str(charge).count(".") == 1
        assert len(str(charge).split(".")[1]) <= 4


# ── finalize_call_billing integration tests ───────────────────────────────────

class TestFinalizeBilling:

    def test_27s_canceled_wallet_debited(self, app, db_conn):
        """27s canceled → wallet decreases by correct charge."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="1.20", pulse=60)
        before = get_balance(tid)
        call_id = _call(db, tid, talk_seconds=27, status="canceled")
        result = finalize_call_billing(call_id)
        assert result["billed"] is True, f"Must be billed: {result}"
        assert result["charge"] == Decimal("1.2000")
        after = get_balance(tid)
        assert after == before - Decimal("1.2000"), f"Wallet not reduced: {before} → {after}"

    def test_42s_canceled_wallet_debited(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="1.20", pulse=60)
        call_id = _call(db, tid, talk_seconds=42, status="canceled")
        result = finalize_call_billing(call_id)
        assert result["charge"] == Decimal("1.2000")

    def test_wallet_decreases_exactly_once(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="100.00", rate_per_min="0.80", pulse=60)
        before = get_balance(tid)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        after = get_balance(tid)
        assert before - after == Decimal("0.8000")

    def test_wallet_ledger_row_created_exactly_once(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=30, status="canceled")
        finalize_call_billing(call_id)
        rows = _ledger_rows(db, tid)
        assert len(rows) == 1, f"Expected 1 ledger row, got {len(rows)}"

    def test_cdr_charge_equals_ledger_amount(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=30, status="canceled")
        result = finalize_call_billing(call_id)
        call_row = db.query_one("SELECT charge FROM calls WHERE id=%s", (call_id,))
        ledger = _ledger_rows(db, tid)
        assert len(ledger) == 1
        assert call_row["charge"] == ledger[0]["amount"]
        assert call_row["charge"] == result["charge"]

    def test_duplicate_completion_does_not_debit_twice(self, app, db_conn):
        """Second finalize_call_billing is a no-op (idempotent)."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        before = get_balance(tid)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        r1 = finalize_call_billing(call_id)
        r2 = finalize_call_billing(call_id)
        assert r1["billed"] is True
        assert r2["billed"] is False
        assert r2["already_finalized"] is True
        assert r2["reason"] == "already_billed"
        after = get_balance(tid)
        assert before - after == r1["charge"], "Wallet debited more than once"

    def test_worker_retry_does_not_debit_twice(self, app, db_conn):
        """3x finalize on same call_id → still only one ledger row."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        for _ in range(3):
            finalize_call_billing(call_id)
        rows = _ledger_rows(db, tid)
        assert len(rows) == 1, f"Must have exactly one ledger row, got {len(rows)}"

    def test_unanswered_quick_call_charges_zero(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        before = get_balance(tid)
        call_id = _call(db, tid, talk_seconds=0, status="no_answer")
        result = finalize_call_billing(call_id)
        assert result["charge"] == Decimal("0.0000")
        assert get_balance(tid) == before

    def test_busy_call_charges_zero(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        before = get_balance(tid)
        call_id = _call(db, tid, talk_seconds=0, status="busy")
        finalize_call_billing(call_id)
        assert get_balance(tid) == before

    def test_failed_call_charges_zero(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        before = get_balance(tid)
        call_id = _call(db, tid, talk_seconds=0, status="failed")
        finalize_call_billing(call_id)
        assert get_balance(tid) == before

    def test_canceled_with_talk_time_bills_on_connection_evidence(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        before = get_balance(tid)
        call_id = _call(db, tid, talk_seconds=45, status="canceled")
        result = finalize_call_billing(call_id)
        assert result["billed"] is True
        assert result["charge"] > 0
        assert get_balance(tid) < before

    def test_no_answer_with_charge_failed_calls_true(self, app, db_conn):
        """no_answer + charge_failed_calls=true → minimum charge applied."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        db.execute("UPDATE customer_rates SET charge_failed_calls=true WHERE tenant_id=%s", (tid,))
        try:
            from app.extensions import redis_ext
            redis_ext._client.delete(f"tenant:rate:{tid}")
        except Exception:
            pass
        before = get_balance(tid)
        call_id = _call(db, tid, talk_seconds=0, status="no_answer")
        result = finalize_call_billing(call_id)
        # Policy says charge for failed calls: minimum charge should apply
        assert result["billed"] is True, f"charge_failed_calls=true must bill: {result}"
        assert get_balance(tid) < before

    def test_insufficient_balance_does_not_update_cdr(self, app, db_conn):
        """Insufficient balance → deduction fails → CDR charge stays 0, billing_finalized False."""
        from app.db import db
        tid = _tenant(db, balance="0.10", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        result = finalize_call_billing(call_id)
        assert result["billed"] is False
        assert result["reason"] == "insufficient_balance"
        row = db.query_one("SELECT billing_finalized, charge FROM calls WHERE id=%s", (call_id,))
        assert row["billing_finalized"] is False or row["billing_finalized"] == 0
        assert row["charge"] == Decimal("0.0000") or row["charge"] is None

    def test_tenant_isolation(self, app, db_conn):
        """Billing tenant A does not touch tenant B's wallet."""
        from app.db import db
        tid_a = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        tid_b = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        before_b = get_balance(tid_b)
        call_a = _call(db, tid_a, talk_seconds=60, status="canceled")
        finalize_call_billing(call_a)
        assert get_balance(tid_b) == before_b, "Tenant B wallet must be untouched"

    def test_expected_tenant_id_mismatch_rejected(self, app, db_conn):
        """finalize_call_billing with wrong expected_tenant_id → rejected, no financial mutation."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        before = get_balance(tid)
        result = finalize_call_billing(call_id, expected_tenant_id=tid + 99999)
        assert result["billed"] is False
        assert result["reason"] == "tenant_mismatch"
        assert get_balance(tid) == before
        row = db.query_one("SELECT billing_finalized FROM calls WHERE id=%s", (call_id,))
        assert not row["billing_finalized"]

    def test_rate_snapshot_used_not_retroactive_rate(self, app, db_conn):
        """Charge uses rate at billing time; rate change after billing is blocked by idempotency."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        result = finalize_call_billing(call_id)
        billed_charge = result["charge"]
        db.execute("UPDATE customer_rates SET rate_per_min=9.99 WHERE tenant_id=%s", (tid,))
        try:
            from app.extensions import redis_ext
            redis_ext._client.delete(f"tenant:rate:{tid}")
        except Exception:
            pass
        r2 = finalize_call_billing(call_id)
        assert r2["billed"] is False
        assert r2["charge"] == billed_charge

    def test_order_call_billing_still_passes(self, app, db_conn):
        """Regression: order confirmation calls (status=completed) still billed."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        before = get_balance(tid)
        call_id = _call(db, tid, talk_seconds=60, status="completed")
        result = finalize_call_billing(call_id)
        assert result["billed"] is True
        assert get_balance(tid) < before

    def test_revenue_report_includes_charge(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        row = db.query_one(
            "SELECT SUM(charge) AS total FROM calls WHERE tenant_id=%s AND charge > 0", (tid,)
        )
        assert row["total"] is not None and row["total"] > 0

    def test_atomicity_wallet_and_billing_flag_together(self, app, db_conn):
        """Atomicity: billing_finalized=TRUE and ledger row always present together."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        result = finalize_call_billing(call_id)
        assert result["billed"] is True

        row = db.query_one(
            "SELECT billing_finalized, wallet_transaction_id FROM calls WHERE id=%s", (call_id,)
        )
        ledger = _ledger_rows(db, tid)
        # Must always be set together
        assert row["billing_finalized"] is True
        assert row["wallet_transaction_id"] is not None
        assert len(ledger) == 1

    def test_missing_wallet_leaves_call_unfinalized(self, app, db_conn):
        """wallet_not_found path: billing_finalized stays False, no charge set."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")

        # Remove wallet to simulate missing wallet condition
        db.execute("DELETE FROM wallet_transactions WHERE tenant_id=%s", (tid,))
        db.execute("DELETE FROM wallets WHERE tenant_id=%s", (tid,))

        result = finalize_call_billing(call_id)
        assert result["billed"] is False
        assert result["reason"] == "wallet_not_found"
        row = db.query_one("SELECT billing_finalized, charge FROM calls WHERE id=%s", (call_id,))
        assert not row["billing_finalized"]
        assert row["charge"] is None or row["charge"] == Decimal("0.0000")

    def test_concurrent_finalizers_exactly_one_debit(self, app, db_conn):
        """Two concurrent threads racing to finalize the same call → exactly one debit."""
        from app.db import db
        import psycopg

        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")

        results = []
        errors = []

        def run_finalize():
            with app.app_context():
                try:
                    r = finalize_call_billing(call_id)
                    results.append(r)
                except Exception as exc:
                    errors.append(exc)

        t1 = threading.Thread(target=run_finalize)
        t2 = threading.Thread(target=run_finalize)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Unexpected errors: {errors}"

        billed_results = [r for r in results if r.get("billed")]
        already_results = [r for r in results if r.get("already_finalized")]

        # Exactly one must have billed; the other must have seen already_finalized
        # OR one got unique constraint violation (rolled back — billed=False)
        assert len(billed_results) <= 1, "At most one thread may bill"
        ledger = _ledger_rows(db, tid)
        assert len(ledger) == 1, f"Exactly one ledger row required, got {len(ledger)}"
        final_balance = get_balance(tid)
        assert final_balance == Decimal("50.00") - ledger[0]["amount"]

    def test_rate_snapshot_on_call_overrides_live_rate(self, app, db_conn):
        """Call with explicit rate_snapshot uses snapshot rate, not live rate."""
        import json
        from app.db import db
        from app.services.wallet_service import credit
        tid = _tenant(db, balance="0.00", rate_per_min="0.80", pulse=60)
        credit(tid, Decimal("10.00"), "seed")
        from app.services.rate_service import POLICY_VERSION
        snap = {
            "policy_version": POLICY_VERSION,
            "tenant_id": tid,
            "rate_per_min": "0.50",
            "rate_per_call": "0.00",
            "min_charge": "0.50",
            "billing_pulse_seconds": 60,
            "minimum_billable_seconds": 60,
            "charge_failed_calls": False,
            "currency": "BDT",
        }
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, number, status, talk_seconds, rate_snapshot, started_at)"
            " VALUES (%s, '01700000000', 'completed', 60, %s, '2026-07-20 12:00:00+00') RETURNING id",
            (tid, json.dumps(snap)),
        )
        result = finalize_call_billing(call_id)
        assert result["billed"] is True
        assert result["charge"] == Decimal("0.5000"), f"Snapshot rate should be used: {result['charge']}"


# ── Refund tests ──────────────────────────────────────────────────────────────

class TestRefundCallCharge:

    def test_refund_credits_wallet(self, app, db_conn):
        """Successful refund credits wallet by the billed amount."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        before_refund = get_balance(tid)
        r = refund_call_charge(call_id, reason_code="test_refund", source="test")
        assert r["refunded"] is True
        assert r["refund_amount"] == Decimal("0.8000")
        assert get_balance(tid) == before_refund + Decimal("0.8000")

    def test_duplicate_refund_is_idempotent(self, app, db_conn):
        """Second refund_call_charge for same call → already_refunded, no extra credit."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        r1 = refund_call_charge(call_id, reason_code="test_r", source="test")
        balance_after_first = get_balance(tid)
        r2 = refund_call_charge(call_id, reason_code="test_r", source="test")
        assert r1["refunded"] is True
        assert r2["refunded"] is False
        assert r2["already_refunded"] is True
        assert get_balance(tid) == balance_after_first

    def test_refund_without_original_debit_rejected(self, app, db_conn):
        """Cannot refund a call that was not billed (zero charge)."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=0, status="no_answer")
        finalize_call_billing(call_id)
        r = refund_call_charge(call_id, reason_code="test_r", source="test")
        assert r["refunded"] is False
        assert r["reason"] in ("no_original_debit",)


# ── Reconciliation tests ──────────────────────────────────────────────────────

class TestReconciliation:

    def test_dry_run_detects_unfinalized_terminal_call(self, app, db_conn):
        """Reconciliation class A: unfinalized terminal call detected, not mutated."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=45, status="canceled")
        # Call is terminal (canceled) but billing_finalized=FALSE by default
        before = get_balance(tid)

        from scripts.reconcile_call_billing import detect_unfinalized_terminal_calls
        anomalies = detect_unfinalized_terminal_calls(db, tenant_id=tid, call_id=call_id)
        assert any(a.call_id == call_id for a in anomalies), "Unfinalized terminal call must be detected"
        # Dry run: wallet unchanged
        assert get_balance(tid) == before

    def test_dry_run_does_not_mutate_data(self, app, db_conn):
        """Running detection functions never writes to DB."""
        from app.db import db
        from scripts.reconcile_call_billing import _run_all_detectors
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        before = get_balance(tid)
        _run_all_detectors(db, tenant_id=tid)
        assert get_balance(tid) == before

    def test_reconciliation_apply_bills_unfinalized_call(self, app, db_conn):
        """Reconciliation apply path (class A) finalizes an unfinalized terminal call."""
        from app.db import db
        from scripts.reconcile_call_billing import detect_unfinalized_terminal_calls, _apply_class_a

        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        before = get_balance(tid)

        anomalies = detect_unfinalized_terminal_calls(db, tenant_id=tid, call_id=call_id)
        assert anomalies, "Must detect the unfinalized call"

        # Apply
        for a in anomalies:
            if a.call_id == call_id:
                result_str = _apply_class_a(db, a, dry_run=False)
                assert "billed" in result_str or "already" in result_str, f"Unexpected: {result_str}"

        # Wallet should have been debited
        assert get_balance(tid) < before

    def test_json_output_has_correct_shape(self, app, db_conn):
        """_run_all_detectors returns a list of Anomaly objects with required fields."""
        from app.db import db
        from scripts.reconcile_call_billing import _run_all_detectors, Anomaly
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        anomalies = _run_all_detectors(db, tenant_id=tid)
        for a in anomalies:
            assert isinstance(a, Anomaly)
            assert a.anomaly_class in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K")


# ── Phase 3: result-contract correctness tests ────────────────────────────────

class TestResultContract:
    """Verify every error-path return dict carries the real tenant_id, not None."""

    def test_not_terminal_returns_tenant_id(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=0, status="calling")  # non-terminal
        r = finalize_call_billing(call_id)
        assert r["billed"] is False
        assert r["error_code"] == "not_terminal"
        assert r["tenant_id"] == tid, f"tenant_id must be {tid}, got {r['tenant_id']}"

    def test_wallet_not_found_returns_tenant_id(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="completed")
        # Delete wallet to trigger wallet_not_found
        db.execute("DELETE FROM wallet_transactions WHERE tenant_id=%s", (tid,))
        db.execute("DELETE FROM wallets WHERE tenant_id=%s", (tid,))
        r = finalize_call_billing(call_id)
        assert r["billed"] is False
        assert r["error_code"] == "wallet_not_found"
        assert r["tenant_id"] == tid, f"tenant_id must be {tid}, got {r['tenant_id']}"

    def test_insufficient_balance_returns_tenant_id(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="0.01", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="completed")
        r = finalize_call_billing(call_id)
        assert r["billed"] is False
        assert r["error_code"] == "insufficient_balance"
        assert r["tenant_id"] == tid

    def test_tenant_mismatch_returns_actual_tenant_id(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="completed")
        r = finalize_call_billing(call_id, expected_tenant_id=tid + 9999)
        assert r["billed"] is False
        assert r["error_code"] == "tenant_mismatch"
        assert r["tenant_id"] == tid, f"must return actual tenant {tid}, got {r['tenant_id']}"

    def test_call_not_found_returns_none_tenant_id(self, app, db_conn):
        r = finalize_call_billing(999999999)
        assert r["billed"] is False
        assert r["error_code"] == "call_not_found"
        assert r["tenant_id"] is None  # intentional — no call, no tenant

    def test_refund_no_original_debit_returns_tenant_id(self, app, db_conn):
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=0, status="no_answer")
        finalize_call_billing(call_id)  # charge=0 → not billed
        r = refund_call_charge(call_id, reason_code="test", source="test")
        assert r["refunded"] is False
        assert r["reason"] == "no_original_debit"
        assert r["tenant_id"] == tid

    def test_refund_wallet_not_found_returns_tenant_id(self, app, db_conn):
        from app.db import db
        from decimal import Decimal
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")

        # We need: billing_finalized=TRUE AND debit row exists (steps 2+3 pass)
        # BUT tenant's wallet is gone (step 6 returns wallet_not_found).
        # wallet_transactions.wallet_id → wallets.id (NOT NULL FK) prevents deleting
        # the wallet while any transaction row references it.  Work-around: insert
        # the debit row manually pointing to a SECOND tenant's wallet, then delete
        # our tenant's wallet (no FK reference remains to it).
        tid2 = _tenant(db, balance="0.00", rate_per_min="0.80", pulse=60)
        wallet2 = db.query_one("SELECT id FROM wallets WHERE tenant_id=%s", (tid2,))
        charge = Decimal("0.80")
        txn_id = db.insert_returning_id(
            "INSERT INTO wallet_transactions"
            "  (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)"
            " VALUES (%s, %s, 'debit', %s, 49.20, %s, %s) RETURNING id",
            (wallet2["id"], tid, charge, f"Call {call_id}", f"call:{call_id}"),
        )
        db.execute(
            "UPDATE calls SET billing_finalized=TRUE, charge=%s, wallet_transaction_id=%s"
            " WHERE id=%s",
            (charge, txn_id, call_id),
        )
        db.execute("DELETE FROM wallets WHERE tenant_id=%s", (tid,))
        r = refund_call_charge(call_id, reason_code="test", source="test")
        assert r["refunded"] is False
        assert r["reason"] == "wallet_not_found"
        assert r["tenant_id"] == tid

    def test_refund_call_not_found_returns_none_tenant_id(self, app, db_conn):
        r = refund_call_charge(999999999, reason_code="test", source="test")
        assert r["refunded"] is False
        assert r["reason"] == "call_not_found"
        assert r["tenant_id"] is None


# ── Phase 6: durable refund state tests ──────────────────────────────────────

class TestDurableRefundState:

    def test_successful_refund_sets_durable_state(self, app, db_conn):
        """After refund, calls.refund_status='refunded' and refund_transaction_id set."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        r = refund_call_charge(call_id, reason_code="test_durable", source="test")
        assert r["refunded"] is True
        row = db.query_one("SELECT refund_status, refund_transaction_id, refunded_at, refund_reason_code FROM calls WHERE id=%s", (call_id,))
        assert row["refund_status"] == "refunded"
        assert row["refund_transaction_id"] == r["refund_transaction_id"]
        assert row["refunded_at"] is not None
        assert row["refund_reason_code"] == "test_durable"

    def test_duplicate_refund_blocked_by_durable_state(self, app, db_conn):
        """Durable state check blocks second refund before even looking at ledger."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        r1 = refund_call_charge(call_id, reason_code="first", source="test")
        balance_after = get_balance(tid)
        r2 = refund_call_charge(call_id, reason_code="second", source="test")
        assert r2["refunded"] is False
        assert r2["already_refunded"] is True
        assert r2["refund_transaction_id"] == r1["refund_transaction_id"]
        assert get_balance(tid) == balance_after  # no extra credit

    def test_ledger_exists_but_durable_state_missing_blocked(self, app, db_conn):
        """Ledger refund row exists but calls.refund_status='none' → still blocked, not double-credited."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        # Manually insert ledger refund row WITHOUT setting durable state
        wallet = db.query_one("SELECT id, balance FROM wallets WHERE tenant_id=%s", (tid,))
        db.execute(
            "INSERT INTO wallet_transactions (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)"
            " VALUES (%s, %s, 'refund', 0.80, %s, 'manual test', %s)",
            (wallet["id"], tid, wallet["balance"], f"refund:call:{call_id}"),
        )
        balance_before = get_balance(tid)
        r = refund_call_charge(call_id, reason_code="test", source="test")
        assert r["refunded"] is False
        assert r["already_refunded"] is True
        assert get_balance(tid) == balance_before  # wallet NOT credited again

    def test_durable_state_exists_but_ledger_missing_not_double_billed(self, app, db_conn):
        """calls.refund_status='refunded' but no ledger row → already_refunded, no new credit."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        # Force durable state to 'refunded' without actual ledger row
        db.execute(
            "UPDATE calls SET refund_status='refunded', refund_transaction_id=1, refunded_at=now()"
            " WHERE id=%s",
            (call_id,),
        )
        balance_before = get_balance(tid)
        r = refund_call_charge(call_id, reason_code="test", source="test")
        assert r["refunded"] is False
        assert r["already_refunded"] is True
        assert get_balance(tid) == balance_before

    def test_missing_wallet_during_refund_leaves_call_unrefunded(self, app, db_conn):
        """Wallet missing at refund time: call stays refund_status='none', no ledger row."""
        from app.db import db
        from decimal import Decimal
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")

        # Same FK work-around as test_refund_wallet_not_found_returns_tenant_id:
        # point the debit row at a second tenant's wallet so ours can be deleted.
        tid2 = _tenant(db, balance="0.00", rate_per_min="0.80", pulse=60)
        wallet2 = db.query_one("SELECT id FROM wallets WHERE tenant_id=%s", (tid2,))
        charge = Decimal("0.80")
        txn_id = db.insert_returning_id(
            "INSERT INTO wallet_transactions"
            "  (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)"
            " VALUES (%s, %s, 'debit', %s, 49.20, %s, %s) RETURNING id",
            (wallet2["id"], tid, charge, f"Call {call_id}", f"call:{call_id}"),
        )
        db.execute(
            "UPDATE calls SET billing_finalized=TRUE, charge=%s, wallet_transaction_id=%s"
            " WHERE id=%s",
            (charge, txn_id, call_id),
        )
        db.execute("DELETE FROM wallets WHERE tenant_id=%s", (tid,))
        r = refund_call_charge(call_id, reason_code="test", source="test")
        assert r["refunded"] is False
        assert r["reason"] == "wallet_not_found"
        row = db.query_one("SELECT refund_status FROM calls WHERE id=%s", (call_id,))
        assert row["refund_status"] == "none"

    def test_refund_without_original_debit_row_rejected(self, app, db_conn):
        """billing_finalized=TRUE but debit ledger row deleted → no_debit_row."""
        from app.db import db
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        # Delete the debit ledger row (simulates anomaly B)
        db.execute(
            "DELETE FROM wallet_transactions WHERE tenant_id=%s AND ref_id=%s AND type='debit'",
            (tid, f"call:{call_id}"),
        )
        r = refund_call_charge(call_id, reason_code="test", source="test")
        assert r["refunded"] is False
        assert r["reason"] == "no_debit_row"


# ── Phase 13: metric operation tests ─────────────────────────────────────────

class TestMetricIncrements:
    """Verify Prometheus counters increment correctly through real billing paths."""

    def _get_counter(self, metric, labels=None):
        """Read current counter value."""
        try:
            if labels:
                return metric.labels(**labels)._value.get()
            return metric._value.get()
        except Exception:
            return 0.0

    def test_new_billing_increments_finalized_counter(self, app, db_conn):
        from app.db import db
        from app.monitoring import CALL_BILLING_FINALIZED, WALLET_CALL_DEBIT
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="completed")
        before_finalized = self._get_counter(CALL_BILLING_FINALIZED)
        before_debit = self._get_counter(WALLET_CALL_DEBIT)
        finalize_call_billing(call_id)
        assert self._get_counter(CALL_BILLING_FINALIZED) == before_finalized + 1
        assert self._get_counter(WALLET_CALL_DEBIT) == before_debit + 1

    def test_duplicate_finalization_increments_already_finalized(self, app, db_conn):
        from app.db import db
        from app.monitoring import CALL_BILLING_ALREADY_FINALIZED, WALLET_CALL_DEBIT
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="completed")
        finalize_call_billing(call_id)
        before_already = self._get_counter(CALL_BILLING_ALREADY_FINALIZED)
        before_debit = self._get_counter(WALLET_CALL_DEBIT)
        finalize_call_billing(call_id)  # second call → already_finalized
        assert self._get_counter(CALL_BILLING_ALREADY_FINALIZED) == before_already + 1
        assert self._get_counter(WALLET_CALL_DEBIT) == before_debit  # no extra debit

    def test_insufficient_balance_increments_failed_counter(self, app, db_conn):
        from app.db import db
        from app.monitoring import CALL_BILLING_FAILED, WALLET_INSUFFICIENT
        tid = _tenant(db, balance="0.01", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="completed")
        before_failed = self._get_counter(CALL_BILLING_FAILED, {"error_code": "insufficient_balance"})
        before_insuf = self._get_counter(WALLET_INSUFFICIENT)
        finalize_call_billing(call_id)
        assert self._get_counter(CALL_BILLING_FAILED, {"error_code": "insufficient_balance"}) == before_failed + 1
        assert self._get_counter(WALLET_INSUFFICIENT) == before_insuf + 1

    def test_refund_increments_refund_counter(self, app, db_conn):
        from app.db import db
        from app.monitoring import WALLET_CALL_REFUND
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        before_refund = self._get_counter(WALLET_CALL_REFUND)
        refund_call_charge(call_id, reason_code="test", source="test")
        assert self._get_counter(WALLET_CALL_REFUND) == before_refund + 1

    def test_duplicate_refund_does_not_double_increment_refund_counter(self, app, db_conn):
        from app.db import db
        from app.monitoring import WALLET_CALL_REFUND
        tid = _tenant(db, balance="50.00", rate_per_min="0.80", pulse=60)
        call_id = _call(db, tid, talk_seconds=60, status="canceled")
        finalize_call_billing(call_id)
        refund_call_charge(call_id, reason_code="test", source="test")
        before_refund = self._get_counter(WALLET_CALL_REFUND)
        refund_call_charge(call_id, reason_code="test", source="test")  # duplicate
        assert self._get_counter(WALLET_CALL_REFUND) == before_refund  # not incremented
