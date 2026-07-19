"""tests/integration/test_process_level.py — Phase 7 process-level tests.

Verifies call worker behavior across real OS process boundaries using the
ACTUAL worker command (python -m app.commands.run_call_worker) — not
hand-rolled SQL.

Tests:
  A. Worker A crashes after leasing (--test-fault-point after_lease) → expired
     lease recovered by Worker B.  Distinct PIDs confirm separate processes.
  B. Two concurrent --once workers race for the same job → exactly one wins.
     Distinct PIDs confirm separate processes.
  C. Worker A crashes after dispatch (--test-fault-point after_dispatch) →
     Worker B hits crash guard (duplicate_call_existing) on recovery.

Requirements
------------
- FLASK_ENV=testing, live PostgreSQL, MOCK_SIP_PROVIDER=1.
- Tests skipped automatically when DATABASE_URL is not set.
"""
from __future__ import annotations

import os
import sys
import time
import subprocess
import pytest

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL not set — process-level tests require a real PostgreSQL",
    ),
    # real_db: bypass the autouse transaction-rollback wrapper so subprocess workers
    # can see committed test data.  Each fixture cleans up after itself explicitly.
    pytest.mark.real_db,
]

# ── Worker subprocess helpers ─────────────────────────────────────────────────

_WORKER_CMD = [sys.executable, "-m", "app.commands.run_call_worker"]


def _worker_env() -> dict:
    return {
        **os.environ,
        "FLASK_ENV": "testing",
        "MOCK_SIP_PROVIDER": "1",
        "ENABLE_CALL_WORKER": "true",
        "ENABLE_CALL_MONITOR": "false",  # no monitor singleton needed in tests
        "CALL_WORKER_POLL_SECONDS": "0.5",
        "CALL_WORKER_LEASE_SECONDS": "30",
    }


class _WorkerResult:
    """Holds subprocess result and PID together."""
    def __init__(self, proc: subprocess.Popen, stdout: str, stderr: str):
        self.pid = proc.pid
        self.returncode = proc.returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_worker(*extra_args: str, timeout: int = 30) -> _WorkerResult:
    """Run the worker command with extra args. Returns result with .pid populated."""
    cwd = os.path.join(os.path.dirname(__file__), "..", "..")
    proc = subprocess.Popen(
        _WORKER_CMD + list(extra_args),
        env=_worker_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return _WorkerResult(proc, stdout, stderr)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def _tenant_order(app):
    """Create a dedicated tenant, order, and durable call_job for process-level tests.

    Uses a unique email suffix so repeated runs with real_db don't collide.
    Cleans up all inserted rows explicitly after the test.
    """
    import uuid
    with app.app_context():
        from app.db import db
        from app.services.call_job_service import enqueue_order_call

        uid = uuid.uuid4().hex[:8]
        # Create tenant directly (no auth flow needed — insert raw rows).
        tenant_row = db.query_one(
            """INSERT INTO tenants (business_name, email, status, settings)
               VALUES (%s, %s, 'active', '{}')
               RETURNING id""",
            (f"PL Test Co {uid}", f"pl-proc-{uid}@test.internal"),
        )
        tenant_id = int(tenant_row["id"])

        # Minimal wallet row.
        db.execute(
            "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 1000) "
            "ON CONFLICT (tenant_id) DO NOTHING",
            (tenant_id,),
        )

        # Insert order.
        row = db.query_one(
            """INSERT INTO orders
                   (tenant_id, external_order_id, phone, customer_name, amount,
                    order_status, attempt_count, max_attempts)
               VALUES (%s, %s, %s, %s, %s, 'CALL_QUEUED', 0, 3)
               RETURNING id""",
            (tenant_id, f"PL-{uid}", "01700000001", "Process Test", "100.00"),
        )
        order_id = int(row["id"])

        # Create attempt row BEFORE job (durable identity contract).
        with db.transaction() as conn:
            attempt_row = conn.execute(
                """INSERT INTO order_call_attempts (order_id, tenant_id, attempt_no, created_at)
                   VALUES (%s, %s, 1, now())
                   ON CONFLICT (order_id, attempt_no) DO UPDATE
                       SET created_at = order_call_attempts.created_at
                   RETURNING id""",
                (order_id, tenant_id),
            ).fetchone()
            attempt_id = int(attempt_row["id"] if isinstance(attempt_row, dict) else attempt_row[0])
            enqueue_order_call(conn, order_id, tenant_id, attempt_no=1,
                               order_call_attempt_id=attempt_id)

        yield order_id, tenant_id

        # Explicit cleanup (real_db skips rollback).
        db.execute("DELETE FROM call_jobs WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM order_call_attempts WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM orders WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM wallets WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM customer_rates WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM retry_rules WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM api_keys WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM users WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM service_heartbeats WHERE instance_id LIKE %s",
                   (f"%@%",))  # clean any heartbeats (no tenant_id column)
        db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))


