"""
Phase 3: Call job queue — real PostgreSQL concurrency tests.

Verifies:
- Two workers cannot lease the same job (FOR UPDATE SKIP LOCKED)
- Expired leases are recoverable
- Terminal jobs cannot be re-leased
- Job status transitions are correct
- Worker crash simulation allows recovery
- Duplicate completion is idempotent

Uses direct psycopg connections (bypasses app pool) so tests are
fully isolated from Flask. Skipped when TEST_DATABASE_URL not set.
"""
from __future__ import annotations

import datetime
import os
import threading
import time
import uuid

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
_UTC = datetime.timezone.utc

pytestmark = pytest.mark.skipif(
    not TEST_DB,
    reason="TEST_DATABASE_URL not set — skipped",
)


@pytest.fixture()
def raw_conn():
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(TEST_DB, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture()
def test_tenant(raw_conn):
    """Insert a throw-away tenant and clean up."""
    subdomain = f"qtest-{uuid.uuid4().hex[:8]}"
    row = raw_conn.execute(
        """INSERT INTO tenants (business_name, subdomain, status, plan_id,
               email, uuid)
           VALUES (%s, %s, 'active',
               (SELECT id FROM plans ORDER BY id LIMIT 1),
               %s, gen_random_uuid())
           RETURNING id""",
        (f"QueueTest-{subdomain}", subdomain, f"{subdomain}@test.example"),
    ).fetchone()
    tid = row[0]
    # Wallet required for tenant
    raw_conn.execute(
        "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 500.00) ON CONFLICT DO NOTHING",
        (tid,)
    )
    yield tid
    raw_conn.execute("DELETE FROM call_jobs WHERE tenant_id=%s", (tid,))
    raw_conn.execute("DELETE FROM orders WHERE tenant_id=%s", (tid,))
    raw_conn.execute("DELETE FROM wallets WHERE tenant_id=%s", (tid,))
    raw_conn.execute("DELETE FROM tenants WHERE id=%s", (tid,))


@pytest.fixture()
def test_order(raw_conn, test_tenant):
    ext_id = f"QTEST-{uuid.uuid4().hex[:8]}"
    row = raw_conn.execute(
        """INSERT INTO orders (tenant_id, external_order_id, phone, customer_name,
               amount, order_status, attempt_count)
           VALUES (%s, %s, '01700000099', 'QTest', '100.00', 'CALL_QUEUED', 0)
           RETURNING id""",
        (test_tenant, ext_id),
    ).fetchone()
    yield row[0]


def _insert_job(conn, order_id: int, tenant_id: int) -> int:
    row = conn.execute(
        """INSERT INTO call_jobs (order_id, tenant_id, job_status, attempt_no)
           VALUES (%s, %s, 'queued', 1) RETURNING id""",
        (order_id, tenant_id),
    ).fetchone()
    return row[0]


def _lease_job(conn, worker_id: str) -> dict | None:
    now = datetime.datetime.now(_UTC)
    expires = now + datetime.timedelta(seconds=120)
    row = conn.execute(
        """
        UPDATE call_jobs
        SET job_status='leased', leased_by=%s, leased_at=%s, lease_expires_at=%s
        WHERE id=(
            SELECT id FROM call_jobs
            WHERE job_status='queued'
               OR (job_status='leased' AND lease_expires_at < %s)
            ORDER BY priority DESC, created_at ASC LIMIT 1
            FOR UPDATE SKIP LOCKED
        ) RETURNING id, order_id, leased_by, lease_expires_at
        """,
        (worker_id, now, expires, now),
    ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "order_id": row[1], "leased_by": row[2], "lease_expires_at": row[3]}


# ── 1. Two workers, one job, only one lease ───────────────────────────────────
def test_two_workers_exclusive_lease(test_order, test_tenant):
    """FOR UPDATE SKIP LOCKED: exactly one worker leases the job."""
    psycopg = pytest.importorskip("psycopg")
    _conn = psycopg.connect(TEST_DB, autocommit=True)
    job_id = _insert_job(_conn, test_order, test_tenant)
    _conn.close()

    results: list[dict | None] = []
    errors: list[Exception] = []

    def worker(name: str):
        c = psycopg.connect(TEST_DB, autocommit=True)
        try:
            results.append(_lease_job(c, name))
        except Exception as e:
            errors.append(e)
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=("worker-A",))
    t2 = threading.Thread(target=worker, args=("worker-B",))
    t1.start(); t2.start()
    t1.join(10); t2.join(10)

    assert not errors, f"Worker errors: {errors}"
    leases = [r for r in results if r is not None]
    assert len(leases) == 1, (
        f"Expected exactly 1 lease, got {len(leases)}. "
        "FOR UPDATE SKIP LOCKED not preventing duplicate lease."
    )


# ── 2. leased_by set exactly once ────────────────────────────────────────────
def test_leased_by_set_once(raw_conn, test_order, test_tenant):
    """leased_by and lease_expires_at set on first lease only."""
    _insert_job(raw_conn, test_order, test_tenant)
    job = _lease_job(raw_conn, "solo-worker")
    assert job is not None
    assert job["leased_by"] == "solo-worker"
    assert job["lease_expires_at"] is not None

    # Second lease attempt on same job returns nothing
    job2 = _lease_job(raw_conn, "second-worker")
    assert job2 is None, "Leased job must not be re-leased while still valid"


