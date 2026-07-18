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

_RETRYABLE_ERROR_CODES = frozenset({
    "sip_unavailable", "provider_error", "db_serialization", "no_caller_id_transient",
})


def _classify_exception(exc: Exception) -> tuple[bool, str, str]:
    """Return (retryable, error_code, safe_message) from an exception."""
    msg = str(exc).lower()
    # Explicit permanent signals from queue_confirmation_call
    if "invalid order_id or tenant_id" in msg:
        return False, "invalid_reference", "Order or tenant not found"
    if "no active caller id" in msg or "no active phone" in msg:
        return False, "no_caller_id", "No active caller ID assigned to tenant"
    # Transient SIP / network signals
    if any(k in msg for k in ("connection refused", "timeout", "network", "temporarily")):
        return True, "sip_unavailable", "Transient SIP/network error"
    # Default: retryable for first N attempts, then permanent
    return True, "dispatch_error", "Dispatch error (see worker logs)"


def _dispatch_job(app, job: dict) -> None:
    """Execute one leased job inside app context.

    1. Reload order; verify eligibility and tenant ownership.
    2. Check crash-after-call guard (existing attempt with call_id).
    3. Call queue_confirmation_call() to start SIP call.
    4. Mark job completed or retry_wait/failed based on result.
    """
    from app.db import db
    from app.services.call_job_service import (
        DispatchResult, mark_dispatched, mark_failed, mark_retry_wait, mark_cancelled,
    )
    from app.models.order import get_order_by_id_system

    job_id    = job["id"]
    order_id  = job["order_id"]
    tenant_id = job["tenant_id"]
    attempt_no = job["attempt_no"]
    attempts   = job["attempts"]        # incremented in _lease_job
    max_attempts = job["max_attempts"]

    # ── 1. Reload order ───────────────────────────────────────────────────────
    order = get_order_by_id_system(order_id)
    if not order or int(order.get("tenant_id", 0)) != tenant_id:
        logger.warning(
            "call_worker: job=%s order=%s tenant=%s — order not found or tenant mismatch",
            job_id, order_id, tenant_id,
        )
        with db.transaction() as conn:
            mark_failed(conn, job_id, "invalid_reference", "Order not found or tenant mismatch")
        return

    # ── 2. Eligibility check ──────────────────────────────────────────────────
    order_status = order.get("order_status", "")
    if order_status in _TERMINAL_ORDER_STATUSES:
        logger.info(
            "call_worker: job=%s order=%s already terminal (%s) — cancelling job",
            job_id, order_id, order_status,
        )
        with db.transaction() as conn:
            mark_cancelled(conn, job_id)
        return

    # ── 3. Crash-after-call duplicate guard ───────────────────────────────────
    # If the previous lease created the attempt row and call, but crashed before
    # marking the job completed, we must not start a second call.
    existing_attempt = db.query_one(
        """SELECT id, call_id FROM order_call_attempts
           WHERE order_id = %s AND tenant_id = %s AND attempt_no = %s
             AND call_id IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        (order_id, tenant_id, attempt_no),
    )
    if existing_attempt and existing_attempt["call_id"]:
        existing_call_id = int(existing_attempt["call_id"])
        existing_attempt_id = int(existing_attempt["id"])
        logger.info(
            "call_worker: job=%s order=%s attempt=%s — call already exists (call_id=%s) "
            "crash recovery: marking completed without re-dispatch",
            job_id, order_id, attempt_no, existing_call_id,
        )
        with db.transaction() as conn:
            mark_dispatched(conn, job_id, existing_call_id, existing_attempt_id)
        return

    # ── 4. Dispatch ───────────────────────────────────────────────────────────
    try:
        from app.services.order_service import queue_confirmation_call
        call_id = queue_confirmation_call(order_id, tenant_id)
    except Exception as exc:
        retryable, code, safe_msg = _classify_exception(exc)
        logger.error(
            "call_worker: job=%s order=%s dispatch error code=%s retryable=%s: %s",
            job_id, order_id, code, retryable, exc,
        )
        if retryable and attempts < max_attempts:
            with db.transaction() as conn:
                mark_retry_wait(conn, job_id, attempts, max_attempts, code, safe_msg)
        else:
            with db.transaction() as conn:
                mark_failed(conn, job_id, code, safe_msg)
                # Transition order out of CALL_QUEUED so it doesn't get stuck.
                conn.execute(
                    "UPDATE orders SET order_status='FAILED', updated_at=now() "
                    "WHERE id=%s AND order_status='CALL_QUEUED'",
                    (order_id,),
                )
        return

    # ── 5. Evaluate dispatch result ───────────────────────────────────────────
    if call_id and call_id > 0:
        # Locate the attempt row that queue_confirmation_call created.
        attempt_row = db.query_one(
            """SELECT id FROM order_call_attempts
               WHERE order_id = %s AND tenant_id = %s AND call_id = %s
               ORDER BY id DESC LIMIT 1""",
            (order_id, tenant_id, call_id),
        )
        attempt_id = int(attempt_row["id"]) if attempt_row else None
        with db.transaction() as conn:
            mark_dispatched(conn, job_id, call_id, attempt_id)
        logger.info(
            "call_worker: job=%s order=%s call_id=%s — dispatched and completed",
            job_id, order_id, call_id,
        )
    else:
        # queue_confirmation_call returned 0 — order set to FAILED inside it.
        code = "no_caller_id" if call_id == 0 else "dispatch_returned_zero"
        safe_msg = "Dispatch returned no call_id; order marked FAILED by dispatch function"
        logger.warning(
            "call_worker: job=%s order=%s dispatch returned call_id=%s — marking job failed",
            job_id, order_id, call_id,
        )
        with db.transaction() as conn:
            mark_failed(conn, job_id, code, safe_msg)


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

    worker_id    = _worker_id()
    poll_interval = float(os.environ.get("CALL_WORKER_POLL_SECONDS", "2"))
    lease_seconds = int(os.environ.get("CALL_WORKER_LEASE_SECONDS", "120"))

    logger.info(
        "Call worker started. worker_id=%s poll=%.1fs lease=%ds",
        worker_id, poll_interval, lease_seconds,
    )

    # Start call monitor (billing and order outcome processing).
    from app.services.order_service import start_call_monitor
    start_call_monitor(app)

    with app.app_context():
        from app.db import db

        while not _SHUTDOWN:
            try:
                with db.transaction() as conn:
                    job = _lease_job(conn, worker_id, lease_seconds)

                if job:
                    logger.info(
                        "call_worker: leased job_id=%s order_id=%s tenant_id=%s attempt=%s",
                        job["id"], job["order_id"], job["tenant_id"], job["attempt_no"],
                    )
                    with app.app_context():
                        _dispatch_job(app, job)
                else:
                    time.sleep(poll_interval)

            except Exception as exc:
                logger.error("call_worker: poll loop error: %s", exc, exc_info=True)
                time.sleep(poll_interval * 2)

    logger.info("Call worker stopped cleanly.")


if __name__ == "__main__":
    main()
