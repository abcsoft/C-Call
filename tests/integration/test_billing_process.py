# tests/integration/test_billing_process.py
"""
Real OS-process financial crash tests for billing atomicity.

Each test spawns actual subprocesses via subprocess.Popen so that
os._exit(1) at a fault point terminates a real OS PID — something
threads cannot simulate.

All setup rows are committed (via an independent psycopg connection)
BEFORE any subprocess starts, so they are visible to separate processes.

Environment: disposable DB set via BILLING_PROCESS_TEST_DB.
Tests are skipped when BILLING_PROCESS_TEST_DB is not set.

Groups
------
  A  Two-finalizer process race (two real OS PIDs)
  B  Finalization crash tests — 7 fault points
  C  Monitor/reconciliation process race (--source monitor / --source reconcile_apply)
  D  Refund process race (two real OS PIDs)
  E  Refund crash tests — 5 fault points
  F  Real pg_terminate_backend connection-loss test
  G  Production fault-point rejection (FLASK_ENV=production)
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from typing import Any

import psycopg
import psycopg.rows
import pytest

# ── DB URL ────────────────────────────────────────────────────────────────────

_PROC_DB_URL: str | None = os.environ.get("BILLING_PROCESS_TEST_DB")

pytestmark = pytest.mark.skipif(
    not _PROC_DB_URL,
    reason="BILLING_PROCESS_TEST_DB not set — set to disposable DB URL to run process tests",
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

# ── Helpers ───────────────────────────────────────────────────────────────────

def _conn() -> psycopg.Connection:
    assert _PROC_DB_URL, "BILLING_PROCESS_TEST_DB not set"
    return psycopg.connect(_PROC_DB_URL, autocommit=False, row_factory=psycopg.rows.dict_row)


def _setup_tenant_and_call(
    balance: str = "500.00",
    rate_per_min: str = "0.80",
    talk_seconds: int = 60,
    status: str = "completed",
) -> dict[str, Any]:
    """Insert tenant + wallet + rate + call, commit, return ids.

    Uses an independent psycopg connection and commits before returning
    so all rows are visible to any OS process that opens a new connection.
    """
    test_run_id = secrets.token_hex(8)
    initial_balance = Decimal(balance)

    with _conn() as conn:
        tid = conn.execute(
            "INSERT INTO tenants (business_name, email, status, plan_id)"
            " SELECT %s, %s, 'active', id FROM plans LIMIT 1"
            " RETURNING id",
            (
                f"ProcTest_{test_run_id}",
                f"proc_{test_run_id}@test.example",
            ),
        ).fetchone()["id"]

        conn.execute(
            "INSERT INTO wallets (tenant_id, balance, opening_balance, currency, low_balance_threshold)"
            " VALUES (%s, %s, %s, 'BDT', 50.00)",
            (tid, initial_balance, initial_balance),
        )

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

        call_id = conn.execute(
            "INSERT INTO calls"
            " (tenant_id, number, status, talk_seconds, total_seconds,"
            "  started_at, ended_at, call_type, rate_snapshot)"
            " VALUES (%s, '01700000000', %s, %s, %s,"
            "  now()-interval '2 min', now(), 'ivr', %s)"
            " RETURNING id",
            (
                tid,
                status,
                talk_seconds,
                talk_seconds + 5,
                json.dumps({
                    "policy_version": 1,
                    "tenant_id": tid,
                    "rate_per_min": str(rate_per_min),
                    "rate_per_call": "0.00",
                    "min_charge": str(rate_per_min),
                    "billing_pulse_seconds": 60,
                    "minimum_billable_seconds": 60,
                    "charge_failed_calls": False,
                    "currency": "BDT",
                    "rate_per_min_source": "tenant_override",
                    "billing_pulse_seconds_source": "tenant_override",
                    "minimum_billable_seconds_source": "tenant_override",
                    "charge_failed_calls_source": "tenant_override",
                }),
            ),
        ).fetchone()["id"]

        conn.commit()

    return {
        "tenant_id": tid,
        "call_id": call_id,
        "initial_balance": initial_balance,
        "test_run_id": test_run_id,
    }


def _db_state(env: dict[str, Any], call_id: int) -> dict[str, Any]:
    """Read wallet + call + debit rows from a fresh connection."""
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
    with _conn() as conn:
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
    """Extract billing result JSON from subprocess stdout; skip structlog lines."""
    _log_keys = {"level", "event", "timestamp"}

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and _log_keys.issubset(obj.keys()):
            continue  # structlog line
        return obj

    # Try multi-line indented block (--json produces indented output)
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


def _make_env(flask_env: str = "testing", fault_point: str | None = None,
              extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ, "DATABASE_URL": _PROC_DB_URL, "FLASK_ENV": flask_env}
    if fault_point:
        env["BILLING_FAULT_POINT"] = fault_point
    if extra:
        env.update(extra)
    return env


def _cwd() -> str:
    # Project root: tests/integration/file.py → ../../ = project root
    return str(pathlib.Path(__file__).parent.parent.parent)


def _run_finalizer(
    call_id: int,
    *,
    fault_point: str | None = None,
    source: str = "cli",
    refund: bool = False,
    reason_code: str = "proc_test",
    flask_env: str = "testing",
    timeout: int = 60,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run run_billing_finalizer synchronously; return completed process."""
    env = _make_env(flask_env=flask_env, fault_point=fault_point, extra=extra_env)
    cmd = [
        PYTHON, "-m", FINALIZER_MODULE,
        "--call-id", str(call_id),
        "--source", source,
        "--json",
    ]
    if refund:
        cmd += ["--refund", "--reason-code", reason_code]
    if fault_point:
        cmd += ["--fault-point", fault_point]

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=_cwd(),
    )


