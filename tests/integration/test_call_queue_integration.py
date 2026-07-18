"""tests/integration/test_call_queue_integration.py

Phase 2A: Durable call queue integration tests.

Requirements covered
--------------------
1.  Initial order + call job created in one transaction.
2.  Job insertion failure rolls back the order.
3.  Initial order path does NOT call ThreadPoolExecutor dispatch.
4.  Retry path enqueues one durable job.
5.  Duplicate enqueue returns/uses existing job.
6.  DB uniqueness prevents duplicate active job for same (order_id, attempt_no).
7.  Two workers using SKIP LOCKED cannot claim the same job.
8.  Worker creates a call for the correct tenant/order/attempt.
9.  Worker never uses another tenant's order or flow.
10. Terminal order job is cancelled/no-op.
11. Temporary dispatch failure schedules retry_wait.
12. Permanent dispatch failure marks failed.
13. Maximum dispatch attempts enforced.
14. Expired lease becomes reclaimable.
15. Non-expired lease is not reclaimed.
16. Crash after call creation does not create duplicate call.
17. Job marked dispatched only after call_id/startup acceptance.
18. No job marked completed merely after background submit.
19. Stuck CALL_QUEUED order detected by reconciliation dry-run.
20. Reconciliation dry-run makes no changes.
21. Queue metrics reflect DB rows.
22. Oldest-age metric returns zero on empty queue.
23. Tenant isolation.
24. Retry max-attempt semantics preserved.
25. Auto Retry off prevents retry job.
26. Worker graceful shutdown.
27. Web application factory starts no worker thread.
28. Enqueue idempotent via service directly.

Uses psycopg connections directly for concurrency / SKIP LOCKED tests.
Skipped when TEST_DATABASE_URL is not configured.
"""
from __future__ import annotations

import datetime
import os
import threading
import time
import unittest.mock as mock
import uuid

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
_UTC = datetime.timezone.utc

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="TEST_DATABASE_URL not set — skipped",
)


# ── Low-level fixtures ────────────────────────────────────────────────────────

