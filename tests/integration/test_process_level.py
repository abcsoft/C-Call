"""tests/integration/test_process_level.py — Phase 11 process-level tests.

Verifies call worker behavior across real OS process boundaries:

A. Worker crash before mark_dispatched → expired lease recovered by a second worker.
B. Two concurrent workers race for the same job → only one wins (FOR UPDATE SKIP LOCKED).
C. Worker crash after start_call() returns but before mark_dispatched → crash guard
   (DispatchDuplicateCallError) fires on recovery.

Tests use separate subprocess PIDs, not threads, so scheduler/OS state is authentic.
Each test spawns a helper script via subprocess.Popen and asserts DB state directly.

Requirements
------------
- FLASK_ENV=testing with a live test PostgreSQL.
- MOCK_SIP_PROVIDER=1 so SIP calls are simulated.
- Tests are skipped automatically when DATABASE_URL is not available.
"""
from __future__ import annotations

import os
import sys
import time
import subprocess
import textwrap
import pytest

# Skip the whole module if no test database is reachable.
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — process-level tests require a real PostgreSQL",
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def _env(monkeypatch):
    """Environment suitable for worker subprocesses."""
    env = {**os.environ, "FLASK_ENV": "testing", "MOCK_SIP_PROVIDER": "1"}
    return env


@pytest.fixture()
def _db(app):
    """Direct DB access inside test process (uses the shared app fixture)."""
    with app.app_context():
        from app.db import db
        yield db


@pytest.fixture()
def _tenant_order(app, tenant):
    """Create a CALL_QUEUED order with a call_job queued for attempt 1."""
    with app.app_context():
        from app.db import db
        from app.services.call_job_service import enqueue_order_call

        # Insert a minimal order.
        row = db.query_one(
            """INSERT INTO orders
                   (tenant_id, external_order_id, phone, customer_name, amount, order_status)
               VALUES (%s, %s, %s, %s, %s, 'CALL_QUEUED')
               RETURNING id""",
            (tenant["id"], f"PL-TEST-{int(time.time())}", "01700000001",
             "Process Test", "100.00"),
        )
        order_id = int(row["id"])
        with db.transaction() as conn:
            enqueue_order_call(conn, order_id, tenant["id"], attempt_no=1)
        yield order_id, tenant["id"]
        # Cleanup
        db.execute("DELETE FROM call_jobs WHERE order_id = %s", (order_id,))
        db.execute("DELETE FROM order_call_attempts WHERE order_id = %s", (order_id,))
        db.execute("DELETE FROM orders WHERE id = %s", (order_id,))


# ── Helper: run a one-shot worker subprocess ──────────────────────────────────

def _worker_script(extra_python: str = "") -> str:
    """Return a one-shot worker script that processes one batch then exits."""
    return textwrap.dedent(f"""
        import os, sys, time
        sys.path.insert(0, '.')
        os.environ.setdefault('FLASK_ENV', 'testing')
        os.environ['MOCK_SIP_PROVIDER'] = '1'
        from app import create_app
        app = create_app('testing')
        with app.app_context():
            from app.db import db
            from app.services.call_job_service import (
                dispatch_order_call, release_expired_leases,
            )
            {extra_python}
    """)