def _popen_finalizer(
    call_id: int,
    *,
    fault_point: str | None = None,
    source: str = "cli",
    refund: bool = False,
    reason_code: str = "proc_race",
    flask_env: str = "testing",
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start finalizer as non-blocking Popen for race tests."""
    env = _make_env(flask_env=flask_env, fault_point=fault_point, extra=extra_env)
    cmd = [
        PYTHON, "-m", FINALIZER_MODULE,
        "--call-id", str(call_id),
        "--source", source,
        "--json",
    ]
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
        cwd=_cwd(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Group A — Two-finalizer process race
# ═══════════════════════════════════════════════════════════════════════════════

class TestTwoFinalizerRace:
    """Two real OS PIDs race to finalize the same call."""

    def setup_method(self):
        self.env = _setup_tenant_and_call()

    def teardown_method(self):
        _cleanup(self.env)

    def test_two_processes_exactly_one_debit(self):
        call_id = self.env["call_id"]
        initial_balance = self.env["initial_balance"]

        p1 = _popen_finalizer(call_id, source="finalizer_1")
        p2 = _popen_finalizer(call_id, source="finalizer_2")

        out1, err1 = p1.communicate(timeout=90)
        out2, err2 = p2.communicate(timeout=90)

        rc1, rc2 = p1.returncode, p2.returncode

        results = []
        for out, rc in [(out1, rc1), (out2, rc2)]:
            parsed = _parse_json_output(out)
            if parsed is not None:
                results.append(parsed)

        state = _db_state(self.env, call_id)
        debit_count = len(state["debit_rows"])
        assert debit_count == 1, (
            f"Exactly one debit required; got {debit_count}. "
            f"P1(pid={p1.pid}) rc={rc1} out={out1[:200]} err={err1[:200]}; "
            f"P2(pid={p2.pid}) rc={rc2} out={out2[:200]} err={err2[:200]}"
        )
        assert state["call"]["billing_finalized"] is True
        charge = state["debit_rows"][0]["amount"]
        assert state["balance"] == initial_balance - Decimal(str(charge))
        billed = [r for r in results if r.get("billed")]
        assert len(billed) <= 1, f"At most one billed=True: {results}"


# ═══════════════════════════════════════════════════════════════════════════════
# Group B — Finalization crash tests (7 fault points)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFinalizationCrash:
    """One OS PID crashes at each fault point; call stays unfinalized; retry succeeds."""

    @pytest.mark.parametrize("fault_point", FINALIZE_FAULT_POINTS)
    def test_crash_then_retry_succeeds(self, fault_point):
        env = _setup_tenant_and_call()
        call_id = env["call_id"]
        initial_balance = env["initial_balance"]
        try:
            proc = _run_finalizer(call_id, fault_point=fault_point,
                                  source="crash_test", timeout=60)
            assert proc.returncode != 0, (
                f"Fault point {fault_point!r} did not crash. rc={proc.returncode}"
            )

            state = _db_state(env, call_id)
            assert not state["call"]["billing_finalized"], (
                f"{fault_point!r}: billing_finalized=True after crash — atomicity broken"
            )
            assert len(state["debit_rows"]) == 0, (
                f"{fault_point!r}: debit row exists after crash"
            )
            assert state["balance"] == initial_balance, (
                f"{fault_point!r}: balance changed after crash"
            )

            retry = _run_finalizer(call_id, source="crash_retry", timeout=60)
            assert retry.returncode == 0, (
                f"Retry after {fault_point!r} failed. rc={retry.returncode} "
                f"err={retry.stderr[:300]}"
            )
            result = _parse_json_output(retry.stdout)
            if result is not None:
                assert result.get("billed") is True, (
                    f"Retry billed=False after {fault_point!r}: {result}"
                )

            state2 = _db_state(env, call_id)
            assert len(state2["debit_rows"]) == 1, (
                f"Retry after {fault_point!r} produced {len(state2['debit_rows'])} debits"
            )
            assert state2["call"]["billing_finalized"] is True
        finally:
            _cleanup(env)


# ═══════════════════════════════════════════════════════════════════════════════
# Group C — Monitor/reconciliation process race
# ═══════════════════════════════════════════════════════════════════════════════

class TestMonitorReconcileRace:
    """Monitor (--source monitor) and reconciliation (--source reconcile_apply) race."""

    def setup_method(self):
        self.env = _setup_tenant_and_call()

    def teardown_method(self):
        _cleanup(self.env)

    def test_monitor_and_reconcile_exactly_one_debit(self):
        call_id = self.env["call_id"]
        initial_balance = self.env["initial_balance"]

        # Use explicit --source CLI argument (not env var)
        p_monitor = _popen_finalizer(call_id, source="monitor")
        p_reconcile = _popen_finalizer(call_id, source="reconcile_apply")

        out_m, err_m = p_monitor.communicate(timeout=90)
        out_r, err_r = p_reconcile.communicate(timeout=90)

        state = _db_state(self.env, call_id)
        debit_count = len(state["debit_rows"])
        assert debit_count == 1, (
            f"Monitor+reconcile race produced {debit_count} debits. "
            f"monitor(pid={p_monitor.pid}) rc={p_monitor.returncode} out={out_m[:200]}; "
            f"reconcile(pid={p_reconcile.pid}) rc={p_reconcile.returncode} out={out_r[:200]}"
        )
        assert state["call"]["billing_finalized"] is True
        charge = state["debit_rows"][0]["amount"]
        assert state["balance"] == initial_balance - Decimal(str(charge))

        # Verify that --source reached the finalizer via log output
        # (structlog emits "source=monitor" or "source=reconcile_apply" in the success log)
        combined_logs = out_m + err_m + out_r + err_r
        assert "monitor" in combined_logs or "reconcile_apply" in combined_logs, (
            "Expected 'monitor' or 'reconcile_apply' in subprocess output — "
            "--source argument may not have been passed"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Group D — Refund process race
# ═══════════════════════════════════════════════════════════════════════════════

class TestRefundProcessRace:
    """Two OS PIDs race to refund the same billed call."""

    def setup_method(self):
        self.env = _setup_tenant_and_call()
        proc = _run_finalizer(self.env["call_id"], source="setup", timeout=60)
        assert proc.returncode == 0, (
            f"Setup finalization failed. rc={proc.returncode} err={proc.stderr[:300]}"
        )

    def teardown_method(self):
        _cleanup(self.env)

    def test_two_processes_exactly_one_refund(self):
        call_id = self.env["call_id"]
        tid = self.env["tenant_id"]

        p1 = _popen_finalizer(call_id, refund=True,
                               reason_code="race_test", source="refund_proc_1")
        p2 = _popen_finalizer(call_id, refund=True,
                               reason_code="race_test", source="refund_proc_2")

        out1, err1 = p1.communicate(timeout=90)
        out2, err2 = p2.communicate(timeout=90)

        state = _refund_state(tid, call_id)
        refund_count = len(state["refund_rows"])
        assert refund_count == 1, (
            f"Refund race produced {refund_count} rows. "
            f"P1(pid={p1.pid}) rc={p1.returncode}; "
            f"P2(pid={p2.pid}) rc={p2.returncode}"
        )
        assert state["call"]["refund_status"] == "refunded"


# ═══════════════════════════════════════════════════════════════════════════════
# Group E — Refund crash tests (5 fault points)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRefundCrash:
    """One OS PID crashes at each refund fault point; no refund committed; retry succeeds."""

    @pytest.mark.parametrize("fault_point", REFUND_FAULT_POINTS)
    def test_refund_crash_then_retry_succeeds(self, fault_point):
        env = _setup_tenant_and_call()
        call_id = env["call_id"]
        tid = env["tenant_id"]
        try:
            fin = _run_finalizer(call_id, source="setup", timeout=60)
            assert fin.returncode == 0, (
                f"Setup finalization failed for {fault_point!r}: err={fin.stderr[:200]}"
            )

            proc = _run_finalizer(
                call_id, fault_point=fault_point, refund=True,
                reason_code="crash_test", source="crash_refund", timeout=60,
            )
            assert proc.returncode != 0, (
                f"Refund fault {fault_point!r} did not crash. rc={proc.returncode}"
            )

            state = _refund_state(tid, call_id)
            assert state["call"]["refund_status"] == "none", (
                f"{fault_point!r}: refund_status={state['call']['refund_status']} after crash"
            )
            assert len(state["refund_rows"]) == 0, (
                f"{fault_point!r}: refund row exists after crash"
            )

            retry = _run_finalizer(
                call_id, refund=True,
                reason_code="crash_retry", source="crash_refund_retry", timeout=60,
            )
            assert retry.returncode == 0, (
                f"Refund retry after {fault_point!r} failed. err={retry.stderr[:300]}"
            )
            result = _parse_json_output(retry.stdout)
            if result is not None:
                assert result.get("refunded") is True, (
                    f"Retry refunded=False after {fault_point!r}: {result}"
                )

            state2 = _refund_state(tid, call_id)
            assert len(state2["refund_rows"]) == 1
            assert state2["call"]["refund_status"] == "refunded"
        finally:
            _cleanup(env)


# ═══════════════════════════════════════════════════════════════════════════════
# Group F — Real pg_terminate_backend connection-loss test
# ═══════════════════════════════════════════════════════════════════════════════

class TestRealPgTerminateBackend:
    """Terminate the actual PostgreSQL backend mid-transaction; verify rollback; retry.

    Mechanism:
      1. Start finalizer with BILLING_FAULT_POINT=after_wallet_lock_pause.
      2. Finalizer acquires wallet row FOR UPDATE, writes pg_backend_pid() to
         BILLING_PAUSE_FILE, then waits for BILLING_RELEASE_FILE.
      3. Test reads the backend PID, verifies it belongs to the disposable DB,
         then calls pg_terminate_backend(<pid>) from an admin connection.
      4. Finalizer's blocked DB call raises an error; it exits non-zero.
      5. PostgreSQL rolls back the transaction automatically.
      6. Test verifies: billing_finalized=False, zero debits, balance unchanged.
      7. Retry finalizer: succeeds, exactly one debit.
    """

    def setup_method(self):
        self.env = _setup_tenant_and_call()
        self._tmp_dir = tempfile.mkdtemp(prefix="ccall_pgterminate_")
        self._pause_file = os.path.join(self._tmp_dir, "backend_pid.txt")
        self._release_file = os.path.join(self._tmp_dir, "release.txt")

    def teardown_method(self):
        # Remove coordination files
        for f in (self._pause_file, self._release_file):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(self._tmp_dir)
        except OSError:
            pass
        _cleanup(self.env)

    def test_pg_terminate_backend_rolls_back(self):
        call_id = self.env["call_id"]
        initial_balance = self.env["initial_balance"]
        tid = self.env["tenant_id"]

        # Start finalizer — it will pause at after_wallet_lock_pause
        extra = {
            "BILLING_PAUSE_FILE": self._pause_file,
            "BILLING_RELEASE_FILE": self._release_file,
        }
        proc = _popen_finalizer(
            call_id,
            fault_point="after_wallet_lock_pause",
            source="pg_terminate_test",
            extra_env=extra,
        )
        finalizer_os_pid = proc.pid

        # Wait for finalizer to write its backend PID (up to 20 s)
        deadline = time.monotonic() + 20.0
        backend_pid: int | None = None
        while time.monotonic() < deadline:
            if os.path.exists(self._pause_file):
                try:
                    raw = pathlib.Path(self._pause_file).read_text().strip()
                    if raw.isdigit():
                        backend_pid = int(raw)
                        break
                except OSError:
                    pass
            time.sleep(0.1)

        assert backend_pid is not None, (
            f"Finalizer (OS PID {finalizer_os_pid}) did not write backend PID "
            f"to {self._pause_file} within 20 s"
        )

        # Verify the backend belongs to our disposable DB and call
        assert _PROC_DB_URL is not None
        db_name = _PROC_DB_URL.split("/")[-1]
        with _conn() as verify_conn:
            row = verify_conn.execute(
                "SELECT datname, application_name, state, query"
                " FROM pg_stat_activity"
                " WHERE pid = %s",
                (backend_pid,),
            ).fetchone()
            verify_conn.commit()

        assert row is not None, (
            f"Backend PID {backend_pid} not found in pg_stat_activity — "
            f"finalizer may have already exited"
        )
        assert row["datname"] == db_name, (
            f"Backend PID {backend_pid} connects to {row['datname']!r}, "
            f"expected {db_name!r} — refusing to terminate unrelated backend"
        )

        # Terminate the backend using a separate admin connection
        with _conn() as admin_conn:
            terminate_row = admin_conn.execute(
                "SELECT pg_terminate_backend(%s) AS terminated",
                (backend_pid,),
            ).fetchone()
            admin_conn.commit()

        terminated = bool(terminate_row["terminated"]) if terminate_row else False
        assert terminated, (
            f"pg_terminate_backend({backend_pid}) returned False — backend may have exited first"
        )

        # Wait for finalizer to exit (backend termination kills the blocked call)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail(
                f"Finalizer (OS PID {finalizer_os_pid}) did not exit within 30 s "
                f"after pg_terminate_backend({backend_pid})"
            )

        # Finalizer must exit non-zero (DB connection error)
        assert proc.returncode != 0, (
            f"Finalizer exited 0 after pg_terminate_backend — expected DB error exit. "
            f"stdout={proc.stdout[:300]}"
        )

        # Allow PostgreSQL a moment to complete the rollback
        time.sleep(0.3)

        # Verify full rollback
        state = _db_state(self.env, call_id)
        assert not state["call"]["billing_finalized"], (
            f"billing_finalized=True after pg_terminate_backend({backend_pid}) — "
            "transaction was not rolled back"
        )
        assert len(state["debit_rows"]) == 0, (
            f"Debit row exists after pg_terminate_backend: {state['debit_rows']}"
        )
        assert state["balance"] == initial_balance, (
            f"Balance changed after pg_terminate_backend: "
            f"{state['balance']} != {initial_balance}"
        )
        assert state["call"]["wallet_transaction_id"] is None, (
            "wallet_transaction_id is not NULL after rollback"
        )

        # Retry must succeed
        retry = _run_finalizer(call_id, source="pg_terminate_retry", timeout=60)
        assert retry.returncode == 0, (
            f"Retry after pg_terminate_backend failed. err={retry.stderr[:300]}"
        )
        state2 = _db_state(self.env, call_id)
        assert len(state2["debit_rows"]) == 1, (
            f"Retry produced {len(state2['debit_rows'])} debit rows"
        )
        assert state2["call"]["billing_finalized"] is True

        # Report evidence (printed to stdout for the evidence table in Phase 7)
        charge = state2["debit_rows"][0]["amount"]
        print(
            f"\n[F] pg_terminate_backend evidence:"
            f"\n  OS PID:           {finalizer_os_pid}"
            f"\n  PG backend PID:   {backend_pid}"
            f"\n  DB:               {db_name}"
            f"\n  call_id:          {call_id}"
            f"\n  tenant_id:        {tid}"
            f"\n  terminated:       {terminated}"
            f"\n  finalizer_exit:   {proc.returncode}"
            f"\n  balance_before:   {initial_balance}"
            f"\n  balance_after:    {state2['balance']}"
            f"\n  debit_count:      1"
            f"\n  charge:           {charge}"
            f"\n  billing_finalized: True"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Group G — Production fault-point rejection
# ═══════════════════════════════════════════════════════════════════════════════

class TestProductionFaultRejection:
    """BILLING_FAULT_POINT must be rejected at startup when FLASK_ENV != testing."""

    def setup_method(self):
        self.env = _setup_tenant_and_call()

    def teardown_method(self):
        _cleanup(self.env)

    def test_production_rejects_finalize_fault_point(self):
        """FLASK_ENV=production with BILLING_FAULT_POINT must refuse to start."""
        call_id = self.env["call_id"]
        initial_balance = self.env["initial_balance"]
        tid = self.env["tenant_id"]

        proc = _run_finalizer(
            call_id,
            fault_point="after_wallet_update",
            source="production_rejection_test",
            flask_env="production",
            timeout=30,
        )

        assert proc.returncode != 0, (
            "Expected non-zero exit when BILLING_FAULT_POINT set in production. "
            f"rc={proc.returncode} out={proc.stdout[:300]}"
        )

        # No secrets / DB URLs / passwords in output
        combined = proc.stdout + proc.stderr
        assert _PROC_DB_URL not in combined, (
            "DB URL leaked in process output during production fault rejection"
        )
        assert "password" not in combined.lower() or "PASSWORD" in combined, \
            "Suspicious 'password' string in output"  # allow env var name but not values

        # Call must remain unfinalized
        state = _db_state(self.env, call_id)
        assert not state["call"]["billing_finalized"], (
            "Call was finalized despite production fault-point rejection"
        )
        assert len(state["debit_rows"]) == 0
        assert state["balance"] == initial_balance

        print(
            f"\n[G] Production billing fault rejection evidence:"
            f"\n  call_id:          {call_id}"
            f"\n  tenant_id:        {tid}"
            f"\n  FLASK_ENV:        production"
            f"\n  fault_point:      after_wallet_update"
            f"\n  exit_code:        {proc.returncode}"
            f"\n  billing_finalized: False"
            f"\n  debit_count:      0"
            f"\n  balance:          {state['balance']} (unchanged)"
        )

    def test_production_rejects_refund_fault_point(self):
        """FLASK_ENV=production with refund BILLING_FAULT_POINT must refuse to start."""
        call_id = self.env["call_id"]
        tid = self.env["tenant_id"]
        initial_balance = self.env["initial_balance"]

        # Finalize first (in testing mode)
        fin = _run_finalizer(call_id, source="setup", timeout=60)
        assert fin.returncode == 0, f"Setup finalization failed: err={fin.stderr[:200]}"

        state_after_fin = _db_state(self.env, call_id)
        balance_after_debit = state_after_fin["balance"]

        proc = _run_finalizer(
            call_id,
            fault_point="after_refund_wallet_update",
            source="production_rejection_test",
            flask_env="production",
            refund=True,
            reason_code="production_rejection",
            timeout=30,
        )

        assert proc.returncode != 0, (
            "Expected non-zero exit for refund fault in production. "
            f"rc={proc.returncode}"
        )

        combined = proc.stdout + proc.stderr
        assert _PROC_DB_URL not in combined, "DB URL leaked in output"

        # Refund must not have been committed
        rstate = _refund_state(tid, call_id)
        assert rstate["call"]["refund_status"] == "none", (
            f"refund_status={rstate['call']['refund_status']} despite production rejection"
        )
        assert len(rstate["refund_rows"]) == 0

        # Balance unchanged from post-debit value
        final_state = _db_state(self.env, call_id)
        assert final_state["balance"] == balance_after_debit, (
            f"Balance changed during production rejection: "
            f"{final_state['balance']} != {balance_after_debit}"
        )

        print(
            f"\n[G] Production refund fault rejection evidence:"
            f"\n  call_id:          {call_id}"
            f"\n  tenant_id:        {tid}"
            f"\n  fault_point:      after_refund_wallet_update"
            f"\n  exit_code:        {proc.returncode}"
            f"\n  refund_status:    none"
            f"\n  refund_count:     0"
            f"\n  balance:          {final_state['balance']} (unchanged from post-debit)"
        )