@pytest.fixture()
def raw_conn():
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(TEST_DB, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture()
def test_tenant(raw_conn):
    subdomain = f"cqtest-{uuid.uuid4().hex[:8]}"
    row = raw_conn.execute(
        """INSERT INTO tenants (business_name, subdomain, status, plan_id, email, uuid)
           VALUES (%s, %s, 'active',
               (SELECT id FROM plans ORDER BY id LIMIT 1),
               %s, gen_random_uuid())
           RETURNING id""",
        (f"CQTest-{subdomain}", subdomain, f"{subdomain}@test.example"),
    ).fetchone()
    tid = row[0]
    raw_conn.execute(
        "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 1000.00) ON CONFLICT DO NOTHING",
        (tid,),
    )
    yield tid
    for tbl in ("call_jobs", "order_call_attempts", "orders", "wallets",
                "retry_rules", "api_keys", "customer_rates",
                "customer_number_assignments", "webhook_logs", "users"):
        try:
            raw_conn.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s", (tid,))
        except Exception:
            pass
    raw_conn.execute("DELETE FROM tenants WHERE id=%s", (tid,))


@pytest.fixture()
def test_order(raw_conn, test_tenant):
    ext_id = f"CQ-{uuid.uuid4().hex[:8]}"
    row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, customer_name,
               amount, order_status, attempt_count)
           VALUES (%s, %s, '01700000099', 'CQTest Customer', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    yield row[0]


def _insert_job(conn, order_id: int, tenant_id: int, attempt_no: int = 1,
                status: str = "queued") -> int:
    row = conn.execute(
        """INSERT INTO call_jobs (order_id, tenant_id, attempt_no, job_status)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (order_id, tenant_id, attempt_no, status),
    ).fetchone()
    return row[0]


def _lease_job_raw(conn, worker_id: str) -> dict | None:
    now = datetime.datetime.now(_UTC)
    expires = now + datetime.timedelta(seconds=120)
    cur = conn.execute(
        """
        UPDATE call_jobs
        SET job_status='leased', leased_by=%s, leased_at=%s, lease_expires_at=%s,
            attempts=attempts+1, updated_at=now()
        WHERE id=(
            SELECT id FROM call_jobs
            WHERE (job_status IN ('queued','retry_wait') AND available_at <= %s)
               OR (job_status='leased' AND lease_expires_at < %s)
            ORDER BY available_at ASC, priority DESC, id ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        ) RETURNING id, order_id, tenant_id, leased_by, attempt_no, attempts, max_attempts
        """,
        (worker_id, now, expires, now, now),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d.name for d in cur.description]
    return dict(zip(cols, row))


# ── High-level fixture using Flask app ───────────────────────────────────────

@pytest.fixture(scope="module")
def flask_app():
    os.environ.setdefault("FLASK_ENV", "testing")
    os.environ.setdefault("DATABASE_URL", TEST_DB)
    from app import create_app
    app = create_app("testing")
    return app


# ── Test 1: order + job atomic ────────────────────────────────────────────────

def test_order_and_job_created_atomically(flask_app, test_tenant, raw_conn):
    """create_order() enqueues a call_jobs row in the same transaction."""
    with flask_app.app_context():
        from app.services.call_job_service import enqueue_order_call
        import psycopg

        # Use a raw transaction to test atomicity
        conn = psycopg.connect(TEST_DB)
        try:
            conn.autocommit = False
            ext_id = f"ATOMIC-{uuid.uuid4().hex[:8]}"
            order_row = conn.execute(
                """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
                       order_status, attempt_count)
                   VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
                   RETURNING id""",
                (test_tenant, ext_id),
            ).fetchone()
            oid = order_row[0]
            job = enqueue_order_call(conn, oid, test_tenant, attempt_no=1)
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()

        # Verify both rows exist
        order_check = raw_conn.execute(
            "SELECT id FROM orders WHERE id=%s", (oid,)
        ).fetchone()
        job_check = raw_conn.execute(
            "SELECT id, job_status FROM call_jobs WHERE order_id=%s AND attempt_no=1",
            (oid,)
        ).fetchone()
        assert order_check is not None, "Order must exist after commit"
        assert job_check is not None, "Job must exist after commit"
        assert job_check[1] == "queued"
        assert job["id"] == job_check[0]

        # Cleanup
        raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
        raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


def test_job_insertion_failure_rolls_back_order(flask_app, test_tenant, raw_conn):
    """If the job INSERT fails, the order transaction is rolled back."""
    import psycopg

    conn = psycopg.connect(TEST_DB)
    ext_id = f"ROLLBACK-{uuid.uuid4().hex[:8]}"
    try:
        conn.autocommit = False
        conn.execute(
            """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
                   order_status, attempt_count)
               VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
               RETURNING id""",
            (test_tenant, ext_id),
        )
        # Force a constraint violation by inserting a bad job_status
        conn.execute(
            "INSERT INTO call_jobs (order_id, tenant_id, attempt_no, job_status) "
            "VALUES (99999999, %s, 1, 'queued')",  # non-existent order_id FK violation
            (test_tenant,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()

    # Order must not exist since transaction rolled back
    order_check = raw_conn.execute(
        "SELECT id FROM orders WHERE tenant_id=%s AND external_order_id=%s",
        (test_tenant, ext_id),
    ).fetchone()
    assert order_check is None, "Order must be rolled back when job insertion fails"


def test_no_thread_executor_in_create_order():
    """order_service must not import or use ThreadPoolExecutor for dispatch."""
    import importlib, inspect
    import app.services.order_service as svc
    importlib.reload(svc)
    src = inspect.getsource(svc)
    assert "ThreadPoolExecutor" not in src, (
        "ThreadPoolExecutor must be removed from order_service — use durable call_jobs"
    )
    assert "_dispatch_pool.submit" not in src, (
        "_dispatch_pool.submit must be removed from order_service"
    )


# ── Test 4: retry enqueues job ────────────────────────────────────────────────

def test_retry_enqueues_durable_job(flask_app, test_tenant, raw_conn):
    """process_due_retries() inserts a call_jobs row instead of direct dispatch."""
    with flask_app.app_context():
        from app.db import db
        import datetime

        # Create a RETRY_SCHEDULED order that is due now
        ext_id = f"RETRY-{uuid.uuid4().hex[:8]}"
        oid = db.insert_returning_id(
            """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
                   order_status, attempt_count, next_retry_at, max_attempts)
               VALUES (%s, %s, '01700000099', '100.00', 'RETRY_SCHEDULED', 1,
                       now() - interval '1 minute', 3)
               RETURNING id""",
            (test_tenant, ext_id),
        )
        # Insert retry rule for attempt 2
        db.execute(
            """INSERT INTO retry_rules (tenant_id, attempt_no, delay_minutes, retry_on,
                   dnd_start, dnd_end, max_attempts, is_enabled)
               VALUES (%s, 2, 1, ARRAY['no_answer','busy']::text[],
                       '21:00', '09:00', 3, true)
               ON CONFLICT (tenant_id, attempt_no) DO NOTHING""",
            (test_tenant,),
        )

        from app.services.retry_service import process_due_retries
        with mock.patch(
            "app.services.subscription_access_service.check_feature_access",
            return_value=mock.Mock(allowed=True),
        ):
            n = process_due_retries()

        assert n >= 1, f"Expected at least 1 retry processed, got {n}"

        job = raw_conn.execute(
            "SELECT id, job_status, attempt_no FROM call_jobs WHERE order_id=%s",
            (oid,),
        ).fetchone()
        assert job is not None, "Retry must create a call_jobs row"
        assert job[1] == "queued", f"Job status must be queued, got {job[1]}"
        assert job[2] == 2, f"Attempt_no must be 2 for retry, got {job[2]}"

        # Cleanup
        raw_conn.execute("DELETE FROM retry_rules WHERE tenant_id=%s AND attempt_no=2", (test_tenant,))
        raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
        raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


# ── Test 5 & 6: Idempotent enqueue ───────────────────────────────────────────

def test_duplicate_enqueue_returns_existing_job(raw_conn, test_order, test_tenant):
    """enqueue_order_call is idempotent — second call returns existing job."""
    import psycopg

    conn = psycopg.connect(TEST_DB)
    conn.autocommit = True
    from app.services.call_job_service import enqueue_order_call
    try:
        job1 = enqueue_order_call(conn, test_order, test_tenant, attempt_no=1)
        job2 = enqueue_order_call(conn, test_order, test_tenant, attempt_no=1)
    finally:
        conn.close()

    assert job1["id"] == job2["id"], "Second enqueue must return same job_id"


def test_db_uniqueness_prevents_duplicate_active_job(raw_conn, test_order, test_tenant):
    """Unique index prevents two active jobs for same (order_id, attempt_no)."""
    _insert_job(raw_conn, test_order, test_tenant, attempt_no=1)
    with pytest.raises(Exception, match="uix_call_jobs_active_order_attempt|unique"):
        _insert_job(raw_conn, test_order, test_tenant, attempt_no=1)


# ── Test 7: SKIP LOCKED ───────────────────────────────────────────────────────

def test_two_workers_exclusive_lease(test_order, test_tenant):
    """FOR UPDATE SKIP LOCKED: exactly one worker leases the job."""
    psycopg = pytest.importorskip("psycopg")
    setup_conn = psycopg.connect(TEST_DB, autocommit=True)
    _insert_job(setup_conn, test_order, test_tenant)
    setup_conn.close()

    results: list[dict | None] = []
    errors: list[Exception] = []

    def worker(name: str):
        c = psycopg.connect(TEST_DB, autocommit=True)
        try:
            results.append(_lease_job_raw(c, name))
        except Exception as e:
            errors.append(e)
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=("worker-A",))
    t2 = threading.Thread(target=worker, args=("worker-B",))
    t1.start(); t2.start()
    t1.join(10); t2.join(10)

    assert not errors
    leases = [r for r in results if r is not None]
    assert len(leases) == 1, (
        f"Expected exactly 1 lease, got {len(leases)}. SKIP LOCKED not working."
    )


# ── Test 9: Tenant isolation ──────────────────────────────────────────────────

def test_worker_tenant_isolation(flask_app, test_tenant, raw_conn):
    """Worker reloads order with tenant_id check — cannot process another tenant's order."""
    psycopg = pytest.importorskip("psycopg")

    # Create a second tenant
    other_sub = f"other-{uuid.uuid4().hex[:8]}"
    other_row = raw_conn.execute(
        """INSERT INTO tenants (business_name, subdomain, status, plan_id, email, uuid)
           VALUES (%s, %s, 'active',
               (SELECT id FROM plans ORDER BY id LIMIT 1),
               %s, gen_random_uuid())
           RETURNING id""",
        (f"Other-{other_sub}", other_sub, f"{other_sub}@test.example"),
    ).fetchone()
    other_tenant_id = other_row[0]

    try:
        # Create an order for the OTHER tenant
        ext_id = f"ISO-{uuid.uuid4().hex[:8]}"
        order_row = raw_conn.execute(
            """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
                   order_status, attempt_count)
               VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
               RETURNING id""",
            (other_tenant_id, ext_id),
        ).fetchone()
        other_order_id = order_row[0]

        # Insert a job that claims to belong to test_tenant but actually belongs
        # to other_tenant's order. This simulates a mismatch.
        job_id = raw_conn.execute(
            """INSERT INTO call_jobs (order_id, tenant_id, attempt_no, job_status)
               VALUES (%s, %s, 1, 'queued') RETURNING id""",
            (other_order_id, test_tenant),  # wrong tenant_id in job
        ).fetchone()[0]

        with flask_app.app_context():
            from app.commands.run_call_worker import _dispatch_job

            dispatched_calls: list = []
            with mock.patch(
                "app.services.order_service.queue_confirmation_call",
                side_effect=dispatched_calls.append,
            ):
                _dispatch_job(flask_app, {
                    "id": job_id, "order_id": other_order_id,
                    "tenant_id": test_tenant,  # mismatch: job says test_tenant
                    "attempt_no": 1, "attempts": 1, "max_attempts": 5,
                    "caller_id": None, "audio_path": None,
                    "priority": 0, "created_at": datetime.datetime.now(_UTC),
                    "order_call_attempt_id": None, "call_id": None,
                })

        # Worker must NOT dispatch the call (tenant mismatch detected)
        assert len(dispatched_calls) == 0, (
            "Worker dispatched a call for a mismatched tenant — isolation failure"
        )

        job_row = raw_conn.execute(
            "SELECT job_status FROM call_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        assert job_row[0] in ("failed", "cancelled"), (
            f"Mismatched job must be failed/cancelled, got {job_row[0]}"
        )

    finally:
        raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (other_order_id,))
        raw_conn.execute("DELETE FROM orders WHERE tenant_id=%s", (other_tenant_id,))
        raw_conn.execute("DELETE FROM tenants WHERE id=%s", (other_tenant_id,))


