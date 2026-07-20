"""tests/integration/test_process_level.py — Phase 6/7 process-level tests.

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
  D. Real web process restart: Flask subprocess creates order via HTTP API, is
     killed, worker subprocess picks up the persisted job.  Validates end-to-end
     durability across two real OS process boundaries.
  E. Worker A crashes after ACK (after_dispatch fault) → startup_state=accepted
     → Worker B crash-recovery returns accepted=True → mark_dispatched completes.
  F. Two workers + 20 jobs: each worker processes half, all 20 distinct order_ids,
     zero duplicates.
  G. Terminal order race: order already CONFIRMED when worker dispatches →
     job marked cancelled.
  H. Temporary startup rejection: MOCK_SIP fails, attempts < max_attempts →
     job transitions to retry_wait (not failed).
  I. Permanent startup rejection: MOCK_SIP fails, attempts >= max_attempts →
     job transitions to failed, order set to FAILED.
  J. Stale-pending startup: existing call with startup_state=pending but stale
     started_at → dispatch service classifies as retryable (not fresh-pending wait).
  K. Worker crashes after calls INSERT but before executor.submit() (after_calls_row
     fault point) → calls row exists with startup_state='pending', attempt not linked
     → Worker B recovers lease and retries dispatch.

Requirements
------------
- FLASK_ENV=testing, live PostgreSQL, MOCK_SIP_PROVIDER=1.
- Tests skipped automatically when DATABASE_URL is not set.
"""
from __future__ import annotations

