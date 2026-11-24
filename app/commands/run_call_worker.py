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

    # Testing only (FLASK_ENV=testing required):
    python -m app.commands.run_call_worker --once
    python -m app.commands.run_call_worker --max-jobs 3
    python -m app.commands.run_call_worker --test-fault-point after_lease

Environment:
    ENABLE_CALL_WORKER=true         required
    WORKER_ID                       optional (defaults to pid@hostname)
    CALL_WORKER_POLL_SECONDS        optional (default 2)
    CALL_WORKER_LEASE_SECONDS       optional (default 120)
    CALL_WORKER_BATCH_SIZE          optional (default 1)
    DATABASE_URL                    required
    REDIS_URL                       required

Test fault points (FLASK_ENV=testing only):
    after_lease      — crash after leasing job, before dispatch
    after_dispatch   — crash after successful dispatch, before mark_dispatched
"""
from __future__ import annotations

import argparse
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
            SELECT cj.id FROM call_jobs cj
            JOIN tenants t ON t.id = cj.tenant_id
            WHERE  COALESCE(cj.campaign_paused, false) = false
              AND  COALESCE(t.is_dialing_paused, false) = false
              AND  (
                       (cj.job_status IN ('queued', 'retry_wait') AND cj.available_at <= %s)
                    OR (cj.job_status = 'leased' AND cj.lease_expires_at < %s)
                   )
            ORDER  BY cj.available_at ASC, cj.priority DESC, cj.id ASC
            LIMIT  1
            FOR UPDATE OF cj SKIP LOCKED
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


_TEST_FAULT_POINT: str | None = None  # set by --test-fault-point (testing only)


def _raise_if_fault(point: str) -> None:
    """Simulate a process crash at a named point — testing only."""
    if _TEST_FAULT_POINT == point:
        logger.warning("test-fault-point=%s — simulating crash", point)
        os._exit(1)


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

    _raise_if_fault("after_lease")

    from app.services.call_job_service import check_provider_channel_capacity, update_call_state
    has_cap, provider_id = check_provider_channel_capacity(tenant_id)
    if not has_cap:
        with db.transaction() as conn:
            mark_retry_wait(conn, job_id, attempts, max_attempts, "channel_limit", "Provider channel limit reached")
        return

    with db.transaction() as conn:
        update_call_state(conn, job_id, "preparing")
        if provider_id is not None:
            conn.execute("UPDATE call_jobs SET provider_id = %s WHERE id = %s", (provider_id, job_id))

    result = dispatch_order_call(order_id, tenant_id, attempt_no)

    if result.accepted and result.call_id:
        _raise_if_fault("after_dispatch")          # legacy alias kept for existing tests
        _raise_if_fault("after_startup_accepted")  # fires at same boundary, named clearly
        _raise_if_fault("before_job_complete")     # before mark_dispatched writes call_id
        with db.transaction() as conn:
            mark_dispatched(conn, job_id, result.call_id, result.order_call_attempt_id)
            update_call_state(conn, job_id, "dialing")
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

    # Mark the pre-created attempt row as dispatch_failed so the monitor and
    # billing service can distinguish "provider failed to start" from "no answer".
    # Only set dispatch_failed when there is no call_id (no SIP dial occurred).
    if not result.call_id:
        try:
            db.execute(
                """UPDATE order_call_attempts
                   SET dispatch_status = 'dispatch_failed'
                   WHERE order_id = %s AND tenant_id = %s AND attempt_no = %s
                     AND dispatch_status IS NULL""",
                (order_id, tenant_id, attempt_no),
            )
        except Exception:
            pass

    if result.error_code in ("terminal_order", "tenant_mismatch", "duplicate_call_existing"):
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
                "WHERE id=%s AND order_status IN ('CALL_QUEUED', 'CALLING')",
                (order_id,),
            )


# ── Worker loop ───────────────────────────────────────────────────────────────

_VALID_FAULT_POINTS = frozenset({
    "after_lease",            # after leasing job, before dispatch
    "after_dispatch",         # after accepted dispatch, before mark_dispatched (legacy alias)
    "after_startup_accepted", # same as after_dispatch, named clearly
    "after_calls_row",        # fires inside call_runner.start_call() after calls INSERT
                              #   — implemented via _CALL_RUNNER_FAULT_POINT env var
    "before_job_complete",    # before mark_dispatched writes call_id to job
})


def main() -> None:
    global _TEST_FAULT_POINT

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--once", action="store_true",
                        help="Process at most one job then exit (testing only)")
    parser.add_argument("--max-jobs", type=int, default=None,
                        help="Exit after processing N jobs (testing only)")
    parser.add_argument("--test-fault-point", default=None,
                        help="Simulate crash at named point (testing only): "
                             "after_lease | after_dispatch | after_startup_accepted | "
                             "after_calls_row | before_job_complete")
    parser.add_argument("--help", "-h", action="store_true")
    args, _ = parser.parse_known_args()

    if args.help:
        parser.print_help()
        sys.exit(0)

    # Test-only options are disabled outside testing to prevent accidental use.
    _is_testing = os.environ.get("FLASK_ENV", "").lower() in ("testing", "test")
    if (args.once or args.max_jobs is not None or args.test_fault_point) and not _is_testing:
        print(
            "ERROR: --once / --max-jobs / --test-fault-point require FLASK_ENV=testing",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.test_fault_point:
        if args.test_fault_point not in _VALID_FAULT_POINTS:
            print(f"ERROR: unknown --test-fault-point '{args.test_fault_point}'. "
                  f"Valid: {sorted(_VALID_FAULT_POINTS)}", file=sys.stderr)
            sys.exit(1)
        _TEST_FAULT_POINT = args.test_fault_point
        # after_calls_row fires inside call_runner.start_call() (a different module).
        # Export via env var so call_runner._maybe_fault() can read it.
        if args.test_fault_point == "after_calls_row":
            os.environ["_CALL_RUNNER_FAULT_POINT"] = "after_calls_row"

    max_jobs = 1 if args.once else args.max_jobs  # --once implies --max-jobs 1
    jobs_processed = 0

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
            # Respect --max-jobs / --once limit.
            if max_jobs is not None and jobs_processed >= max_jobs:
                logger.info("call_worker: reached max-jobs=%d — exiting", max_jobs)
                break

            try:
                dispatched_in_batch = 0
                for _ in range(batch_size):
                    if _SHUTDOWN:
                        break
                    if max_jobs is not None and jobs_processed >= max_jobs:
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
                        jobs_processed += 1
                    else:
                        break  # no more jobs; stop this batch

                # Update heartbeat timestamp (last successful poll tick).
                CALL_WORKER_LAST_POLL.set_to_current_time()
                try:
                    db.execute(
                        """INSERT INTO service_heartbeats (service_name, instance_id, last_seen_at, status)
                           VALUES ('call_worker', %s, now(), 'ok')
                           ON CONFLICT (service_name, instance_id) DO UPDATE
                               SET last_seen_at = now(), status = 'ok'""",
                        (worker_id,),
                    )
                except Exception:
                    pass  # heartbeat failure is non-fatal

                if dispatched_in_batch == 0:
                    # In --once/--max-jobs mode with no job found, exit cleanly.
                    if max_jobs is not None:
                        logger.info("call_worker: no jobs available — exiting (--max-jobs mode)")
                        break
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