# ── 3. Expired lease is recoverable ──────────────────────────────────────────
def test_expired_lease_recovered(raw_conn, test_order, test_tenant):
    """A job with expired lease is claimed by a new worker."""
    job_id = _insert_job(raw_conn, test_order, test_tenant)
    past = datetime.datetime.now(_UTC) - datetime.timedelta(seconds=200)
    raw_conn.execute(
        """UPDATE call_jobs SET job_status='leased', leased_by='dead-worker',
           leased_at=%s, lease_expires_at=%s WHERE id=%s""",
        (past, past + datetime.timedelta(seconds=120), job_id),
    )
    job = _lease_job(raw_conn, "recovery-worker")
    assert job is not None, "Expired lease must be recoverable"
    assert job["leased_by"] == "recovery-worker"


# ── 4. Terminal job not re-leasable ──────────────────────────────────────────
def test_terminal_job_not_releasable(raw_conn, test_order, test_tenant):
    """completed/failed/cancelled jobs must never be leased."""
    for status in ("completed", "failed", "cancelled"):
        jid = _insert_job(raw_conn, test_order, test_tenant)
        raw_conn.execute(
            "UPDATE call_jobs SET job_status=%s, completed_at=now() WHERE id=%s",
            (status, jid),
        )
    job = _lease_job(raw_conn, "bad-worker")
    assert job is None, "Terminal jobs must not be leasable"


# ── 5. Worker crash simulation — lease expires and is recovered ───────────────
def test_worker_crash_recovery(test_order, test_tenant):
    """Simulate crashed worker: lease expires, new worker claims it."""
    psycopg = pytest.importorskip("psycopg")

    # Worker A leases and "crashes" (closes connection without completing)
    conn_a = psycopg.connect(TEST_DB, autocommit=True)
    job_id = _insert_job(conn_a, test_order, test_tenant)
    conn_a.execute(
        """UPDATE call_jobs SET job_status='leased', leased_by='crashed-worker',
           leased_at=now() - interval '200 seconds',
           lease_expires_at=now() - interval '80 seconds'
           WHERE id=%s""",
        (job_id,),
    )
    conn_a.close()  # crash

    # New worker recovers
    conn_b = psycopg.connect(TEST_DB, autocommit=True)
    job = _lease_job(conn_b, "recovery-worker")
    conn_b.close()

    assert job is not None, "Recovery worker failed to claim crashed worker's expired lease"
    assert job["leased_by"] == "recovery-worker"


# ── 6. Status transition: queued → leased → completed ────────────────────────
def test_job_status_transitions(raw_conn, test_order, test_tenant):
    """Valid state machine: queued → leased → completed."""
    job_id = _insert_job(raw_conn, test_order, test_tenant)

    row = raw_conn.execute(
        "SELECT job_status FROM call_jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert row[0] == "queued"

    job = _lease_job(raw_conn, "state-worker")
    assert job is not None
    row = raw_conn.execute(
        "SELECT job_status, leased_by FROM call_jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert row[0] == "leased"
    assert row[1] == "state-worker"

    raw_conn.execute(
        "UPDATE call_jobs SET job_status='completed', completed_at=now() WHERE id=%s",
        (job_id,),
    )
    row = raw_conn.execute(
        "SELECT job_status FROM call_jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert row[0] == "completed"


# ── 7. Duplicate completion idempotent ───────────────────────────────────────
def test_duplicate_completion_idempotent(raw_conn, test_order, test_tenant):
    """Marking a completed job as completed again is idempotent."""
    job_id = _insert_job(raw_conn, test_order, test_tenant)
    _lease_job(raw_conn, "worker-X")
    raw_conn.execute(
        "UPDATE call_jobs SET job_status='completed', completed_at=now() WHERE id=%s",
        (job_id,),
    )
    # Second completion attempt must not raise
    raw_conn.execute(
        "UPDATE call_jobs SET job_status='completed' WHERE id=%s AND job_status='completed'",
        (job_id,),
    )
    row = raw_conn.execute(
        "SELECT job_status FROM call_jobs WHERE id=%s", (job_id,)
    ).fetchone()
    assert row[0] == "completed"


# ── 8. DB assertion: leased_by stored correctly ──────────────────────────────
def test_db_assertion_leased_by(raw_conn, test_order, test_tenant):
    """Direct DB read confirms leased_by value after lease."""
    _insert_job(raw_conn, test_order, test_tenant)
    _lease_job(raw_conn, "verify-worker")
    row = raw_conn.execute(
        "SELECT leased_by, lease_expires_at, job_status FROM call_jobs WHERE order_id=%s",
        (test_order,),
    ).fetchone()
    assert row[0] == "verify-worker", f"leased_by={row[0]!r}, expected 'verify-worker'"
    assert row[1] is not None, "lease_expires_at must be set"
    assert row[2] == "leased", f"job_status={row[2]!r}, expected 'leased'"
