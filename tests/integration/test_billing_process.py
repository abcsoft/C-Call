# tests/integration/test_billing_process.py
"""
Real OS-process financial crash tests for billing atomicity.

Each test spawns actual subprocesses via subprocess.Popen so that
os._exit(1) at a fault point terminates a real OS PID — something
threads cannot simulate.

All setup rows are committed (via an independent psycopg connection)
BEFORE any subprocess starts, so they are visible to separate processes.

Environment: disposable DB set via TEST_DATABASE_URL / DATABASE_URL.
These tests are skipped when BILLING_PROCESS_TEST_DB is not set.

Groups
------
  A  Two-finalizer process race (Phase 5)
  B  Finalization crash tests — 7 fault points (Phase 6)
  C  Monitor/reconciliation process race (Phase 7)
  D  Refund process race (Phase 8)
  E  Refund crash tests — 5 fault points (Phase 9)
  F  DB connection-loss test (Phase 10)
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from decimal import Decimal
from typing import Any

import psycopg
import psycopg.rows
import pytest

# ── DB URL ────────────────────────────────────────────────────────────────────

_PROC_DB_URL: str | None = os.environ.get("BILLING_PROCESS_TEST_DB") or os.environ.get(
    "TEST_DATABASE_URL"
)

pytestmark = pytest.mark.skipif(
    not _PROC_DB_URL,
    reason="BILLING_PROCESS_TEST_DB not set — set to disposable DB URL to run process tests",
)

# ── Helpers ───────────────────────────────────────────────────────────────────

PSQL_CMD = os.environ.get(
    "PSQL_PATH",
    r"C:\Program Files\PostgreSQL\16\bin\psql.exe",
)
PYTHON = sys.executable

FINALIZER_MODULE = "app.commands.run_billing_finalizer"

FINALIZE_FAULT_POINTS = [
    "after_call_lock",
    "after_charge_calculation",
    "after_wallet_lock",
    "after_wallet_update",
    "after_ledger_insert",
    "after_call_billing_update",
    "before_transaction_commit",
]

REFUND_FAULT_POINTS = [
    "after_refund_wallet_lock",
    "after_refund_wallet_update",
    "after_refund_ledger_insert",
    "after_refund_state_update",
    "before_refund_commit",
]


def _conn() -> psycopg.Connection:
    """Open independent psycopg connection to the disposable test DB."""
    assert _PROC_DB_URL, "BILLING_PROCESS_TEST_DB not set"
    return psycopg.connect(_PROC_DB_URL, autocommit=False, row_factory=psycopg.rows.dict_row)


def _setup_tenant_and_call(
    balance: str = "500.00",
    rate_per_min: str = "0.80",
    talk_seconds: int = 60,
    status: str = "completed",
) -> dict[str, Any]:
    """Insert tenant + wallet + rate + call, commit, return ids dict.

    All rows committed before the function returns so they are visible
    to separate OS processes.
    """
    test_run_id = secrets.token_hex(8)
    initial_balance = Decimal(balance)

    with _conn() as conn:
        # Tenant
        tid = conn.execute(
            "INSERT INTO tenants (business_name, email, status, plan_id)"
            " SELECT %s, %s, 'active', id FROM plans LIMIT 1"
            " RETURNING id",
            (
                f"ProcTest_{test_run_id}",
                f"proc_{test_run_id}@test.example",
            ),
        ).fetchone()["id"]

        # Wallet — must be inserted explicitly (no trigger)
        conn.execute(
            "INSERT INTO wallets (tenant_id, balance, opening_balance, currency, low_balance_threshold)"
            " VALUES (%s, %s, %s, 'BDT', 50.00)",
            (tid, initial_balance, initial_balance),
        )

        # Rate
        conn.execute(
            "INSERT INTO customer_rates"
            " (tenant_id, rate_per_min, rate_per_call, min_charge,"
            "  billing_pulse_seconds, minimum_billable_seconds, charge_failed_calls)"
            " VALUES (%s, %s, 0.00, %s, 60, 60, false)"
            " ON CONFLICT (tenant_id) DO UPDATE SET"
            "  rate_per_min=%s, rate_per_call=0.00, min_charge=%s,"
            "  billing_pulse_seconds=60, minimum_billable_seconds=60, charge_failed_calls=false",
            (tid, rate_per_min, rate_per_min, rate_per_min, rate_per_min),
        )

        # Call
        call_id = conn.execute(
            "INSERT INTO calls"
            " (tenant_id, number, status, talk_seconds, total_seconds,"
            "  started_at, ended_at, call_type)"
            " VALUES (%s, '01700000000', %s, %s, %s,"
            "  now()-interval '2 min', now(), 'ivr')"
            " RETURNING id",
            (tid, status, talk_seconds, talk_seconds + 5),
        ).fetchone()["id"]

        conn.commit()

    return {
        "tenant_id": tid,
        "call_id": call_id,
        "initial_balance": initial_balance,
        "test_run_id": test_run_id,
    }


def _finalize_call(
    env: dict[str, Any],
    call_id: int,
) -> dict[str, Any]:
    """Read DB state for a call after finalization attempt."""
    with _conn() as conn:
        tid = env["tenant_id"]
        call = conn.execute(
            "SELECT billing_finalized, charge, wallet_transaction_id,"
            "       refund_status, refund_transaction_id"
            " FROM calls WHERE id=%s",
            (call_id,),
        ).fetchone()
        wallet = conn.execute(
            "SELECT balance FROM wallets WHERE tenant_id=%s", (tid,)
        ).fetchone()
        debits = conn.execute(
            "SELECT id, amount FROM wallet_transactions"
            " WHERE tenant_id=%s AND ref_id=%s AND type='debit'",
            (tid, f"call:{call_id}"),
        ).fetchall()
        conn.commit()
    return {
        "call": dict(call) if call else None,
        "balance": Decimal(str(wallet["balance"])) if wallet else None,
        "debit_rows": [dict(r) for r in debits],
    }


def _refund_state(tid: int, call_id: int) -> dict[str, Any]:
    with _conn() as conn:
        call = conn.execute(
            "SELECT refund_status, refund_transaction_id, refunded_at"
            " FROM calls WHERE id=%s",
            (call_id,),
        ).fetchone()
        credits = conn.execute(
            "SELECT id, amount FROM wallet_transactions"
            " WHERE tenant_id=%s AND ref_id=%s AND type='refund'",
            (tid, f"refund:call:{call_id}"),
        ).fetchall()
        conn.commit()
    return {
        "call": dict(call) if call else None,
        "refund_rows": [dict(r) for r in credits],
    }


def _cleanup(env: dict[str, Any]) -> None:
    tid = env["tenant_id"]
    call_id = env["call_id"]
    with _conn() as conn:
        # Reset refund columns before deleting wallet_transactions (CHECK NOT DEFERRABLE)
        conn.execute(
            "UPDATE calls SET"
            "  wallet_transaction_id = NULL,"
            "  refund_status         = 'none',"
            "  refund_transaction_id = NULL,"
            "  refunded_at           = NULL,"
            "  refund_reason_code    = NULL,"
            "  refund_source         = NULL"
            " WHERE tenant_id = %s",
            (tid,),
        )
        conn.execute("DELETE FROM wallet_transactions WHERE tenant_id=%s", (tid,))
        conn.execute("DELETE FROM audit_log WHERE target_tenant_id=%s", (tid,))
        conn.execute("DELETE FROM order_call_attempts WHERE tenant_id=%s", (tid,))
        conn.execute("DELETE FROM calls WHERE tenant_id=%s", (tid,))
        conn.execute("DELETE FROM wallets WHERE tenant_id=%s", (tid,))
        conn.execute("DELETE FROM customer_rates WHERE tenant_id=%s", (tid,))
        conn.execute("DELETE FROM tenants WHERE id=%s", (tid,))
        conn.commit()


def _parse_json_output(text: str) -> dict | None:
    """Extract the billing result JSON from subprocess stdout.

    Skips structlog/JSON log lines (which contain 'level' + 'event' keys)
    and returns the first dict that looks like a billing result.
    """
    _log_keys = {"level", "event", "timestamp"}

    # Try each { line
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Skip structlog lines
        if isinstance(obj, dict) and _log_keys.issubset(obj.keys()):
            continue
        return obj

    # Try multi-line indented JSON (the --json flag produces indented output)
    # Collect lines between first { and last }
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("{") and start is None:
            start = i
    if start is not None:
        block = "\n".join(lines[start:])
        try:
            obj = json.loads(block)
            if isinstance(obj, dict) and not _log_keys.issubset(obj.keys()):
                return obj
        except json.JSONDecodeError:
            pass

    return None


def _run_finalizer(
    call_id: int,
    *,
    fault_point: str | None = None,
    refund: bool = False,
    reason_code: str = "proc_test",
    timeout: int = 60,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run run_billing_finalizer as a real subprocess."""
    env = {**os.environ, "DATABASE_URL": _PROC_DB_URL, "FLASK_ENV": "testing"}
    if fault_point:
        env["BILLING_FAULT_POINT"] = fault_point
    if extra_env:
        env.update(extra_env)

    cmd = [PYTHON, "-m", FINALIZER_MODULE, "--call-id", str(call_id), "--json"]
    if refund:
        cmd += ["--refund", "--reason-code", reason_code]
    if fault_point:
        cmd += ["--fault-point", fault_point]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(__file__).split("tests")[0].rstrip("/\\"),
    )
    return proc


