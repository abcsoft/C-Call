"""
Tests for migrations/run_migrations.py advisory lock.

Runs against TEST_DATABASE_URL (real PostgreSQL).
Skipped when TEST_DATABASE_URL is not set.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
MIGRATIONS_SCRIPT = str(
    Path(__file__).resolve().parents[3] / "migrations" / "run_migrations.py"
)
LOCK_KEY = 99999

pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="TEST_DATABASE_URL not set — skipped"
)


@pytest.fixture()
def db_conn():
    psycopg = pytest.importorskip("psycopg")
    conn = psycopg.connect(TEST_DB, autocommit=True)
    yield conn
    conn.close()


# ── 1. First runner acquires lock ─────────────────────────────────────────────
def test_lock_acquired_by_first_session(db_conn):
    row = db_conn.execute(
        "SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)
    ).fetchone()
    assert row[0] is True, "First session must acquire the advisory lock"
    # Release
    db_conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


# ── 2. Second runner rejected while first holds lock ─────────────────────────
def test_second_runner_rejected_while_lock_held(db_conn):
    psycopg = pytest.importorskip("psycopg")
    # First session holds
    db_conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        # Second connection tries non-blocking acquire
        conn2 = psycopg.connect(TEST_DB, autocommit=True)
        try:
            row = conn2.execute(
                "SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)
            ).fetchone()
            assert row[0] is False, "Second session must NOT acquire lock while first holds it"
        finally:
            conn2.close()
    finally:
        db_conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


# ── 3. Lock released after success ───────────────────────────────────────────
def test_lock_released_after_connection_close():
    psycopg = pytest.importorskip("psycopg")
    # Open, acquire, close
    conn1 = psycopg.connect(TEST_DB, autocommit=True)
    conn1.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    conn1.close()  # session-scoped lock released here

    # Now another session should be able to acquire
    conn2 = psycopg.connect(TEST_DB, autocommit=True)
    try:
        row = conn2.execute(
            "SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)
        ).fetchone()
        assert row[0] is True, "Lock must be available after previous session closed"
        conn2.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    finally:
        conn2.close()


# ── 4. Lock released after failure ───────────────────────────────────────────
def test_lock_released_after_connection_close_on_failure():
    """Same as #3 — session lock auto-releases on any disconnect."""
    psycopg = pytest.importorskip("psycopg")
    conn1 = psycopg.connect(TEST_DB, autocommit=True)
    conn1.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    conn1.close()  # simulates crash / sys.exit

    conn2 = psycopg.connect(TEST_DB, autocommit=True)
    try:
        row = conn2.execute(
            "SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)
        ).fetchone()
        assert row[0] is True, "Lock must be reclaimable after failed session disconnects"
        conn2.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    finally:
        conn2.close()


# ── 5. Concurrent subprocess: second exits non-zero ──────────────────────────
def test_concurrent_subprocess_exits_nonzero(db_conn):
    """Hold lock from main connection; subprocess migration runner exits 2."""
    db_conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        result = subprocess.run(
            [sys.executable, MIGRATIONS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "DATABASE_URL": TEST_DB},
        )
        assert result.returncode != 0, (
            "Migration runner must exit non-zero when advisory lock is held"
        )
        assert "advisory lock" in result.stdout.lower() or "ERROR" in result.stdout, (
            f"Expected lock error message, got: {result.stdout}"
        )
    finally:
        db_conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


# ── 6. Dry-run does not acquire lock ─────────────────────────────────────────
def test_dry_run_does_not_acquire_or_block(db_conn):
    """--dry-run must not acquire the advisory lock."""
    # Hold the lock from main connection
    db_conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        result = subprocess.run(
            [sys.executable, MIGRATIONS_SCRIPT, "--dry-run"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "DATABASE_URL": TEST_DB},
        )
        # Dry-run should succeed (exit 0) even when lock is held
        assert result.returncode == 0, (
            f"--dry-run must not block on advisory lock. stderr: {result.stderr}"
        )
    finally:
        db_conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


# ── 7. schema_migrations remains consistent after lock rejection ──────────────
def test_schema_migrations_consistent_after_lock_rejection(db_conn):
    """After concurrent runner is blocked, schema_migrations unchanged."""
    # Snapshot current migration count
    row = db_conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
    count_before = row[0] if row else 0

    # Hold lock and run a competing migration (will fail at lock)
    db_conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        subprocess.run(
            [sys.executable, MIGRATIONS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "DATABASE_URL": TEST_DB},
        )
    finally:
        db_conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))

    row = db_conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
    count_after = row[0] if row else 0
    assert count_before == count_after, (
        f"schema_migrations changed while lock was held: {count_before} → {count_after}"
    )
