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


# ── Subscription lifecycle scheduler jobs ─────────────────────────────────

def job_subscription_lifecycle() -> None:
    """Hourly: expire trials, start grace periods, expire grace periods."""
    import datetime
    _UTC = datetime.timezone.utc
    now = datetime.datetime.now(_UTC)

    from app.services.subscription_lifecycle_service import (
        expire_subscription, enter_grace_period, enter_past_due
    )

    # 1. Expire trials whose trial_ends_at has passed
    expired_trials = db.query(
        "SELECT id FROM tenants WHERE status = 'trial' AND trial_ends_at IS NOT NULL AND trial_ends_at <= %s",
        (now,),
    )
    for t in expired_trials:
        try:
            expire_subscription(t["id"], from_status="trial")
            logger.info("subscription_lifecycle: expired trial for tenant %s", t["id"])
        except Exception:
            logger.warning("subscription_lifecycle: failed to expire trial for tenant %s", t["id"], exc_info=True)

    # 2. Move grace_period tenants whose grace_ends_at has passed to past_due
    past_grace = db.query(
        """
        SELECT t.id FROM tenants t
        JOIN subscriptions s ON s.id = t.current_subscription_id
        WHERE t.status = 'grace_period'
          AND s.grace_ends_at IS NOT NULL
          AND s.grace_ends_at <= %s
        """,
        (now,),
    )
    for t in past_grace:
        try:
            enter_past_due(t["id"])
            logger.info("subscription_lifecycle: grace period expired → past_due for tenant %s", t["id"])
        except Exception:
            logger.warning("subscription_lifecycle: failed to enter past_due for tenant %s", t["id"], exc_info=True)

    # 3. Cancel subscriptions whose cancel_at_period_end is set and period has ended
    eop_cancels = db.query(
        """
        SELECT t.id, s.id AS sub_id FROM tenants t
        JOIN subscriptions s ON s.id = t.current_subscription_id
        WHERE s.cancel_at_period_end = TRUE
          AND s.current_period_end IS NOT NULL
          AND s.current_period_end <= %s
          AND t.status IN ('active', 'grace_period')
        """,
        (now,),
    )
    from app.services.subscription_lifecycle_service import cancel_subscription
    for t in eop_cancels:
        try:
            cancel_subscription(t["id"], immediate=True, reason="End-of-period cancellation", actor_type="system")
            logger.info("subscription_lifecycle: end-of-period cancel for tenant %s", t["id"])
        except Exception:
            logger.warning("subscription_lifecycle: failed to cancel EOP for tenant %s", t["id"], exc_info=True)

    # 4. Send trial-ending-soon notifications (3 days before expiry)
    _send_trial_ending_soon_notifications(now)


def _send_trial_ending_soon_notifications(now) -> None:
    """Email tenants whose trial ends in ≤ 3 days (once per tenant per day via Redis lock)."""
    import datetime
    window_end = now + datetime.timedelta(days=3)
    tenants = db.query(
        """
        SELECT id, email, business_name, trial_ends_at
        FROM tenants
        WHERE status = 'trial'
          AND trial_ends_at IS NOT NULL
          AND trial_ends_at > %s
          AND trial_ends_at <= %s
        """,
        (now, window_end),
    )
    for t in tenants:
        try:
            from app.extensions import redis_ext
            lock_key = f"trial_ending_notif:{t['id']}:{now.strftime('%Y-%m-%d')}"
            if redis_ext.client.set(lock_key, "1", ex=86400, nx=True):
                from app.services.email_service import send_trial_ending_soon_email
                send_trial_ending_soon_email(t["id"], t["trial_ends_at"])
        except Exception:
            logger.warning("trial_ending_soon: notification failed for tenant %s", t["id"], exc_info=True)


def scheduled_subscription_lifecycle() -> None:
    """Hourly subscription lifecycle checks — trial expiry, grace, past_due, EOP cancel."""
    from app.extensions import scheduler
    with scheduler.app.app_context():
        job_subscription_lifecycle()