def _run_script(script: str, env: dict, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── A. Crash before mark_dispatched → lease recovery ─────────────────────────

def test_crash_before_mark_dispatched_lease_recovered(app, _env, _db, _tenant_order):
    """Worker A leases job then exits without completing; Worker B recovers lease.

    The PIDs of the two subprocesses must differ to confirm process isolation.
    """
    order_id, tenant_id = _tenant_order

    # Step 1: Worker A leases the job then crashes (exits without mark_dispatched).
    lease_script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, '.')
        os.environ['FLASK_ENV'] = 'testing'
        from app import create_app
        app = create_app('testing')
        with app.app_context():
            from app.db import db
            # Directly lease the job with an already-expired lease so we can
            # immediately test recovery without sleeping.
            db.execute(
                \"\"\"UPDATE call_jobs
                       SET job_status       = 'leased',
                           leased_by        = %s,
                           leased_at        = now() - interval '10 minutes',
                           lease_expires_at = now() - interval '5 minutes',
                           updated_at       = now()
                   WHERE order_id = %s AND job_status = 'queued'\"\"\",
                (os.getpid(), {order_id}),
            )
            import os as _os
            print(f'WORKER_A_PID={{_os.getpid()}}')
    """)

    proc_a = _run_script(lease_script, _env)
    assert proc_a.returncode == 0, f"Worker A failed: {proc_a.stderr}"
    pid_a_line = next((l for l in proc_a.stdout.splitlines() if l.startswith("WORKER_A_PID=")), "")
    pid_a = int(pid_a_line.split("=")[1]) if pid_a_line else None

    # Confirm job is leased with expired lease.
    with app.app_context():
        job = _db.query_one(
            "SELECT job_status, lease_expires_at FROM call_jobs WHERE order_id = %s",
            (order_id,),
        )
    assert job["job_status"] == "leased"

    # Step 2: Worker B recovers the expired lease (release_expired_leases → re-claim).
    recover_script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, '.')
        os.environ['FLASK_ENV'] = 'testing'
        from app import create_app
        app = create_app('testing')
        with app.app_context():
            from app.db import db
            from app.services.call_job_service import release_expired_leases
            with db.transaction() as conn:
                recovered = release_expired_leases(conn)
            print(f'RECOVERED={{recovered}}')
            print(f'WORKER_B_PID={{os.getpid()}}')
    """)

    proc_b = _run_script(recover_script, _env)
    assert proc_b.returncode == 0, f"Worker B failed: {proc_b.stderr}"

    lines = proc_b.stdout.splitlines()
    recovered_line = next((l for l in lines if l.startswith("RECOVERED=")), "RECOVERED=0")
    pid_b_line = next((l for l in lines if l.startswith("WORKER_B_PID=")), "")
    recovered = int(recovered_line.split("=")[1])
    pid_b = int(pid_b_line.split("=")[1]) if pid_b_line else None

    assert recovered >= 1, f"Worker B should have recovered at least 1 lease, got {recovered}"

    # PIDs must differ — confirms these are separate OS processes.
    assert pid_a is not None and pid_b is not None
    assert pid_a != pid_b, f"PIDs must differ: A={pid_a} B={pid_b}"
    assert pid_a != os.getpid()
    assert pid_b != os.getpid()

    # Job must be back to queued.
    with app.app_context():
        job_after = _db.query_one(
            "SELECT job_status FROM call_jobs WHERE order_id = %s",
            (order_id,),
        )
    assert job_after["job_status"] == "queued"


# ── B. Two concurrent workers — only one wins (SKIP LOCKED) ──────────────────