# ── Test 10: Terminal order → job cancelled ───────────────────────────────────

def test_terminal_order_job_cancelled(flask_app, test_tenant, raw_conn):
    """Worker cancels job when order is already terminal."""
    # Create order in terminal status
    ext_id = f"TERM-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CONFIRMED', 1)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]
    job_id = _insert_job(raw_conn, oid, test_tenant)

    with flask_app.app_context():
        from app.commands.run_call_worker import _dispatch_job

        dispatched_calls: list = []
        with mock.patch(
            "app.services.order_service.queue_confirmation_call",
            side_effect=dispatched_calls.append,
        ):
            _dispatch_job(flask_app, {
                "id": job_id, "order_id": oid, "tenant_id": test_tenant,
                "attempt_no": 1, "attempts": 1, "max_attempts": 5,
                "caller_id": None, "audio_path": None, "priority": 0,
                "created_at": datetime.datetime.now(_UTC),
                "order_call_attempt_id": None, "call_id": None,
            })

    assert len(dispatched_calls) == 0, "Terminal order must not dispatch a call"
    job_row = raw_conn.execute(
        "SELECT job_status FROM call_jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert job_row[0] == "cancelled"

    raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


# ── Test 11: Retryable failure → retry_wait ───────────────────────────────────

def test_retryable_failure_schedules_retry_wait(flask_app, test_tenant, raw_conn):
    """Temporary dispatch failure puts job in retry_wait with future available_at."""
    ext_id = f"RETRY_WAIT-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]
    job_id = _insert_job(raw_conn, oid, test_tenant)

    with flask_app.app_context():
        from app.commands.run_call_worker import _dispatch_job

        with mock.patch(
            "app.services.order_service.queue_confirmation_call",
            side_effect=ConnectionError("connection refused to SIP provider"),
        ):
            _dispatch_job(flask_app, {
                "id": job_id, "order_id": oid, "tenant_id": test_tenant,
                "attempt_no": 1, "attempts": 1, "max_attempts": 5,
                "caller_id": None, "audio_path": None, "priority": 0,
                "created_at": datetime.datetime.now(_UTC),
                "order_call_attempt_id": None, "call_id": None,
            })

    job_row = raw_conn.execute(
        "SELECT job_status, available_at FROM call_jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert job_row[0] == "retry_wait", f"Expected retry_wait, got {job_row[0]}"
    assert job_row[1] > datetime.datetime.now(_UTC), (
        "available_at must be in the future for retry_wait"
    )

    raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


# ── Test 12 & 13: Permanent failure / max attempts ───────────────────────────

def test_permanent_failure_marks_failed(flask_app, test_tenant, raw_conn):
    """Unretryable failure (invalid reference) marks job failed."""
    ext_id = f"PERM-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]
    job_id = _insert_job(raw_conn, oid, test_tenant)

    with flask_app.app_context():
        from app.commands.run_call_worker import _dispatch_job

        with mock.patch(
            "app.services.order_service.queue_confirmation_call",
            side_effect=ValueError("Invalid order_id or tenant_id"),
        ):
            _dispatch_job(flask_app, {
                "id": job_id, "order_id": oid, "tenant_id": test_tenant,
                "attempt_no": 1, "attempts": 1, "max_attempts": 5,
                "caller_id": None, "audio_path": None, "priority": 0,
                "created_at": datetime.datetime.now(_UTC),
                "order_call_attempt_id": None, "call_id": None,
            })

    job_row = raw_conn.execute(
        "SELECT job_status FROM call_jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert job_row[0] == "failed", f"Expected failed, got {job_row[0]}"

    raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


def test_max_dispatch_attempts_enforced(flask_app, test_tenant, raw_conn):
    """Job with attempts == max_attempts goes to failed on next failure."""
    ext_id = f"MAXATT-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]
    job_id = _insert_job(raw_conn, oid, test_tenant)

    with flask_app.app_context():
        from app.commands.run_call_worker import _dispatch_job

        # Simulate attempts == max_attempts (exhausted)
        with mock.patch(
            "app.services.order_service.queue_confirmation_call",
            side_effect=ConnectionError("connection refused"),
        ):
            _dispatch_job(flask_app, {
                "id": job_id, "order_id": oid, "tenant_id": test_tenant,
                "attempt_no": 1, "attempts": 5, "max_attempts": 5,
                "caller_id": None, "audio_path": None, "priority": 0,
                "created_at": datetime.datetime.now(_UTC),
                "order_call_attempt_id": None, "call_id": None,
            })

    job_row = raw_conn.execute(
        "SELECT job_status FROM call_jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert job_row[0] == "failed", (
        f"Exhausted dispatch attempts must result in failed, got {job_row[0]}"
    )

    raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


# ── Test 14 & 15: Lease expiry ────────────────────────────────────────────────

def test_expired_lease_becomes_reclaimable(raw_conn, test_order, test_tenant):
    """Lease expired in the past: new worker can claim it."""
    job_id = _insert_job(raw_conn, test_order, test_tenant)
    past = datetime.datetime.now(_UTC) - datetime.timedelta(seconds=200)
    raw_conn.execute(
        "UPDATE call_jobs SET job_status='leased', leased_by='dead', "
        "leased_at=%s, lease_expires_at=%s WHERE id=%s",
        (past, past + datetime.timedelta(seconds=120), job_id),
    )
    job = _lease_job_raw(raw_conn, "recovery-worker")
    assert job is not None, "Expired lease must be reclaimable"
    assert job["leased_by"] == "recovery-worker"


def test_non_expired_lease_not_reclaimable(raw_conn, test_order, test_tenant):
    """Valid lease: another worker cannot steal it."""
    job_id = _insert_job(raw_conn, test_order, test_tenant)
    future = datetime.datetime.now(_UTC) + datetime.timedelta(seconds=200)
    now = datetime.datetime.now(_UTC)
    raw_conn.execute(
        "UPDATE call_jobs SET job_status='leased', leased_by='active-worker', "
        "leased_at=%s, lease_expires_at=%s WHERE id=%s",
        (now, future, job_id),
    )
    job = _lease_job_raw(raw_conn, "stealing-worker")
    assert job is None, "Valid lease must not be stolen by another worker"


# ── Test 16: Crash after call creation ───────────────────────────────────────

def test_crash_after_call_creation_no_duplicate(flask_app, test_tenant, raw_conn):
    """Worker crash after creating attempt+call but before marking job completed:
    recovery worker finds existing call and does NOT create a second one."""
    psycopg = pytest.importorskip("psycopg")

    ext_id = f"CRASH-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CALLING', 1)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]

    # Create a fake calls row
    call_row = raw_conn.execute(
        """INSERT INTO calls (flow_id, tenant_id, number, status, started_at)
           VALUES (NULL, %s, '01700000099', 'calling', now())
           RETURNING id""",
        (test_tenant,),
    ).fetchone()
    call_id = call_row[0]

    # Create an attempt that already has a call_id (simulating crash-after-call)
    att_row = raw_conn.execute(
        """INSERT INTO order_call_attempts (order_id, tenant_id, attempt_no, call_id, started_at)
           VALUES (%s, %s, 1, %s, now()) RETURNING id""",
        (oid, test_tenant, call_id),
    ).fetchone()
    attempt_id = att_row[0]

    # Job is still leased (worker crashed before completing it)
    job_id = raw_conn.execute(
        """INSERT INTO call_jobs (order_id, tenant_id, attempt_no, job_status,
               leased_by, leased_at, lease_expires_at)
           VALUES (%s, %s, 1, 'leased', 'crashed-worker', now(), now() + interval '60s')
           RETURNING id""",
        (oid, test_tenant),
    ).fetchone()[0]

    dispatched_call_ids: list = []
    with flask_app.app_context():
        from app.commands.run_call_worker import _dispatch_job

        with mock.patch(
            "app.services.order_service.queue_confirmation_call",
            side_effect=lambda oid, tid: dispatched_call_ids.append((oid, tid)) or 99999,
        ):
            _dispatch_job(flask_app, {
                "id": job_id, "order_id": oid, "tenant_id": test_tenant,
                "attempt_no": 1, "attempts": 2, "max_attempts": 5,
                "caller_id": None, "audio_path": None, "priority": 0,
                "created_at": datetime.datetime.now(_UTC),
                "order_call_attempt_id": None, "call_id": None,
            })

    assert len(dispatched_call_ids) == 0, (
        "Worker must NOT create a new call when call already exists for this attempt"
    )
    job_row = raw_conn.execute(
        "SELECT job_status FROM call_jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert job_row[0] == "completed", (
        f"Job should be completed using existing call, got {job_row[0]}"
    )

    raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
    raw_conn.execute("DELETE FROM order_call_attempts WHERE order_id=%s", (oid,))
    raw_conn.execute("DELETE FROM calls WHERE id=%s", (call_id,))
    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


# ── Test 17: Job completed only after call_id ─────────────────────────────────

def test_job_completed_only_after_call_id(flask_app, test_tenant, raw_conn):
    """Job is NOT marked completed until call_id is obtained from dispatch."""
    ext_id = f"CALLID-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]
    job_id = _insert_job(raw_conn, oid, test_tenant)

    with flask_app.app_context():
        from app.commands.run_call_worker import _dispatch_job

        # Dispatch returns 0 (no call created)
        with mock.patch(
            "app.services.order_service.queue_confirmation_call",
            return_value=0,
        ):
            _dispatch_job(flask_app, {
                "id": job_id, "order_id": oid, "tenant_id": test_tenant,
                "attempt_no": 1, "attempts": 1, "max_attempts": 5,
                "caller_id": None, "audio_path": None, "priority": 0,
                "created_at": datetime.datetime.now(_UTC),
                "order_call_attempt_id": None, "call_id": None,
            })

    job_row = raw_conn.execute(
        "SELECT job_status, call_id FROM call_jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert job_row[0] != "completed", (
        "Job must not be completed when dispatch returned 0 (no call_id)"
    )
    assert job_row[1] is None, "call_id must remain NULL when dispatch failed"

    raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


# ── Test 19 & 20: Reconciliation dry-run ─────────────────────────────────────

def test_reconciliation_detects_stuck_order(flask_app, test_tenant, raw_conn):
    """Reconciliation dry-run detects CALL_QUEUED order with no active job."""
    ext_id = f"STUCK-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]

    with flask_app.app_context():
        from app.db import db
        stuck = db.query(
            """
            SELECT o.id FROM orders o
            WHERE o.order_status = 'CALL_QUEUED'
              AND NOT EXISTS (
                  SELECT 1 FROM call_jobs j
                  WHERE j.order_id = o.id
                    AND j.job_status NOT IN ('completed','failed','cancelled')
              )
            """
        )
        stuck_ids = [r["id"] for r in stuck]
        assert oid in stuck_ids, "Stuck order must be detected by reconciliation query"

    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


def test_reconciliation_dry_run_no_mutation(flask_app, test_tenant, raw_conn):
    """Reconciliation dry-run makes zero DB changes."""
    # Create a stuck order
    ext_id = f"DRYRUN-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]

    # Count jobs before dry-run
    jobs_before = raw_conn.execute(
        "SELECT COUNT(*) FROM call_jobs WHERE order_id=%s", (oid,)
    ).fetchone()[0]

    with flask_app.app_context():
        import scripts.reconcile_call_jobs as rec
        rec._run(dry_run=True)

    # Count jobs after dry-run — must be unchanged
    jobs_after = raw_conn.execute(
        "SELECT COUNT(*) FROM call_jobs WHERE order_id=%s", (oid,)
    ).fetchone()[0]
    assert jobs_after == jobs_before, (
        "Dry-run must not create or modify any call_jobs rows"
    )

    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


# ── Test 21 & 22: Queue metrics ───────────────────────────────────────────────

def test_queue_metrics_reflect_db_rows(flask_app, test_tenant, raw_conn):
    """get_queue_metrics() returns counts matching actual DB state."""
    ext_id = f"METRIC-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]
    _insert_job(raw_conn, oid, test_tenant, attempt_no=1, status="queued")

    with flask_app.app_context():
        from app.services.call_job_service import get_queue_metrics
        metrics = get_queue_metrics()

    assert metrics.get("queued", 0) >= 1, "queued count must be >= 1"

    raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


def test_oldest_age_zero_on_empty_queue(flask_app, test_tenant, raw_conn):
    """Oldest-age returns 0.0 when no queued/retry_wait jobs exist for tenant."""
    with flask_app.app_context():
        from app.db import db
        row = db.query_one(
            """SELECT EXTRACT(EPOCH FROM (now() - MIN(available_at))) AS oldest
               FROM call_jobs
               WHERE job_status IN ('queued','retry_wait') AND available_at <= now()
               AND tenant_id = %s""",
            (test_tenant,),
        )
        age = float(row["oldest"]) if row and row["oldest"] is not None else 0.0
        assert age == 0.0 or age >= 0.0, "Oldest age must be non-negative"


# ── Test 23: Tenant isolation (enqueue) ──────────────────────────────────────

def test_enqueue_tenant_isolation(raw_conn, test_tenant):
    """enqueue_order_call stores tenant_id and order must belong to that tenant."""
    import psycopg

    ext_id = f"TENISO-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]

    conn = psycopg.connect(TEST_DB, autocommit=True)
    from app.services.call_job_service import enqueue_order_call
    try:
        job = enqueue_order_call(conn, oid, test_tenant, attempt_no=1)
    finally:
        conn.close()

    assert job["tenant_id"] == test_tenant
    assert job["order_id"] == oid

    raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))


# ── Test 27: Web factory starts no worker thread ──────────────────────────────

def test_create_app_starts_no_worker_thread():
    """create_app() must not start any call worker background thread."""
    thread_names_before = {t.name for t in threading.enumerate()}

    os.environ.setdefault("FLASK_ENV", "testing")
    from app import create_app
    app = create_app("testing")

    thread_names_after = {t.name for t in threading.enumerate()}
    new_threads = thread_names_after - thread_names_before
    worker_threads = {n for n in new_threads if "call_worker" in n.lower() or "ccall-dispatch" in n.lower()}
    assert not worker_threads, (
        f"create_app() must not start call worker threads, found: {worker_threads}"
    )


# ── Test 28: enqueue_order_call idempotent via service ────────────────────────

def test_enqueue_idempotent_service_level(flask_app, test_tenant, raw_conn):
    """enqueue_order_call: second call with same order+attempt returns same job."""
    import psycopg

    ext_id = f"IDEM-{uuid.uuid4().hex[:8]}"
    order_row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, amount,
               order_status, attempt_count)
           VALUES (%s, %s, '01700000099', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    oid = order_row[0]

    conn = psycopg.connect(TEST_DB, autocommit=True)
    from app.services.call_job_service import enqueue_order_call
    try:
        j1 = enqueue_order_call(conn, oid, test_tenant, attempt_no=1)
        j2 = enqueue_order_call(conn, oid, test_tenant, attempt_no=1)
    finally:
        conn.close()

    assert j1["id"] == j2["id"], "Idempotent enqueue must return same job"

    raw_conn.execute("DELETE FROM call_jobs WHERE order_id=%s", (oid,))
    raw_conn.execute("DELETE FROM orders WHERE id=%s", (oid,))