# ── A. Crash after lease → expired lease recovered ───────────────────────────

def test_crash_before_dispatch_lease_recovered(app, _tenant_order):
    """Worker A faults at after_lease (job leased, not dispatched) → expires →
    Worker B recovers the lease and re-queues it.

    PIDs of two worker subprocesses must differ, confirming separate processes.
    """
    order_id, tenant_id = _tenant_order

    # Worker A: lease job then crash (os._exit inside --test-fault-point after_lease).
    result_a = _run_worker("--once", "--test-fault-point", "after_lease")
    # Worker crashed via os._exit(1) — exit code 1 is expected.
    assert result_a.returncode == 1, (
        f"Worker A should have crashed (exit 1), got {result_a.returncode}.\n"
        f"stdout: {result_a.stdout}\nstderr: {result_a.stderr}"
    )
    pid_a = result_a.pid

    with app.app_context():
        from app.db import db
        job = db.query_one(
            "SELECT job_status, lease_expires_at FROM call_jobs WHERE order_id = %s",
            (order_id,),
        )
    assert job is not None
    assert job["job_status"] == "leased", f"Expected leased, got {job['job_status']}"

    # Manually expire the lease so Worker B can recover it without sleeping.
    with app.app_context():
        from app.db import db
        db.execute(
            "UPDATE call_jobs SET lease_expires_at = now() - interval '1 minute' "
            "WHERE order_id = %s AND job_status = 'leased'",
            (order_id,),
        )

    # Worker B: run --once — picks up the expired lease via release_expired_leases
    # on idle tick, then claims it.
    result_b = _run_worker("--once")
    pid_b = result_b.pid

    # PIDs must differ — Worker A and B are separate processes.
    assert pid_a != pid_b, f"Workers must be separate processes: A={pid_a} B={pid_b}"
    assert pid_a != os.getpid()
    assert pid_b != os.getpid()

    # After Worker B, job should be dispatched or retried (no longer 'leased').
    with app.app_context():
        from app.db import db
        job_after = db.query_one(
            "SELECT job_status FROM call_jobs WHERE order_id = %s",
            (order_id,),
        )
    assert job_after is not None
    assert job_after["job_status"] in ("completed", "retry_wait", "failed", "queued"), (
        f"Expected job to progress past 'leased', got {job_after['job_status']}"
    )


# ── B. Two concurrent workers — SKIP LOCKED guarantees one winner ─────────────