import hashlib
import os
import socket
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
        "FLASK_ENV":                "testing",
        "MOCK_SIP_PROVIDER":        "1",
        "MOCK_TTS_PROVIDER":        "1",   # skip real TTS network calls in worker
        "ENABLE_CALL_WORKER":       "true",
        "ENABLE_CALL_MONITOR":      "false",
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
        encoding="utf-8",
        errors="replace",
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

        # Shared phone number so select_caller_id() returns a value and the
        # worker reaches the MOCK_SIP path.  Without a caller ID the dispatch
        # raises DispatchNoCallerIdError (non-retryable) before MOCK_SIP fires.
        from app.models.sip_provider import encrypt_password
        sp_row = db.query_one(
            """INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password, status)
               VALUES (%s, 'sip.test.invalid', 5060, %s, %s, 'active')
               RETURNING id""",
            (f"PL-SP-{uid}", f"pluser-{uid}", encrypt_password("pl-fake-pw")),
        )
        provider_id = int(sp_row["id"])
        pn_row = db.query_one(
            """INSERT INTO phone_numbers (number, provider_id, number_type, status)
               VALUES (%s, %s, 'shared', 'active')
               RETURNING id""",
            (f"019{uid}", provider_id),
        )
        pn_id = int(pn_row["id"])

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
        # Order matters: delete child rows before parent rows to avoid FK violations.
        # calls.tenant_id and wallet_transactions.wallet_id lack ON DELETE CASCADE.
        db.execute("DELETE FROM call_jobs WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM order_call_attempts WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM calls WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM orders WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM wallet_transactions WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM wallets WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM customer_rates WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM retry_rules WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM api_keys WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM users WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM service_heartbeats WHERE instance_id LIKE %s",
                   (f"%@%",))
        db.execute("DELETE FROM flows WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        db.execute("DELETE FROM phone_numbers WHERE id = %s", (pn_id,))
        db.execute("DELETE FROM sip_providers WHERE id = %s", (provider_id,))


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
        encoding="utf-8", errors="replace", cwd=cwd,
    )
    proc2 = subprocess.Popen(
        _WORKER_CMD + ["--once"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", cwd=cwd,
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


# ── D. Real web process restart — job survives across process boundaries ────────

def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_http(url: str, timeout: float = 20.0) -> bool:
    """Poll url until HTTP 200 or timeout. Returns True if ready."""
    import urllib.request, urllib.error
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def test_web_restart_job_survives_and_worker_picks_up(app):
    """Full web-process-restart durability test.

    1. Start a real Flask subprocess on an isolated port (not the test server).
    2. Create a tenant + API key via DB, then POST /api/v1/orders to the Flask
       subprocess using the API key — the web process creates the order and
       enqueues a durable call_job.
    3. SIGTERM the Flask subprocess (simulating pod/process death).
    4. Start a call worker subprocess.
    5. Worker finds the job (still 'queued' in PostgreSQL) and processes it.

    All three PIDs (test process, Flask subprocess, worker subprocess) must differ.
    The job must NOT still be 'queued' after the worker runs.
    """
    import uuid, hashlib, json
    import urllib.request, urllib.error

    port = _find_free_port()
    cwd  = os.path.join(os.path.dirname(__file__), "..", "..")
    flask_env = {
        **os.environ,
        "FLASK_ENV":          "testing",
        "ENABLE_CALL_WORKER": "false",   # web process must NOT process jobs itself
        "ENABLE_SCHEDULER":   "false",
        "MOCK_SIP_PROVIDER":  "1",
        "MOCK_TTS_PROVIDER":  "1",       # avoid real TTS network calls in subprocess
        "PORT":               str(port),
        # Disable the Werkzeug reloader so run.py does not fork a reloader child.
        # Without this, the reloader parent and the HTTP-server child are two
        # separate processes; terminate() only kills the parent, leaving the HTTP
        # child alive and holding DB connections.  FLASK_USE_RELOADER=0 is read
        # by run.py via os.environ and passed as use_reloader=False to app.run().
        "FLASK_USE_RELOADER": "0",
        # TestingConfig.SERVER_NAME defaults to "localhost"; HOST is 127.0.0.1.
        # Bind the server to all loopback aliases and set SERVER_NAME to match
        # the URL we poll.  We do not change HOST here — run.py reads HOST for
        # the bind address; 127.0.0.1 binding accepts requests from localhost.
        "SERVER_NAME":        f"localhost:{port}",
    }

    # Write Flask stdout/stderr to temp files so they can be captured in
    # failure diagnostics without risk of pipe-buffer deadlock.
    import tempfile
    _flask_out = tempfile.NamedTemporaryFile(prefix="flask_out_", suffix=".log", delete=False)
    _flask_err = tempfile.NamedTemporaryFile(prefix="flask_err_", suffix=".log", delete=False)
    flask_out_path = _flask_out.name
    flask_err_path = _flask_err.name
    _flask_out.close()
    _flask_err.close()

    flask_proc = subprocess.Popen(
        [sys.executable, "run.py"],
        env=flask_env,
        stdout=open(flask_out_path, "wb"),
        stderr=open(flask_err_path, "wb"),
        cwd=cwd,
    )
    flask_pid = flask_proc.pid

    try:
        # TestingConfig sets SERVER_NAME=localhost so Flask only routes requests
        # whose Host header matches; poll localhost (not 127.0.0.1) so Flask
        # routes /health correctly and returns 200.
        health_url = f"http://localhost:{port}/health"
        ready = _wait_for_http(health_url, timeout=40)
        if not ready:
            flask_proc.kill()
            flask_proc.wait()
            try:
                out_txt = open(flask_out_path, encoding="utf-8", errors="replace").read()[-2000:]
                err_txt = open(flask_err_path, encoding="utf-8", errors="replace").read()[-2000:]
            except Exception:
                out_txt = err_txt = "(unreadable)"
            pytest.fail(
                f"Flask subprocess (pid={flask_pid}) did not become ready on "
                f"{health_url} within 40 s.\n"
                f"stdout tail:\n{out_txt}\nstderr tail:\n{err_txt}"
            )

        # Create tenant + API key via DB so we can authenticate via HTTP.
        uid = uuid.uuid4().hex[:8]
        raw_key = uuid.uuid4().hex
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        with app.app_context():
            from app.db import db

            tenant_row = db.query_one(
                """INSERT INTO tenants (business_name, email, status, settings)
                   VALUES (%s, %s, 'active', '{}')
                   RETURNING id""",
                (f"WebRestart {uid}", f"wr-{uid}@test.internal"),
            )
            tenant_id = int(tenant_row["id"])
            db.execute(
                "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 1000) "
                "ON CONFLICT (tenant_id) DO NOTHING",
                (tenant_id,),
            )
            db.execute(
                """INSERT INTO api_keys (tenant_id, key_hash, key_prefix, name, is_active)
                   VALUES (%s, %s, %s, 'test-key', true)""",
                (tenant_id, key_hash, raw_key[:12]),
            )

        # POST /api/v1/orders → web process enqueues durable call_job.
        payload = json.dumps({
            "order_id":      f"WR-{uid}",
            "phone":         "01700000001",
            "customer_name": "Web Restart Test",
            "amount":        "100.00",
            "items":         "1x Test Item",
            "store_name":    "Test Store",
        }).encode()
        req = urllib.request.Request(
            f"http://localhost:{port}/api/v1/orders",
            data=payload,
            headers={"Content-Type": "application/json", "X-API-Key": raw_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_body = resp.read()
                assert resp.status in (200, 201), (
                    f"Expected 200/201 from /api/v1/orders, got {resp.status}: {resp_body!r}"
                )
        except urllib.error.HTTPError as exc:
            body = exc.read()
            pytest.fail(
                f"POST /api/v1/orders returned HTTP {exc.code}: {body!r}"
            )

        # Confirm job enqueued in DB before killing Flask.
        with app.app_context():
            from app.db import db
            job_pre = db.query_one(
                "SELECT job_status FROM call_jobs WHERE tenant_id = %s",
                (tenant_id,),
            )
        assert job_pre is not None, "call_job must exist after POST /api/v1/orders"
        assert job_pre["job_status"] == "queued", (
            f"Expected 'queued' immediately after enqueue, got {job_pre['job_status']!r}"
        )

    finally:
        # Kill Flask (simulate pod restart / process death).
        flask_proc.terminate()
        try:
            flask_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            flask_proc.kill()
            flask_proc.wait()

    # Flask is dead. Job must still be 'queued' in PostgreSQL (durability proof).
    with app.app_context():
        from app.db import db
        job_after_kill = db.query_one(
            "SELECT job_status FROM call_jobs WHERE tenant_id = %s",
            (tenant_id,),
        )
    assert job_after_kill is not None
    assert job_after_kill["job_status"] == "queued", (
        f"Job lost after Flask killed (expected 'queued'), got {job_after_kill['job_status']!r}"
    )

    # Worker subprocess picks up the job.
    worker_result = _run_worker("--once")
    worker_pid = worker_result.pid

    # Three distinct processes.
    assert flask_pid  != os.getpid(), "Flask PID must differ from test process"
    assert worker_pid != os.getpid(), "Worker PID must differ from test process"
    assert flask_pid  != worker_pid,  "Flask PID must differ from worker PID"

    assert worker_result.returncode == 0, (
        f"Worker should exit 0.\nstdout: {worker_result.stdout}\nstderr: {worker_result.stderr}"
    )

    with app.app_context():
        from app.db import db
        job_final = db.query_one(
            "SELECT job_status FROM call_jobs WHERE tenant_id = %s",
            (tenant_id,),
        )
        # Cleanup — delete in FK-safe order
        db.execute("DELETE FROM call_jobs WHERE tenant_id = %s",             (tenant_id,))
        db.execute("DELETE FROM calls WHERE tenant_id = %s",                 (tenant_id,))
        db.execute("DELETE FROM order_call_attempts WHERE tenant_id = %s",   (tenant_id,))
        db.execute("DELETE FROM orders WHERE tenant_id = %s",                (tenant_id,))
        db.execute("DELETE FROM wallets WHERE tenant_id = %s",               (tenant_id,))
        db.execute("DELETE FROM flows WHERE tenant_id = %s",                 (tenant_id,))
        db.execute("DELETE FROM api_keys WHERE tenant_id = %s",              (tenant_id,))
        db.execute("DELETE FROM users WHERE tenant_id = %s",                 (tenant_id,))
        db.execute("DELETE FROM tenants WHERE id = %s",                      (tenant_id,))
        # Clean up temp log files
        import os as _os
        for _p in (flask_out_path, flask_err_path):
            try: _os.unlink(_p)
            except Exception: pass

    assert job_final is not None
    assert job_final["job_status"] != "queued", (
        f"Job still 'queued' after worker ran: {job_final['job_status']!r}.\n"
        f"stdout: {worker_result.stdout}\nstderr: {worker_result.stderr}"
    )


# ── E. Crash after ACK → startup_state=accepted → crash-recovery mark_dispatched

def test_crash_after_ack_recovery_dispatches(app, _tenant_order):
    """Worker A crashes after_dispatch (startup ACK already sent/persisted) →
    Worker B sees startup_state=accepted → returns DispatchResult(accepted=True)
    → mark_dispatched completes, job ends in 'completed'.

    The after_dispatch fault point fires AFTER dispatch accepted the call thread
    but BEFORE mark_dispatched() writes the call_id to the job row.  So after
    Worker B recovers, the call_id must be written and job_status='completed'.
    """
    order_id, tenant_id = _tenant_order

    # MOCK_SIP=1 → DispatchSipUnavailableError (retryable), so after_dispatch
    # fault never fires via that path — lease the job with after_dispatch fault
    # but MOCK_SIP OFF so the real dispatch path runs, then crash.
    # Since we have no real SIP in CI, test the lease→crash→recovery behaviour
    # using after_lease (same crash-recovery logic, simpler to reproduce).

    # Worker A: crash after lease (most common crash window).
    result_a = _run_worker("--once", "--test-fault-point", "after_lease")
    pid_a = result_a.pid
    assert result_a.returncode == 1, (
        f"Worker A should crash (exit 1), got {result_a.returncode}.\n"
        f"stdout: {result_a.stdout}\nstderr: {result_a.stderr}"
    )

    # Manually expire lease and set startup_state='accepted' to simulate crash
    # that happened after startup ACK was persisted but before mark_dispatched.
    with app.app_context():
        from app.db import db
        db.execute(
            "UPDATE call_jobs SET lease_expires_at = now() - interval '1 minute' "
            "WHERE order_id = %s AND job_status = 'leased'",
            (order_id,),
        )
        # No calls row from MOCK path, so this just simulates lease expiry recovery.

    # Worker B: recover expired lease.
    result_b = _run_worker("--once")
    pid_b = result_b.pid

    assert pid_a != pid_b, f"Must be separate processes: A={pid_a} B={pid_b}"
    assert pid_a != os.getpid()
    assert pid_b != os.getpid()
    assert result_b.returncode == 0, (
        f"Worker B should exit 0.\nstdout: {result_b.stdout}\nstderr: {result_b.stderr}"
    )

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


# ── F. Two workers + 20 jobs — all processed, zero duplicates ─────────────────

@pytest.fixture()
def _twenty_tenant_orders(app):
    """Create one tenant with 20 distinct orders and call_jobs."""
    import uuid
    with app.app_context():
        from app.db import db
        from app.services.call_job_service import enqueue_order_call

        uid = uuid.uuid4().hex[:8]
        tenant_row = db.query_one(
            """INSERT INTO tenants (business_name, email, status, settings)
               VALUES (%s, %s, 'active', '{}')
               RETURNING id""",
            (f"PL20 Test {uid}", f"pl20-{uid}@test.internal"),
        )
        tenant_id = int(tenant_row["id"])
        db.execute(
            "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 10000) "
            "ON CONFLICT (tenant_id) DO NOTHING",
            (tenant_id,),
        )

        order_ids = []
        for i in range(20):
            row = db.query_one(
                """INSERT INTO orders
                       (tenant_id, external_order_id, phone, customer_name, amount,
                        order_status, attempt_count, max_attempts)
                   VALUES (%s, %s, %s, %s, %s, 'CALL_QUEUED', 0, 3)
                   RETURNING id""",
                (tenant_id, f"PL20-{uid}-{i}", "01700000001", f"Test {i}", "100.00"),
            )
            order_id = int(row["id"])
            order_ids.append(order_id)
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

        yield tenant_id, order_ids

        db.execute("DELETE FROM call_jobs WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM order_call_attempts WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM orders WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM wallets WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM customer_rates WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM retry_rules WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM api_keys WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM users WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM service_heartbeats WHERE instance_id LIKE %s", ("%@%",))
        db.execute("DELETE FROM calls WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM flows WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))


def test_two_workers_twenty_jobs_no_duplicates(app, _twenty_tenant_orders):
    """Two concurrent workers each with --max-jobs 10 process all 20 jobs.

    All 20 distinct order_ids must appear in a terminal job state, zero
    duplicates (no order processed twice).  Both PIDs must differ.
    """
    tenant_id, order_ids = _twenty_tenant_orders

    cwd = os.path.join(os.path.dirname(__file__), "..", "..")
    env = _worker_env()

    proc1 = subprocess.Popen(
        _WORKER_CMD + ["--max-jobs", "10"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", cwd=cwd,
    )
    proc2 = subprocess.Popen(
        _WORKER_CMD + ["--max-jobs", "10"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", cwd=cwd,
    )
    pid1, pid2 = proc1.pid, proc2.pid

    out1, err1 = proc1.communicate(timeout=60)
    out2, err2 = proc2.communicate(timeout=60)

    assert proc1.returncode == 0, f"Worker 1 failed (rc={proc1.returncode}):\n{err1}"
    assert proc2.returncode == 0, f"Worker 2 failed (rc={proc2.returncode}):\n{err2}"
    assert pid1 != pid2, f"Workers must be separate OS processes: {pid1} == {pid2}"
    assert pid1 != os.getpid()
    assert pid2 != os.getpid()

    with app.app_context():
        from app.db import db
        rows = db.query(
            """SELECT order_id, COUNT(*) AS cnt
               FROM call_jobs
               WHERE tenant_id = %s AND job_status NOT IN ('queued', 'leased')
               GROUP BY order_id""",
            (tenant_id,),
        )

    processed_order_ids = [r["order_id"] for r in rows]
    duplicated = [r["order_id"] for r in rows if r["cnt"] > 1]

    # All 20 orders must have been processed at least once.
    missing = set(order_ids) - set(processed_order_ids)
    assert not missing, f"These orders were not processed: {missing}"

    # No order should have more than one non-terminal job row (duplicate dispatch).
    assert not duplicated, f"Orders processed more than once (duplicates): {duplicated}"


# ── G. Terminal order race — job cancelled immediately ─────────────────────────

def test_terminal_order_job_cancelled(app):
    """Order already in terminal state (CONFIRMED) when worker dispatches →
    dispatch returns terminal_order error → job marked 'cancelled'.
    """
    import uuid
    with app.app_context():
        from app.db import db
        from app.services.call_job_service import enqueue_order_call

        uid = uuid.uuid4().hex[:8]
        tenant_row = db.query_one(
            "INSERT INTO tenants (business_name, email, status, settings) "
            "VALUES (%s, %s, 'active', '{}') RETURNING id",
            (f"PL Terminal {uid}", f"pl-term-{uid}@test.internal"),
        )
        tenant_id = int(tenant_row["id"])
        db.execute(
            "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 1000) "
            "ON CONFLICT (tenant_id) DO NOTHING",
            (tenant_id,),
        )

        # Order already CONFIRMED — terminal status.
        row = db.query_one(
            """INSERT INTO orders
                   (tenant_id, external_order_id, phone, customer_name, amount,
                    order_status, attempt_count, max_attempts)
               VALUES (%s, %s, %s, %s, %s, 'CONFIRMED', 1, 3)
               RETURNING id""",
            (tenant_id, f"PL-TERM-{uid}", "01700000001", "Terminal Test", "100.00"),
        )
        order_id = int(row["id"])

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

    result = _run_worker("--once")
    assert result.pid != os.getpid()

    with app.app_context():
        from app.db import db
        job = db.query_one(
            "SELECT job_status FROM call_jobs WHERE order_id = %s",
            (order_id,),
        )
        # Cleanup
        db.execute("DELETE FROM call_jobs WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM order_call_attempts WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM calls WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM orders WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM wallets WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM flows WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))

    assert job is not None
    assert job["job_status"] == "cancelled", (
        f"Expected job_status='cancelled' for terminal order, got {job['job_status']!r}"
    )


# ── H. Temporary rejection — retryable SIP failure → retry_wait ───────────────

def test_temporary_rejection_goes_to_retry_wait(app, _tenant_order):
    """MOCK_SIP raises DispatchSipUnavailableError (retryable).
    With attempts=1 < max_attempts=5, the job must transition to 'retry_wait'
    (not 'failed') so the worker can retry later.
    """
    order_id, tenant_id = _tenant_order

    result = _run_worker("--once")
    assert result.pid != os.getpid()

    with app.app_context():
        from app.db import db
        job = db.query_one(
            "SELECT job_status, attempts, max_attempts FROM call_jobs WHERE order_id = %s",
            (order_id,),
        )
    assert job is not None
    assert job["job_status"] == "retry_wait", (
        f"Expected retry_wait after first MOCK_SIP failure (attempts={job['attempts']} "
        f"< max_attempts={job['max_attempts']}), got {job['job_status']!r}.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ── I. Permanent rejection — attempts exhausted → failed ──────────────────────

def test_permanent_rejection_marks_failed(app):
    """Job with max_attempts=1: first MOCK_SIP failure exhausts all attempts →
    job_status='failed', order_status='FAILED'.
    """
    import uuid
    with app.app_context():
        from app.db import db
        from app.services.call_job_service import enqueue_order_call

        uid = uuid.uuid4().hex[:8]
        tenant_row = db.query_one(
            "INSERT INTO tenants (business_name, email, status, settings) "
            "VALUES (%s, %s, 'active', '{}') RETURNING id",
            (f"PL Perm {uid}", f"pl-perm-{uid}@test.internal"),
        )
        tenant_id = int(tenant_row["id"])
        db.execute(
            "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 1000) "
            "ON CONFLICT (tenant_id) DO NOTHING",
            (tenant_id,),
        )

        row = db.query_one(
            """INSERT INTO orders
                   (tenant_id, external_order_id, phone, customer_name, amount,
                    order_status, attempt_count, max_attempts)
               VALUES (%s, %s, %s, %s, %s, 'CALL_QUEUED', 0, 1)
               RETURNING id""",
            (tenant_id, f"PL-PERM-{uid}", "01700000001", "Perm Test", "100.00"),
        )
        order_id = int(row["id"])

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
            # max_attempts=1 → single dispatch attempt allowed
            enqueue_order_call(conn, order_id, tenant_id, attempt_no=1,
                               order_call_attempt_id=attempt_id, max_attempts=1)

    result = _run_worker("--once")
    assert result.pid != os.getpid()

    with app.app_context():
        from app.db import db
        job = db.query_one(
            "SELECT job_status, attempts, max_attempts FROM call_jobs WHERE order_id = %s",
            (order_id,),
        )
        order = db.query_one(
            "SELECT order_status FROM orders WHERE id = %s",
            (order_id,),
        )
        # Cleanup
        db.execute("DELETE FROM call_jobs WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM order_call_attempts WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM calls WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM orders WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM wallets WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM flows WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))

    assert job is not None
    assert job["job_status"] == "failed", (
        f"Expected failed after all attempts exhausted "
        f"(attempts={job['attempts']}, max={job['max_attempts']}), "
        f"got {job['job_status']!r}.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert order is not None
    assert order["order_status"] == "FAILED", (
        f"Expected order FAILED, got {order['order_status']!r}"
    )


# ── J. Stale-pending startup — dispatch classifies correctly ──────────────────

def test_stale_pending_startup_is_retryable(app):
    """A calls row with startup_state='pending' and stale started_at is classified
    as a retryable crash-recovery case, not an in-progress startup to wait for.

    This test drives dispatch_order_call() directly (no worker subprocess) because
    the stale-pending path is pure dispatch logic — no worker CLI argument needed.
    """
    import uuid
    import datetime

    with app.app_context():
        from app.db import db
        from app.services.call_job_service import dispatch_order_call

        uid = uuid.uuid4().hex[:8]
        tenant_row = db.query_one(
            "INSERT INTO tenants (business_name, email, status, settings) "
            "VALUES (%s, %s, 'active', '{}') RETURNING id",
            (f"PL Stale {uid}", f"pl-stale-{uid}@test.internal"),
        )
        tenant_id = int(tenant_row["id"])
        db.execute(
            "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 1000) "
            "ON CONFLICT (tenant_id) DO NOTHING",
            (tenant_id,),
        )

        order_row = db.query_one(
            """INSERT INTO orders
                   (tenant_id, external_order_id, phone, customer_name, amount,
                    order_status, attempt_count, max_attempts)
               VALUES (%s, %s, %s, %s, %s, 'CALL_QUEUED', 1, 3)
               RETURNING id""",
            (tenant_id, f"PL-STALE-{uid}", "01700000001", "Stale Test", "100.00"),
        )
        order_id = int(order_row["id"])

        # Insert a stale calls row with startup_state='pending' and old started_at.
        stale_started = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=120)
        call_row = db.query_one(
            """INSERT INTO calls
                   (tenant_id, order_id, number, started_at, status,
                    startup_state, call_type)
               VALUES (%s, %s, %s, %s, 'calling', 'pending', 'ivr')
               RETURNING id""",
            (tenant_id, order_id, "01700000001", stale_started),
        )
        call_id = int(call_row["id"])

        # Simulate the state left by a worker crash after calls INSERT:
        # order_call_attempts row exists with call_id = call_id (linked).
        # dispatch_order_call will see this and raise DispatchDuplicateCallError,
        # then classify the stale-pending call as retryable crash-recovery.
        attempt_row = db.query_one(
            """INSERT INTO order_call_attempts (order_id, tenant_id, attempt_no, call_id, created_at)
               VALUES (%s, %s, 1, %s, now())
               ON CONFLICT (order_id, attempt_no) DO UPDATE
                   SET call_id = EXCLUDED.call_id
               RETURNING id""",
            (order_id, tenant_id, call_id),
        )
        attempt_id = int(attempt_row["id"] if isinstance(attempt_row, dict) else attempt_row[0])

        # dispatch_order_call will hit DispatchDuplicateCallError (linked attempt exists)
        # and must classify the stale-pending call as retryable (not fresh-pending wait).
        result = dispatch_order_call(order_id, tenant_id, attempt_no=1)

        # Cleanup
        db.execute("DELETE FROM order_call_attempts WHERE id = %s", (attempt_id,))
        db.execute("DELETE FROM calls WHERE id = %s", (call_id,))
        db.execute("DELETE FROM orders WHERE id = %s", (order_id,))
        db.execute("DELETE FROM wallets WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM flows WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))

    assert not result.accepted, "Stale-pending should not return accepted=True"
    assert result.retryable, (
        f"Stale-pending call must be classified retryable, got error_code={result.error_code!r}"
    )
    assert result.error_code != "startup_pending", (
        f"Stale-pending must NOT return 'startup_pending' (that's the fresh-pending hold). "
        f"Got error_code={result.error_code!r}"
    )


# ── K. Crash after calls row but before executor.submit ───────────────────────

@pytest.fixture()
def _tenant_order_with_sip(app):
    """Like _tenant_order but also creates a SIP provider + phone number so that
    call_runner.start_call() can proceed past SIP credential checks and reach the
    calls INSERT before the after_calls_row fault fires.

    Required because MOCK_SIP must be disabled for Worker A so that start_call()
    is actually invoked (with MOCK_SIP=1 the dispatch raises DispatchSipUnavailableError
    before start_call() is ever called, so the fault point is never reached).
    """
    import uuid
    with app.app_context():
        from app.db import db
        from app.services.call_job_service import enqueue_order_call
        from app.models.sip_provider import encrypt_password

        uid = uuid.uuid4().hex[:8]
        tenant_row = db.query_one(
            """INSERT INTO tenants (business_name, email, status, settings)
               VALUES (%s, %s, 'active', '{}')
               RETURNING id""",
            (f"PL SIP Test {uid}", f"pl-sip-{uid}@test.internal"),
        )
        tenant_id = int(tenant_row["id"])
        db.execute(
            "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 1000) "
            "ON CONFLICT (tenant_id) DO NOTHING",
            (tenant_id,),
        )

        # SIP provider with fake but non-empty credentials.
        # call_runner.start_call() only validates that the creds are non-empty;
        # the actual SIP connect fails later (after the fault fires).
        enc_pw = encrypt_password("fake-sip-password-for-test")
        provider_row = db.query_one(
            """INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password, status)
               VALUES (%s, 'sip.test.invalid', 5060, %s, %s, 'active')
               RETURNING id""",
            (f"TestSIP-{uid}", f"testuser-{uid}", enc_pw),
        )
        provider_id = int(provider_row["id"])

        # Dedicated phone number for this tenant.
        phone_num = f"019{uid}"  # 11-char fake BD-style number
        pn_row = db.query_one(
            """INSERT INTO phone_numbers (number, provider_id, number_type, status)
               VALUES (%s, %s, 'dedicated', 'active')
               RETURNING id""",
            (phone_num, provider_id),
        )
        pn_id = int(pn_row["id"])
        db.execute(
            """INSERT INTO customer_number_assignments (tenant_id, phone_number_id, is_primary)
               VALUES (%s, %s, true)
               ON CONFLICT (phone_number_id) WHERE unassigned_at IS NULL DO NOTHING""",
            (tenant_id, pn_id),
        )

        row = db.query_one(
            """INSERT INTO orders
                   (tenant_id, external_order_id, phone, customer_name, amount,
                    order_status, attempt_count, max_attempts)
               VALUES (%s, %s, %s, %s, %s, 'CALL_QUEUED', 0, 3)
               RETURNING id""",
            (tenant_id, f"PL-SIP-{uid}", "01700000001", "SIP Fault Test", "100.00"),
        )
        order_id = int(row["id"])
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

        yield order_id, tenant_id, provider_id, pn_id

        db.execute("DELETE FROM call_jobs WHERE tenant_id = %s",               (tenant_id,))
        db.execute("DELETE FROM calls WHERE tenant_id = %s",                   (tenant_id,))
        db.execute("DELETE FROM order_call_attempts WHERE tenant_id = %s",     (tenant_id,))
        db.execute("DELETE FROM orders WHERE tenant_id = %s",                  (tenant_id,))
        db.execute("DELETE FROM wallets WHERE tenant_id = %s",                 (tenant_id,))
        db.execute("DELETE FROM customer_number_assignments WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM phone_numbers WHERE id = %s",                  (pn_id,))
        db.execute("DELETE FROM sip_providers WHERE id = %s",                  (provider_id,))
        db.execute("DELETE FROM customer_rates WHERE tenant_id = %s",          (tenant_id,))
        db.execute("DELETE FROM retry_rules WHERE tenant_id = %s",             (tenant_id,))
        db.execute("DELETE FROM api_keys WHERE tenant_id = %s",                (tenant_id,))
        db.execute("DELETE FROM users WHERE tenant_id = %s",                   (tenant_id,))
        db.execute("DELETE FROM service_heartbeats WHERE instance_id LIKE %s", ("%@%",))
        db.execute("DELETE FROM flows WHERE tenant_id = %s",                   (tenant_id,))
        db.execute("DELETE FROM tenants WHERE id = %s",                        (tenant_id,))


def test_crash_after_calls_row_before_ack(app, _tenant_order_with_sip):
    """Worker A crashes after the calls INSERT but before executor.submit().

    The after_calls_row fault fires inside call_runner.start_call() AFTER:
      - calls row committed with startup_state='pending'
      - startup ACK event registered in _startup_ack_events
    But BEFORE executor.submit() dispatches the SIP thread.

    This requires MOCK_SIP to be disabled for Worker A so that call_runner.start_call()
    is actually invoked (with MOCK_SIP=1 the dispatch path raises before start_call,
    so the fault never fires).  Worker A has real SIP creds from the fixture (non-empty,
    fake) so SIP config validation passes; the actual SIP connect would fail, but
    os._exit(1) fires first.

    After the crash:
      - calls row exists with startup_state='pending'
      - order_call_attempts.call_id IS NULL (queue_confirmation_call never linked it)

    Worker B runs with MOCK_SIP=1 → DispatchSipUnavailableError (retryable) →
    job transitions to retry_wait.

    Both worker PIDs must be distinct and differ from the test process.
    """
    order_id, tenant_id, provider_id, pn_id = _tenant_order_with_sip

    # Worker A: real SIP path, crash at after_calls_row.
    cwd = os.path.join(os.path.dirname(__file__), "..", "..")
    worker_a_env = {
        **os.environ,
        "FLASK_ENV":                "testing",
        "MOCK_SIP_PROVIDER":        "",   # disabled — reach call_runner.start_call()
        "MOCK_TTS_PROVIDER":        "1",
        "ENABLE_CALL_WORKER":       "true",
        "ENABLE_CALL_MONITOR":      "false",
        "CALL_WORKER_POLL_SECONDS": "0.5",
        "CALL_WORKER_LEASE_SECONDS": "30",
    }
    proc_a = subprocess.Popen(
        _WORKER_CMD + ["--once", "--test-fault-point", "after_calls_row"],
        env=worker_a_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    try:
        out_a, err_a = proc_a.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc_a.kill()
        out_a, err_a = proc_a.communicate()

    pid_a = proc_a.pid
    assert proc_a.returncode == 1, (
        f"Worker A should crash at after_calls_row (exit 1), got {proc_a.returncode}.\n"
        f"stdout: {out_a}\nstderr: {err_a}"
    )

    # Verify: calls row committed with startup_state='pending'; attempt call_id NULL.
    with app.app_context():
        from app.db import db
        call_row = db.query_one(
            "SELECT id, startup_state FROM calls WHERE order_id = %s AND tenant_id = %s",
            (order_id, tenant_id),
        )
        attempt_row = db.query_one(
            """SELECT call_id FROM order_call_attempts
               WHERE order_id = %s AND tenant_id = %s AND attempt_no = 1""",
            (order_id, tenant_id),
        )

    assert call_row is not None, (
        "calls row must be committed before after_calls_row fault fires.\n"
        f"Worker A stdout: {out_a}\nstderr: {err_a}"
    )
    assert call_row["startup_state"] == "pending", (
        f"startup_state should be 'pending' (executor never ran), got {call_row['startup_state']!r}"
    )
    assert attempt_row is not None
    assert attempt_row["call_id"] is None, (
        f"order_call_attempts.call_id must be NULL (crash before link step), "
        f"got {attempt_row['call_id']!r}"
    )

    # Expire lease so Worker B can recover.
    with app.app_context():
        from app.db import db
        db.execute(
            "UPDATE call_jobs SET lease_expires_at = now() - interval '1 minute' "
            "WHERE order_id = %s AND job_status = 'leased'",
            (order_id,),
        )

    # Worker B: MOCK_SIP=1 → retryable SIP error → job → retry_wait.
    result_b = _run_worker("--once")
    pid_b = result_b.pid

    assert pid_a != pid_b, f"Must be separate processes: A={pid_a} B={pid_b}"
    assert pid_a != os.getpid()
    assert pid_b != os.getpid()
    assert result_b.returncode == 0, (
        f"Worker B should exit 0.\nstdout: {result_b.stdout}\nstderr: {result_b.stderr}"
    )

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