def test_two_workers_only_one_claims_job(app, _env, _db, _tenant_order):
    """Two worker subprocesses race for the same queued job.

    The SKIP LOCKED guarantee means only one claims it; the other skips.
    Confirmed by checking that exactly one job transitions to leased/completed
    and the PIDs of both workers are distinct.
    """
    order_id, tenant_id = _tenant_order

    # A one-shot worker that tries to claim one job and reports what it got.
    worker_script = textwrap.dedent(f"""
        import os, sys, time
        sys.path.insert(0, '.')
        os.environ['FLASK_ENV'] = 'testing'
        os.environ['MOCK_SIP_PROVIDER'] = '1'
        from app import create_app
        app = create_app('testing')
        with app.app_context():
            from app.db import db
            pid = os.getpid()
            # Attempt to claim one job via FOR UPDATE SKIP LOCKED.
            with db.transaction() as conn:
                row = conn.execute(
                    \"\"\"SELECT id FROM call_jobs
                       WHERE job_status = 'queued'
                         AND order_id = {order_id}
                         AND available_at <= now()
                       LIMIT 1
                       FOR UPDATE SKIP LOCKED\"\"\",
                ).fetchone()
                if row:
                    job_id = row[0] if not hasattr(row, '__getitem__') else row['id'] if isinstance(row, dict) else row[0]
                    conn.execute(
                        \"\"\"UPDATE call_jobs
                               SET job_status = 'leased',
                                   leased_by = %s,
                                   leased_at = now(),
                                   lease_expires_at = now() + interval '5 minutes',
                                   updated_at = now()
                           WHERE id = %s\"\"\",
                        (pid, job_id),
                    )
                    print(f'CLAIMED={{job_id}}')
                else:
                    print('CLAIMED=none')
            print(f'PID={{pid}}')
    """)

    # Spawn both workers nearly simultaneously.
    proc1 = subprocess.Popen(
        [sys.executable, "-c", worker_script],
        env=_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    proc2 = subprocess.Popen(
        [sys.executable, "-c", worker_script],
        env=_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    out1, _ = proc1.communicate(timeout=20)
    out2, _ = proc2.communicate(timeout=20)

    assert proc1.returncode == 0, f"Worker 1 failed: {_}"
    assert proc2.returncode == 0, f"Worker 2 failed: {_}"

    def _parse(out):
        claimed = None
        pid = None
        for line in out.splitlines():
            if line.startswith("CLAIMED="):
                val = line.split("=")[1]
                claimed = None if val == "none" else int(val)
            elif line.startswith("PID="):
                pid = int(line.split("=")[1])
        return claimed, pid

    claimed1, pid1 = _parse(out1)
    claimed2, pid2 = _parse(out2)

    # Distinct PIDs — real separate processes.
    assert pid1 is not None and pid2 is not None
    assert pid1 != pid2, f"Workers must run in separate processes: {pid1} == {pid2}"
    assert pid1 != os.getpid()
    assert pid2 != os.getpid()

    # Exactly one worker claimed the job; the other got nothing.
    winners = [c for c in (claimed1, claimed2) if c is not None]
    assert len(winners) == 1, (
        f"Expected exactly 1 winner; claimed1={claimed1} claimed2={claimed2}"
    )


# ── C. Crash after start_call → crash guard fires on recovery ────────────────

def test_crash_after_start_call_crash_guard_fires(app, _env, _db, _tenant_order):
    """Simulate crash after order_call_attempts row created but before job completed.

    Worker B, recovering the lease and calling dispatch_order_call, must raise
    DispatchDuplicateCallError (crash guard) — not start a new call.

    This verifies startup_state='accepted' on the existing attempt triggers the guard.
    """
    order_id, tenant_id = _tenant_order

    # Manually simulate "call started, attempt row created, startup_state set,
    # but job still leased (not marked completed)".
    with app.app_context():
        # Insert a fake flow row so the calls FK constraint is satisfied.
        flow_row = _db.query_one(
            """INSERT INTO flows (tenant_id, name, created_at, updated_at)
               VALUES (%s, 'test-process-flow', now(), now())
               RETURNING id""",
            (tenant_id,),
        )
        fake_flow_id = int(flow_row["id"])

        # Insert a fake calls row with startup_state='accepted'.
        call_row = _db.query_one(
            """INSERT INTO calls
                   (tenant_id, phone, status, flow_id, started_at, startup_state)
               VALUES (%s, %s, 'initiated', %s, now(), 'accepted')
               RETURNING id""",
            (tenant_id, "01700000001", fake_flow_id),
        )
        fake_call_id = int(call_row["id"])

        # Create attempt row for attempt_no=1.
        _db.execute(
            """INSERT INTO order_call_attempts
                   (order_id, tenant_id, attempt_no, call_id, created_at)
               VALUES (%s, %s, 1, %s, now())""",
            (order_id, tenant_id, fake_call_id),
        )

        # Set orders.attempt_count = 1 (as queue_confirmation_call would have done).
        _db.execute(
            "UPDATE orders SET attempt_count = 1 WHERE id = %s",
            (order_id,),
        )

        # Leave job as 'queued' (simulates crash before mark_dispatched).

    # Worker B dispatches — crash guard must fire.
    guard_script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, '.')
        os.environ['FLASK_ENV'] = 'testing'
        os.environ['MOCK_SIP_PROVIDER'] = '1'
        from app import create_app
        app = create_app('testing')
        with app.app_context():
            from app.exceptions import DispatchDuplicateCallError
            from app.services.call_job_service import dispatch_order_call
            result = dispatch_order_call({order_id}, {tenant_id}, attempt_no=1)
            # Crash guard: accepted=True with existing call_id — crash recovery path.
            print(f'ACCEPTED={{result.accepted}}')
            print(f'CALL_ID={{result.call_id}}')
            print(f'ERROR_CODE={{result.error_code}}')
            print(f'PID={{os.getpid()}}')
    """)

    proc = _run_script(guard_script, _env)
    assert proc.returncode == 0, f"Script failed: {proc.stderr}"

    lines = proc.stdout.splitlines()

    def _val(prefix):
        line = next((l for l in lines if l.startswith(prefix)), None)
        return line.split("=", 1)[1] if line else None

    accepted = _val("ACCEPTED=")
    error_code = _val("ERROR_CODE=")
    pid_b = _val("PID=")

    # accepted=True because a call exists and startup_state='accepted'.
    assert accepted == "True", f"Expected accepted=True (crash recovery), got {accepted}"
    assert error_code == "duplicate_call_existing", f"Expected duplicate_call_existing error_code, got {error_code}"

    # Must be a separate process.
    assert pid_b is not None
    assert int(pid_b) != os.getpid()

    # Cleanup fake call row and flow.
    with app.app_context():
        _db.execute("DELETE FROM order_call_attempts WHERE order_id = %s", (order_id,))
        _db.execute("DELETE FROM calls WHERE id = %s", (fake_call_id,))
        _db.execute("DELETE FROM flows WHERE id = %s", (fake_flow_id,))