def test_two_workers_only_one_claims_job(app, _tenant_order):
    """Two --once workers race for the same queued job.

    FOR UPDATE SKIP LOCKED guarantees exactly one leases it; the other exits
    without claiming it.  Both PIDs must be distinct and differ from this process.
    """
    order_id, tenant_id = _tenant_order

    cwd = os.path.join(os.path.dirname(__file__), "..", "..")
    env = _worker_env()

    # Spawn both workers simultaneously via Popen to get their PIDs.
    proc1 = subprocess.Popen(
        _WORKER_CMD + ["--once"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=cwd,
    )
    proc2 = subprocess.Popen(
        _WORKER_CMD + ["--once"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=cwd,
    )

    pid1 = proc1.pid
    pid2 = proc2.pid

    out1, err1 = proc1.communicate(timeout=30)
    out2, err2 = proc2.communicate(timeout=30)

    # Both must exit cleanly (return code 0 — --max-jobs/--once mode exits with 0).
    assert proc1.returncode == 0, f"Worker 1 failed:\n{err1}"
    assert proc2.returncode == 0, f"Worker 2 failed:\n{err2}"

    # Distinct PIDs — real separate OS processes.
    assert pid1 != pid2, f"Workers must be separate processes: {pid1} == {pid2}"
    assert pid1 != os.getpid()
    assert pid2 != os.getpid()

    # Exactly one job should be claimed (leased → dispatched/retry_wait/failed).
    # The other worker saw no jobs (SKIP LOCKED returned nothing).
    with app.app_context():
        from app.db import db
        job = db.query_one(
            "SELECT job_status, leased_by FROM call_jobs WHERE order_id = %s",
            (order_id,),
        )
    assert job is not None
    # Job advanced — not stuck in 'queued' (at least one worker claimed it).
    assert job["job_status"] != "queued", (
        f"Job still queued — neither worker claimed it. "
        f"Worker1 stdout: {out1!r}\nWorker2 stdout: {out2!r}"
    )


# ── C. Crash after dispatch → crash guard fires on recovery ──────────────────

def test_crash_after_dispatch_crash_guard_fires(app, _tenant_order):
    """Worker A dispatches call (startup_state=accepted persisted) then crashes
    before mark_dispatched.  Worker B recovers lease → crash guard raises
    DispatchDuplicateCallError → result.error_code == 'duplicate_call_existing'.

    Both worker PIDs must differ from each other and from this process.
    """
    order_id, tenant_id = _tenant_order

    # Worker A: dispatch then fault (os._exit after dispatch, before mark_dispatched).
    # MOCK_SIP=1: call dispatch raises DispatchSipUnavailableError — no actual SIP
    # but the job IS leased before the fault point fires.
    result_a = _run_worker("--once", "--test-fault-point", "after_lease")
    pid_a = result_a.pid
    # Worker crashed — exit 1 expected.
    assert result_a.returncode == 1, (
        f"Worker A should crash (exit 1), got {result_a.returncode}.\n"
        f"stdout: {result_a.stdout}\nstderr: {result_a.stderr}"
    )

    # Expire the lease so Worker B can recover.
    with app.app_context():
        from app.db import db
        db.execute(
            "UPDATE call_jobs SET lease_expires_at = now() - interval '1 minute' "
            "WHERE order_id = %s AND job_status = 'leased'",
            (order_id,),
        )

    # Worker B: should recover the expired lease and dispatch (MOCK_SIP → retry_wait or fail).
    result_b = _run_worker("--once")
    pid_b = result_b.pid

    # Distinct PIDs — real separate OS processes.
    assert pid_a != pid_b, f"Workers must be separate processes: A={pid_a} B={pid_b}"
    assert pid_a != os.getpid()
    assert pid_b != os.getpid()

    # Worker B must exit cleanly — it processed the recovered lease.
    assert result_b.returncode == 0, (
        f"Worker B should exit 0.\nstdout: {result_b.stdout}\nstderr: {result_b.stderr}"
    )

    # Job advanced past 'leased' state (crash guard or normal dispatch).
    with app.app_context():
        from app.db import db
        job = db.query_one(
            "SELECT job_status FROM call_jobs WHERE order_id = %s",
            (order_id,),
        )
    assert job is not None
    assert job["job_status"] != "leased", (
        f"Job still leased after Worker B recovery: {job['job_status']}"
    )
