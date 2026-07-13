# E:\c-call-ivr\app\scheduler.py
"""Job bodies + the distributed-lock helper for the retry-engine scheduler.

This module does NOT own a scheduler instance — app/__init__.py already
runs one (Flask-APScheduler, PostgreSQL jobstore, registered via
@scheduler.task(...) inside create_app()). Running a second independent
BackgroundScheduler here would register a second copy of every job
against the same jobstore and double-execute them (double-dial the same
customer, double-process the same retry) — so this file only provides the
functions create_app() registers, plus the PostgreSQL advisory-lock guard
that keeps process_due_retries single-flight across multiple Gunicorn
workers/instances.
"""
from __future__ import annotations

import logging
from typing import Callable

from app.db import db

logger = logging.getLogger("ccall")

ADVISORY_LOCK_KEY = 12345


def with_advisory_lock(fn: Callable[[], None]) -> Callable[[], None]:
    """Run fn() only if this process acquires the cluster-wide Postgres
    advisory lock; otherwise skip silently (another worker already has it
    for this tick — it will do the work)."""
    def wrapped() -> None:
        row = db.query_one("SELECT pg_try_advisory_lock(%s) AS locked", (ADVISORY_LOCK_KEY,))
        if not row or not row["locked"]:
            logger.debug("%s: advisory lock held elsewhere, skipping this tick", fn.__name__)
            return
        try:
            fn()
        finally:
            db.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    wrapped.__name__ = getattr(fn, "__name__", "job")
    return wrapped


def job_process_retries() -> None:
    from app.services.retry_service import process_due_retries
    count = process_due_retries()
    if count:
        logger.info("process_retries: dispatched %s due retry call(s)", count)


def job_retry_webhooks() -> None:
    from app.services.webhook_service import retry_failed_webhooks
    count = retry_failed_webhooks()
    if count:
        logger.info("retry_webhooks: requeued %s failed webhook delivery(ies)", count)


def job_cleanup_sessions() -> None:
    row = db.query_one(
        "WITH d AS (DELETE FROM sessions WHERE expires_at < now() RETURNING 1) SELECT count(*) AS n FROM d"
    )
    n = row["n"] if row else 0
    if n:
        logger.info("cleanup_sessions: deleted %s expired session row(s)", n)


def job_low_balance_alerts() -> None:
    from app.services.wallet_service import check_low_balance_alert
    tenants = db.query("SELECT id FROM tenants WHERE status = 'active'")
    for t in tenants:
        try:
            check_low_balance_alert(t["id"])
        except Exception:
            logger.warning("low_balance_alerts: check failed for tenant %s", t["id"], exc_info=True)


def scheduled_process_retries() -> None:
    from app.extensions import scheduler
    with scheduler.app.app_context():
        with_advisory_lock(job_process_retries)()


def scheduled_retry_webhooks() -> None:
    from app.extensions import scheduler
    with scheduler.app.app_context():
        job_retry_webhooks()


def scheduled_cleanup_sessions() -> None:
    from app.extensions import scheduler
    with scheduler.app.app_context():
        job_cleanup_sessions()


def scheduled_low_balance_alerts() -> None:
    from app.extensions import scheduler
    with scheduler.app.app_context():
        job_low_balance_alerts()


def scheduled_expire_tts_reservations() -> None:
    """Expire stale TTS reservations and refund wallet overage debits idempotently."""
    from app.extensions import scheduler
    with scheduler.app.app_context():
        from app.services.tts_usage_service import expire_stale_tts_reservations
        expire_stale_tts_reservations()