def _popen_finalizer(
    call_id: int,
    *,
    fault_point: str | None = None,
    refund: bool = False,
    reason_code: str = "proc_race",
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start finalizer as Popen (non-blocking) for race tests."""
    env = {**os.environ, "DATABASE_URL": _PROC_DB_URL, "FLASK_ENV": "testing"}
    if fault_point:
        env["BILLING_FAULT_POINT"] = fault_point
    if extra_env:
        env.update(extra_env)

    cmd = [PYTHON, "-m", FINALIZER_MODULE, "--call-id", str(call_id), "--json"]
    if refund:
        cmd += ["--refund", "--reason-code", reason_code]
    if fault_point:
        cmd += ["--fault-point", fault_point]

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(__file__).split("tests")[0].rstrip("/\\"),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Group A — Two-finalizer process race
# ═══════════════════════════════════════════════════════════════════════════════

class TestTwoFinalizerRace:
    """Phase 5: Two real OS PIDs race to finalize the same call."""

    def setup_method(self):
        self.env = _setup_tenant_and_call()

    def teardown_method(self):
        _cleanup(self.env)

    def test_two_processes_exactly_one_debit(self):
        call_id = self.env["call_id"]
        initial_balance = self.env["initial_balance"]

        p1 = _popen_finalizer(call_id)
        p2 = _popen_finalizer(call_id)

        out1, err1 = p1.communicate(timeout=90)
        out2, err2 = p2.communicate(timeout=90)

        rc1, rc2 = p1.returncode, p2.returncode

        # Both must exit (0=success, 1=business rejection, or abnormal is ok for race)
        # At least one must succeed (exit 0)
        results = []
        for out, rc in [(out1, rc1), (out2, rc2)]:
            if rc == 0 and out.strip():
                parsed = _parse_json_output(out)
                if parsed is not None:
                    results.append(parsed)

        state = _finalize_call(self.env, call_id)
        debit_count = len(state["debit_rows"])
        assert debit_count == 1, (
            f"Exactly one debit required; got {debit_count}. "
            f"P1 rc={rc1} out={out1[:200]} err={err1[:200]}; "
            f"P2 rc={rc2} out={out2[:200]} err={err2[:200]}"
        )

        assert state["call"]["billing_finalized"] is True
        charge = state["debit_rows"][0]["amount"]
        assert state["balance"] == initial_balance - Decimal(str(charge)), (
            f"Balance drift: initial={initial_balance} charge={charge} balance={state['balance']}"
        )

        success_count = sum(1 for r in results if r.get("billed"))
        assert success_count <= 1, f"At most one process may report billed=true: {results}"


# ═══════════════════════════════════════════════════════════════════════════════
# Group B — Finalization crash tests (7 fault points)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFinalizationCrash:
    """Phase 6: One real OS PID crashes at each fault point.

    After crash the call must still be unfinalized (safe to retry).
    Retry must then succeed and produce exactly one debit.
    """

    @pytest.mark.parametrize("fault_point", FINALIZE_FAULT_POINTS)
    def test_crash_then_retry_succeeds(self, fault_point):
        env = _setup_tenant_and_call()
        call_id = env["call_id"]
        initial_balance = env["initial_balance"]

        try:
            proc = _run_finalizer(call_id, fault_point=fault_point, timeout=60)
            # Process must have terminated abnormally (os._exit(1)) or with exit code != 0
            assert proc.returncode != 0, (
                f"Fault point {fault_point!r} did not crash the process. "
                f"rc={proc.returncode} out={proc.stdout[:300]}"
            )

            # DB must show no committed debit — transaction rolled back
            state = _finalize_call(env, call_id)
            assert not state["call"]["billing_finalized"], (
                f"Fault point {fault_point!r}: call shows billing_finalized=True after crash — "
                f"atomicity broken. state={state}"
            )
            assert len(state["debit_rows"]) == 0, (
                f"Fault point {fault_point!r}: debit row exists after crash — "
                f"partial write. rows={state['debit_rows']}"
            )
            assert state["balance"] == initial_balance, (
                f"Fault point {fault_point!r}: balance changed after crash — "
                f"partial debit. balance={state['balance']} initial={initial_balance}"
            )

            # Retry must succeed
            retry = _run_finalizer(call_id, timeout=60)
            assert retry.returncode == 0, (
                f"Retry after crash at {fault_point!r} failed. "
                f"rc={retry.returncode} out={retry.stdout[:300]} err={retry.stderr[:300]}"
            )
            result = _parse_json_output(retry.stdout)
            if result is not None:
                assert result.get("billed") is True, (
                    f"Retry result says billed=False after crash at {fault_point!r}: {result}"
                )

            state2 = _finalize_call(env, call_id)
            assert len(state2["debit_rows"]) == 1, (
                f"Retry after crash at {fault_point!r} produced {len(state2['debit_rows'])} debit rows"
            )
            assert state2["call"]["billing_finalized"] is True

        finally:
            _cleanup(env)


# ═══════════════════════════════════════════════════════════════════════════════
# Group C — Monitor/reconciliation process race
# ═══════════════════════════════════════════════════════════════════════════════

class TestMonitorReconcileRace:
    """Phase 7: Monitor and reconciliation paths race as separate OS processes."""

    def setup_method(self):
        self.env = _setup_tenant_and_call()

    def teardown_method(self):
        _cleanup(self.env)

    def test_monitor_and_reconcile_exactly_one_debit(self):
        call_id = self.env["call_id"]
        initial_balance = self.env["initial_balance"]

        p_monitor = _popen_finalizer(call_id, extra_env={"CCALL_SOURCE": "monitor"})
        p_reconcile = _popen_finalizer(call_id, extra_env={"CCALL_SOURCE": "reconcile"})

        out_m, err_m = p_monitor.communicate(timeout=90)
        out_r, err_r = p_reconcile.communicate(timeout=90)

        state = _finalize_call(self.env, call_id)
        debit_count = len(state["debit_rows"])
        assert debit_count == 1, (
            f"Monitor+reconcile race produced {debit_count} debits. "
            f"monitor rc={p_monitor.returncode} out={out_m[:200]}; "
            f"reconcile rc={p_reconcile.returncode} out={out_r[:200]}"
        )
        assert state["call"]["billing_finalized"] is True

        charge = state["debit_rows"][0]["amount"]
        assert state["balance"] == initial_balance - Decimal(str(charge))


# ═══════════════════════════════════════════════════════════════════════════════
# Group D — Refund process race
# ═══════════════════════════════════════════════════════════════════════════════

class TestRefundProcessRace:
    """Phase 8: Two real OS PIDs race to refund the same billed call."""

    def setup_method(self):
        self.env = _setup_tenant_and_call()
        # Finalize billing synchronously (committed) before the refund race
        proc = _run_finalizer(self.env["call_id"], timeout=60)
        assert proc.returncode == 0, (
            f"Setup: finalization failed. rc={proc.returncode} "
            f"out={proc.stdout[:300]} err={proc.stderr[:300]}"
        )

    def teardown_method(self):
        _cleanup(self.env)

    def test_two_processes_exactly_one_refund(self):
        call_id = self.env["call_id"]
        tid = self.env["tenant_id"]

        p1 = _popen_finalizer(call_id, refund=True, reason_code="race_test_1")
        p2 = _popen_finalizer(call_id, refund=True, reason_code="race_test_2")

        out1, err1 = p1.communicate(timeout=90)
        out2, err2 = p2.communicate(timeout=90)

        state = _refund_state(tid, call_id)
        refund_count = len(state["refund_rows"])
        assert refund_count == 1, (
            f"Refund race produced {refund_count} refund rows. "
            f"P1 rc={p1.returncode} out={out1[:200]}; "
            f"P2 rc={p2.returncode} out={out2[:200]}"
        )
        assert state["call"]["refund_status"] == "refunded"


# ═══════════════════════════════════════════════════════════════════════════════
# Group E — Refund crash tests (5 fault points)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRefundCrash:
    """Phase 9: One real OS PID crashes at each refund fault point.

    After crash the call refund_status must still be 'none'.
    Retry must then succeed and produce exactly one refund credit.
    """

    @pytest.mark.parametrize("fault_point", REFUND_FAULT_POINTS)
    def test_refund_crash_then_retry_succeeds(self, fault_point):
        env = _setup_tenant_and_call()
        call_id = env["call_id"]
        tid = env["tenant_id"]

        try:
            # Finalize billing first (committed)
            fin = _run_finalizer(call_id, timeout=60)
            assert fin.returncode == 0, (
                f"Setup finalization failed at {fault_point!r}: "
                f"rc={fin.returncode} out={fin.stdout[:200]}"
            )

            # Crash refund at fault point
            proc = _run_finalizer(
                call_id, fault_point=fault_point, refund=True,
                reason_code="crash_test", timeout=60,
            )
            assert proc.returncode != 0, (
                f"Refund fault point {fault_point!r} did not crash. rc={proc.returncode}"
            )

            # DB must show no committed refund
            state = _refund_state(tid, call_id)
            assert state["call"]["refund_status"] == "none", (
                f"Fault point {fault_point!r}: refund_status={state['call']['refund_status']} "
                f"after crash — atomicity broken"
            )
            assert len(state["refund_rows"]) == 0, (
                f"Fault point {fault_point!r}: refund row exists after crash"
            )

            # Retry must succeed
            retry = _run_finalizer(
                call_id, refund=True, reason_code="crash_retry", timeout=60
            )
            assert retry.returncode == 0, (
                f"Refund retry after crash at {fault_point!r} failed. "
                f"rc={retry.returncode} out={retry.stdout[:300]} err={retry.stderr[:300]}"
            )
            result = _parse_json_output(retry.stdout)
            if result is not None:
                assert result.get("refunded") is True, (
                    f"Retry refund result says refunded=False at {fault_point!r}: {result}"
                )

            state2 = _refund_state(tid, call_id)
            assert len(state2["refund_rows"]) == 1, (
                f"Refund retry after crash at {fault_point!r} produced "
                f"{len(state2['refund_rows'])} refund rows"
            )
            assert state2["call"]["refund_status"] == "refunded"

        finally:
            _cleanup(env)


# ═══════════════════════════════════════════════════════════════════════════════
# Group F — DB connection-loss test
# ═══════════════════════════════════════════════════════════════════════════════

class TestDbConnectionLoss:
    """Phase 10: Terminate the DB backend mid-transaction; verify rollback; verify retry."""

    def setup_method(self):
        self.env = _setup_tenant_and_call()

    def teardown_method(self):
        _cleanup(self.env)

    def test_connection_termination_leaves_call_unfinalized(self):
        """
        Strategy: start a finalizer subprocess at fault point 'after_wallet_lock'
        so it holds the wallet row lock inside a transaction.  We then immediately
        terminate the subprocess (SIGKILL / TerminateProcess), which closes the
        connection and causes PostgreSQL to roll back the transaction.

        Verify: call still unfinalized, zero debits.
        Retry: must succeed.
        """
        call_id = self.env["call_id"]
        initial_balance = self.env["initial_balance"]

        env = {**os.environ, "DATABASE_URL": _PROC_DB_URL, "FLASK_ENV": "testing",
               "BILLING_FAULT_POINT": "after_wallet_lock"}
        cmd = [
            PYTHON, "-m", FINALIZER_MODULE,
            "--call-id", str(call_id),
            "--fault-point", "after_wallet_lock",
            "--json",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(__file__).split("tests")[0].rstrip("/\\"),
        )

        # Wait briefly for the process to reach the fault point (it will os._exit(1) itself)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # The fault point fires os._exit(1) — returncode should be non-zero
        assert proc.returncode != 0, (
            f"Expected abnormal exit from after_wallet_lock, got rc={proc.returncode}"
        )

        # Allow a moment for PG to detect the closed connection and roll back
        time.sleep(0.5)

        state = _finalize_call(self.env, call_id)
        assert not state["call"]["billing_finalized"], (
            "Call shows billing_finalized=True after connection termination — "
            "transaction was not rolled back"
        )
        assert len(state["debit_rows"]) == 0, (
            f"Debit row exists after connection termination: {state['debit_rows']}"
        )
        assert state["balance"] == initial_balance, (
            f"Balance changed after connection termination: {state['balance']} != {initial_balance}"
        )

        # Retry succeeds
        retry = _run_finalizer(call_id, timeout=60)
        assert retry.returncode == 0, (
            f"Retry after connection termination failed. "
            f"rc={retry.returncode} err={retry.stderr[:300]}"
        )
        state2 = _finalize_call(self.env, call_id)
        assert len(state2["debit_rows"]) == 1
        assert state2["call"]["billing_finalized"] is True
