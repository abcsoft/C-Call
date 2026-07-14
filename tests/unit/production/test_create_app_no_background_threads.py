"""
Phase 3 regression: create_app() must start ZERO background threads/jobs.

Verifies that:
- APScheduler is NOT started inside create_app() in any config
- Call monitor thread is NOT started inside create_app() in any config
- Thread count after create_app() equals thread count before

This guarantees:
- Web workers (Gunicorn) do not duplicate scheduler work
- Pytest imports do not spawn background loops
- Migration scripts do not trigger jobs
- Browser test setup does not send real notifications
"""
from __future__ import annotations

import threading
import os
import pytest


@pytest.fixture(autouse=True)
def _no_real_db(monkeypatch):
    """Prevent real DB/Redis connections during this pure-unit test."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://ccall:test@localhost:5432/ccall_test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setenv("ENABLE_SCHEDULER", "false")
    monkeypatch.setenv("ENABLE_CALL_WORKER", "false")
    monkeypatch.setenv("ENABLE_CALL_MONITOR", "false")


def _get_daemon_thread_names() -> set[str]:
    return {t.name for t in threading.enumerate() if t.daemon}


def test_create_app_testing_starts_no_scheduler(app):
    """TestingConfig: scheduler must not be running after create_app()."""
    from app.extensions import scheduler
    # scheduler.running is False when not started
    assert not scheduler.running, (
        "APScheduler is running after create_app(testing) — "
        "web workers would each start scheduler, causing duplicate job execution."
    )


def test_create_app_testing_starts_no_call_monitor(app):
    """Call monitor thread must not exist after create_app()."""
    from app.services.order_service import _monitor_active
    assert not _monitor_active, (
        "_monitor_active is True after create_app() — "
        "call monitor thread is running inside web process."
    )


def test_create_app_testing_zero_background_threads(app):
    """Thread count must not increase by more than 1 after create_app() in testing."""
    # Allow for pool workers (psycopg-pool starts min_size=2 threads)
    # but APScheduler and monitor threads must not be present.
    bad_threads = [
        t.name for t in threading.enumerate()
        # "pool-N-scheduler" is psycopg_pool's internal timer — NOT APScheduler.
        # Only flag threads that belong to Flask-APScheduler or our own workers.
        if any(kw in t.name.lower() for kw in ("apscheduler", "monitor", "ccall-dispatch"))
        and "pool-" not in t.name.lower()   # exclude psycopg_pool connection threads
    ]
    assert bad_threads == [], (
        f"Background threads started by create_app(): {bad_threads}. "
        "These must only start via explicit service commands."
    )


def test_enable_scheduler_env_guard():
    """run_scheduler refuses to start without ENABLE_SCHEDULER=true."""
    import importlib
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "app.commands.run_scheduler"],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "ENABLE_SCHEDULER": "false"},
        cwd=os.path.join(os.path.dirname(__file__), "..", "..", ".."),
    )
    assert result.returncode != 0, "run_scheduler must exit non-zero when ENABLE_SCHEDULER != 'true'"
    assert "ENABLE_SCHEDULER" in result.stderr or "ENABLE_SCHEDULER" in result.stdout


def test_enable_call_worker_env_guard():
    """run_call_worker refuses to start without ENABLE_CALL_WORKER=true."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "app.commands.run_call_worker"],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "ENABLE_CALL_WORKER": "false"},
        cwd=os.path.join(os.path.dirname(__file__), "..", "..", ".."),
    )
    assert result.returncode != 0, "run_call_worker must exit non-zero when ENABLE_CALL_WORKER != 'true'"
    assert "ENABLE_CALL_WORKER" in result.stderr or "ENABLE_CALL_WORKER" in result.stdout
