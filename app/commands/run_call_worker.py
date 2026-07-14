"""
Production call worker service.

Polls call_jobs table with FOR UPDATE SKIP LOCKED to atomically lease
one job per worker. Multiple worker instances can run safely — each
leases a different job. Dead worker leases expire after lease_expires_at
and are recovered by any live worker.

Usage:
    FLASK_ENV=production python -m app.commands.run_call_worker

Environment:
    ENABLE_CALL_WORKER=true  (required — guards against accidental invocation)
    WORKER_ID                (optional — defaults to pid:hostname)
    DATABASE_URL             (required)
    REDIS_URL                (required)
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
import socket
import datetime

logger = logging.getLogger("ccall.call_worker")

_UTC = datetime.timezone.utc
_SHUTDOWN = False


def _handle_shutdown(signum, frame):
    global _SHUTDOWN
    logger.info("Call worker received signal %s — initiating graceful shutdown.", signum)
    _SHUTDOWN = True


def _worker_id() -> str:
    return os.environ.get("WORKER_ID", f"{os.getpid()}@{socket.gethostname()}")


def _lease_job(conn, worker_id: str, lease_seconds: int = 120) -> dict | None:
    """Atomically claim one queued job. Returns job row or None."""
    now = datetime.datetime.now(_UTC)
    expires = now + datetime.timedelta(seconds=lease_seconds)
    row = conn.execute(
        """
        UPDATE call_jobs
        SET    job_status     = 'leased',
               leased_by      = %s,
               leased_at      = %s,
               lease_expires_at = %s
        WHERE  id = (
            SELECT id FROM call_jobs
            WHERE  job_status IN ('queued', 'leased')
              AND  (job_status = 'queued'
                   OR lease_expires_at < %s)   -- recover expired leases
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, order_id, tenant_id, caller_id, audio_path,
                  attempt_no, priority, created_at
        """,
        (worker_id, now, expires, now),
    ).fetchone()
    return dict(row) if row else None


def _complete_job(conn, job_id: int, result: str) -> None:
    conn.execute(
        "UPDATE call_jobs SET job_status=%s, completed_at=now() WHERE id=%s",
        (result, job_id),
    )


def _process_one_job(app, job: dict, worker_id: str) -> None:
    """Execute the SIP call for one leased job inside app context."""
    with app.app_context():
        from app.db import db
        from app.services.subscription_access_service import check_feature_access
        from app.services.wallet_service import check_balance
        from app.services.rate_service import estimate_charge

        order_id = job["order_id"]
        tenant_id = job["tenant_id"]

        # Re-check subscription before provider call
        access = check_feature_access(tenant_id, "order_intake")
        if not access.allowed:
            logger.warning(
                "call_worker: job %s tenant %s subscription blocked (%s) — skipping.",
                job["id"], tenant_id, access.message,
            )
            with db.transaction() as conn:
                _complete_job(conn, job["id"], "cancelled")
            return

        # Re-check wallet balance
        min_charge = estimate_charge(tenant_id)
        if not check_balance(tenant_id, min_charge):
            logger.warning(
                "call_worker: job %s tenant %s insufficient balance — skipping.",
                job["id"], tenant_id,
            )
            with db.transaction() as conn:
                _complete_job(conn, job["id"], "cancelled")
            return

        try:
            from app.services.order_service import queue_confirmation_call
            queue_confirmation_call(order_id, tenant_id)
            with db.transaction() as conn:
                _complete_job(conn, job["id"], "completed")
            logger.info("call_worker: job %s completed (order=%s).", job["id"], order_id)
        except Exception as exc:
            logger.error(
                "call_worker: job %s failed: %s", job["id"], exc, exc_info=True
            )
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE call_jobs SET job_status='failed', error_message=%s, completed_at=now() WHERE id=%s",
                    (str(exc)[:500], job["id"]),
                )


def main() -> None:
    if os.environ.get("ENABLE_CALL_WORKER", "false").lower() != "true":
        print(
            "ENABLE_CALL_WORKER is not 'true'. "
            "Set ENABLE_CALL_WORKER=true to start the call worker service.",
            file=sys.stderr,
        )
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    from dotenv import load_dotenv
    load_dotenv()

    config_name = os.environ.get("FLASK_ENV", "production")
    from app import create_app
    app = create_app(config_name)

    worker_id = _worker_id()
    poll_interval = float(os.environ.get("CALL_WORKER_POLL_SECONDS", "2"))
    logger.info("Call worker started. worker_id=%s poll=%.1fs", worker_id, poll_interval)

    from app.services.order_service import start_call_monitor
    start_call_monitor(app)

    with app.app_context():
        from app.db import db

        while not _SHUTDOWN:
            try:
                with db.transaction() as conn:
                    job = _lease_job(conn, worker_id)

                if job:
                    logger.info(
                        "call_worker: leased job %s order=%s tenant=%s attempt=%s",
                        job["id"], job["order_id"], job["tenant_id"], job["attempt_no"],
                    )
                    _process_one_job(app, job, worker_id)
                else:
                    time.sleep(poll_interval)
            except Exception as exc:
                logger.error("call_worker: poll loop error: %s", exc, exc_info=True)
                time.sleep(poll_interval * 2)

    logger.info("Call worker stopped cleanly.")


if __name__ == "__main__":
    main()
