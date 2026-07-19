"""Production call worker service.

Polls call_jobs with FOR UPDATE SKIP LOCKED to atomically lease one job per
worker process. Multiple worker instances run safely — each leases a different
job. Dead-worker leases expire after lease_expires_at and are recovered by any
live worker on the next poll.

Dispatch contract
-----------------
1. Reload job, order and attempt using tenant-safe queries.
2. Verify order is still eligible (not terminal, not already CALLING with call).
3. Verify no call already exists for the current attempt (crash-after-call guard).
4. Call queue_confirmation_call() synchronously — it creates the calls row and
   starts the SIP subprocess.
5. Receive call_id. Only mark job completed once call_id is confirmed.
6. Mark job completed/dispatched with call_id. Job never marked completed merely
   because a background task was submitted.

Failure handling
----------------
Retryable (retry_wait with backoff):
  - SIP trunk temporarily unavailable (MOCK_SIP / temporary provider error)
  - Transient DB serialization errors
  - Worker interrupted after lease but before dispatch

Permanent (failed → order marked FAILED):
  - Missing tenant or order
  - Order already terminal
  - No caller ID configured
  - Attempt exceeds order max_attempts
  - Any unclassified exception after max_dispatch_attempts

Usage:
    ENABLE_CALL_WORKER=true FLASK_ENV=production python -m app.commands.run_call_worker

Environment:
    ENABLE_CALL_WORKER=true         required
    WORKER_ID                       optional (defaults to pid@hostname)
    CALL_WORKER_POLL_SECONDS        optional (default 2)
    CALL_WORKER_LEASE_SECONDS       optional (default 120)
    CALL_WORKER_BATCH_SIZE          optional (default 1)
    DATABASE_URL                    required
    REDIS_URL                       required
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import time
import datetime

logger = logging.getLogger("ccall.call_worker")

_UTC = datetime.timezone.utc
_SHUTDOWN = False

# Re-export for config validation.
from app.services.call_job_service import DEFAULT_MAX_DISPATCH_ATTEMPTS, release_expired_leases


def _handle_shutdown(signum, frame):
    global _SHUTDOWN
    logger.info("Call worker received signal %s — graceful shutdown initiated.", signum)
    _SHUTDOWN = True


def _worker_id() -> str:
    return os.environ.get("WORKER_ID", f"{os.getpid()}@{socket.gethostname()}")


# ── Job leasing ───────────────────────────────────────────────────────────────

def _lease_job(conn, worker_id: str, lease_seconds: int = 120) -> dict | None:
    """Atomically claim one available job. Returns job row dict or None.

    Selects jobs where:
      - status is 'queued' or 'retry_wait', AND
      - available_at <= NOW()
    Also recovers expired 'leased' jobs whose lease_expires_at has passed.

    FOR UPDATE SKIP LOCKED ensures no two workers can claim the same job.
    """
    now = datetime.datetime.now(_UTC)
    expires = now + datetime.timedelta(seconds=lease_seconds)

    row = conn.execute(
        """
        UPDATE call_jobs
        SET    job_status       = 'leased',
               leased_by        = %s,
               leased_at        = %s,
               lease_expires_at = %s,
               attempts         = attempts + 1,
               updated_at       = now()
        WHERE  id = (
            SELECT id FROM call_jobs
            WHERE  (
                       (job_status IN ('queued', 'retry_wait') AND available_at <= %s)
                    OR (job_status = 'leased' AND lease_expires_at < %s)
                   )
            ORDER  BY available_at ASC, priority DESC, id ASC
            LIMIT  1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, order_id, tenant_id, attempt_no, attempts, max_attempts,
                  caller_id, audio_path, priority, created_at,
                  order_call_attempt_id, call_id
        """,
        (worker_id, now, expires, now, now),
    ).fetchone()

    return dict(row) if row else None


# ── Dispatch ──────────────────────────────────────────────────────────────────

_TERMINAL_ORDER_STATUSES = frozenset({
    "CONFIRMED", "CANCELLED", "UNREACHABLE", "FAILED",
})


def _dispatch_job(app, job: dict) -> None:
    """Execute one leased job inside app context.

    Uses dispatch_order_call() which returns a structured DispatchResult.
    No exception message string parsing for classification.

    Dispatch success boundary:
      1. calls row committed by start_call()
      2. order_call_attempts row created with call_id linked
      3. SIP executor accepted the thread (start_call returned non-zero call_id)
    """
    from app.db import db
    from app.monitoring import CALL_JOB_DISPATCH_SUCCESS, CALL_JOB_DISPATCH_FAILURE
    from app.services.call_job_service import (
        dispatch_order_call, mark_dispatched, mark_failed, mark_retry_wait, mark_cancelled,
    )

    job_id       = job["id"]
    order_id     = job["order_id"]
    tenant_id    = job["tenant_id"]
    attempt_no   = job["attempt_no"]
    attempts     = job["attempts"]      # incremented in _lease_job
    max_attempts = job["max_attempts"]

    result = dispatch_order_call(order_id, tenant_id, attempt_no)

    if result.accepted and result.call_id:
        with db.transaction() as conn:
            mark_dispatched(conn, job_id, result.call_id, result.order_call_attempt_id)
        CALL_JOB_DISPATCH_SUCCESS.inc()
        logger.info(
            "call_worker: job=%s order=%s attempt=%s call_id=%s — completed",
            job_id, order_id, attempt_no, result.call_id,
        )
        return

    CALL_JOB_DISPATCH_FAILURE.labels(error_code=result.error_code).inc()
    logger.warning(
        "call_worker: job=%s order=%s attempt=%s code=%s retryable=%s msg=%s",
        job_id, order_id, attempt_no, result.error_code, result.retryable, result.safe_message,
    )

    if result.error_code in ("terminal_order", "tenant_mismatch"):
        with db.transaction() as conn:
            mark_cancelled(conn, job_id)
        return

    if result.retryable and attempts < max_attempts:
        with db.transaction() as conn:
            mark_retry_wait(conn, job_id, attempts, max_attempts, result.error_code, result.safe_message)
    else:
        with db.transaction() as conn:
            mark_failed(conn, job_id, result.error_code, result.safe_message)
            conn.execute(
                "UPDATE orders SET order_status='FAILED', updated_at=now() "
                "WHERE id=%s AND order_status='CALL_QUEUED'",
                (order_id,),
            )


# ── Worker loop ───────────────────────────────────────────────────────────────

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

    worker_id     = _worker_id()
    poll_interval = float(os.environ.get("CALL_WORKER_POLL_SECONDS", "2"))
    lease_seconds = int(os.environ.get("CALL_WORKER_LEASE_SECONDS", "120"))
    batch_size    = int(os.environ.get("CALL_WORKER_BATCH_SIZE", "1"))

    # Validate configuration — fail fast on nonsense values.
    if poll_interval < 0.5:
        raise ValueError(f"CALL_WORKER_POLL_SECONDS={poll_interval} too low (min 0.5)")
    if lease_seconds < 30:
        raise ValueError(f"CALL_WORKER_LEASE_SECONDS={lease_seconds} too low (min 30)")
    if batch_size < 1 or batch_size > 50:
        raise ValueError(f"CALL_WORKER_BATCH_SIZE={batch_size} out of range [1, 50]")
    max_dispatch_attempts = int(os.environ.get("CALL_WORKER_MAX_DISPATCH_ATTEMPTS",
                                               str(DEFAULT_MAX_DISPATCH_ATTEMPTS)))
    if max_dispatch_attempts < 1:
        raise ValueError(f"CALL_WORKER_MAX_DISPATCH_ATTEMPTS={max_dispatch_attempts} must be >= 1")

    logger.info(
        "Call worker started. worker_id=%s poll=%.1fs lease=%ds batch=%d",
        worker_id, poll_interval, lease_seconds, batch_size,
    )

    # Call monitor: only start when explicitly enabled.
    # With replicas > 1, multiple workers would each start an unsafe monitor loop.
    # Set ENABLE_CALL_MONITOR=true only in the scheduler (replicas=1) or a
    # dedicated singleton service.
    if os.environ.get("ENABLE_CALL_MONITOR", "false").lower() == "true":
        # Fail closed: if Redis is unavailable the monitor must NOT start.
        # The monitor relies on Redis for distributed locking; without it a
        # second instance could run concurrently and cause split-brain.
        _redis_ok = False
        try:
            from app.extensions import redis_ext
            redis_ext.client.ping()
            _redis_ok = True
        except Exception as _exc:
            logger.error(
                "call_worker: Redis unavailable — call monitor will NOT start "
                "(fail-closed). Error type: %s", type(_exc).__name__,
            )
        if _redis_ok:
            from app.services.order_service import start_call_monitor
            logger.info("call_worker: starting call monitor (ENABLE_CALL_MONITOR=true, Redis OK)")
            start_call_monitor(app)
        else:
            logger.warning("call_worker: call monitor skipped — Redis not reachable")
    else:
        logger.info("call_worker: call monitor disabled (ENABLE_CALL_MONITOR!=true)")

    with app.app_context():
        from app.db import db
        from app.monitoring import CALL_WORKER_LAST_POLL, EXPIRED_LEASES_RECOVERED

        while not _SHUTDOWN:
            try:
                dispatched_in_batch = 0
                for _ in range(batch_size):
                    if _SHUTDOWN:
                        break
                    with db.transaction() as conn:
                        job = _lease_job(conn, worker_id, lease_seconds)
                    if job:
                        logger.info(
                            "call_worker: leased job_id=%s order_id=%s tenant_id=%s attempt=%s",
                            job["id"], job["order_id"], job["tenant_id"], job["attempt_no"],
                        )
                        _dispatch_job(app, job)
                        dispatched_in_batch += 1
                    else:
                        break  # no more jobs; stop this batch

                # Update heartbeat timestamp (last successful poll tick).
                CALL_WORKER_LAST_POLL.set_to_current_time()

                if dispatched_in_batch == 0:
                    # Release expired leases as part of idle maintenance.
                    try:
                        with db.transaction() as conn:
                            recovered = release_expired_leases(conn)
                        if recovered > 0:
                            EXPIRED_LEASES_RECOVERED.inc(recovered)
                    except Exception:
                        pass
                    time.sleep(poll_interval)

            except Exception as exc:
                logger.error(
                    "call_worker: poll loop error (type=%s) — will retry in %.1fs",
                    type(exc).__name__, poll_interval * 2,
                )
                time.sleep(poll_interval * 2)

    logger.info("Call worker stopped cleanly.")


if __name__ == "__main__":
    main()
