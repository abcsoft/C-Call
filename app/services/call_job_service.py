"""app/services/call_job_service.py — central call job queue service.

All call_jobs INSERT, UPDATE and query logic lives here.

Job lifecycle
-------------
queued     → waiting for a worker (available_at <= now())
leased     → exclusively claimed by one worker
retry_wait → dispatch failed temporarily; available_at set to a future time
completed  → dispatch accepted; call_id set; queue work done
failed     → dispatch permanently failed after max_attempts
cancelled  → job no longer needed (order terminal, duplicate, etc.)

Job completion ≠ call completion.
The telephony result lives in: calls, order_call_attempts, orders.
"""
from __future__ import annotations

import datetime
import logging
import random
from dataclasses import dataclass
from typing import Optional

from app.db import db

logger = logging.getLogger("ccall.call_job_service")

_UTC = datetime.timezone.utc

# Bounded backoff delays for dispatch failures (seconds).
# Index 0 = first failure; index N = Nth failure onward.
DISPATCH_BACKOFF_SECONDS: list[int] = [15, 60, 300, 900]

DEFAULT_MAX_DISPATCH_ATTEMPTS = 5


@dataclass
class DispatchResult:
    accepted: bool                # True when call startup was accepted
    call_id: Optional[int]        # calls.id — set only when accepted=True
    attempt_id: Optional[int]     # order_call_attempts.id — set only when accepted=True
    retryable: bool               # True when failure is temporary
    error_code: str               # machine-readable slug
    safe_message: str             # log-safe; no PII, no raw exception text


# ── Enqueue ───────────────────────────────────────────────────────────────────

def enqueue_order_call(
    conn,
    order_id: int,
    tenant_id: int,
    attempt_no: int,
    available_at: Optional[datetime.datetime] = None,
    max_attempts: int = DEFAULT_MAX_DISPATCH_ATTEMPTS,
    priority: int = 0,
) -> dict:
    """Insert an order-call job idempotently. Returns the job row dict.

    Must be called inside an existing transaction — accepts the caller's
    psycopg connection so order creation and job insertion commit atomically.

    If a non-terminal job already exists for (order_id, attempt_no), the
    existing row is returned unchanged (idempotent re-enqueue).

    Never dispatches directly. The worker reads the queue and dispatches.
    """
    if available_at is None:
        available_at = datetime.datetime.now(_UTC)

    # Idempotency: return existing active job without modification.
    existing = conn.execute(
        """SELECT id, order_id, tenant_id, attempt_no, job_status, available_at
           FROM call_jobs
           WHERE order_id = %s AND attempt_no = %s
             AND job_status NOT IN ('completed', 'failed', 'cancelled')""",
        (order_id, attempt_no),
    ).fetchone()
    def _row_to_dict(r):
        if isinstance(r, dict):
            return {k: r[k] for k in ("id", "order_id", "tenant_id", "attempt_no", "job_status", "available_at")}
        # psycopg3 default tuple-based Row
        return {"id": r[0], "order_id": r[1], "tenant_id": r[2],
                "attempt_no": r[3], "job_status": r[4], "available_at": r[5]}

    if existing:
        d = _row_to_dict(existing)
        logger.info(
            "enqueue_order_call: existing job returned  job_id=%s order_id=%s attempt=%s status=%s",
            d["id"], order_id, attempt_no, d["job_status"],
        )
        return d

    row = conn.execute(
        """INSERT INTO call_jobs
               (order_id, tenant_id, attempt_no, priority, available_at,
                max_attempts, job_status, job_type, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, 'queued', 'order_call', now())
           RETURNING id, order_id, tenant_id, attempt_no, job_status, available_at""",
        (order_id, tenant_id, attempt_no, priority, available_at, max_attempts),
    ).fetchone()

    d = _row_to_dict(row)
    logger.info(
        "enqueue_order_call: queued  job_id=%s order_id=%s tenant_id=%s attempt=%s available_at=%s",
        d["id"], order_id, tenant_id, attempt_no, available_at,
    )
    return d


# ── Status transitions ────────────────────────────────────────────────────────

def mark_dispatched(
    conn,
    job_id: int,
    call_id: int,
    attempt_id: Optional[int] = None,
) -> None:
    """Mark job completed after call startup was accepted.

    Stores call_id and order_call_attempt_id as the durable duplicate guard —
    if this worker crashes before the UPDATE reaches the DB and another worker
    recovers the lease, it will find the attempt row already created and know
    that a call already exists for this job.
    """
    conn.execute(
        """UPDATE call_jobs
           SET job_status            = 'completed',
               call_id               = %s,
               order_call_attempt_id = COALESCE(%s, order_call_attempt_id),
               dispatched_at         = now(),
               completed_at          = now(),
               updated_at            = now(),
               leased_by             = NULL,
               leased_at             = NULL,
               lease_expires_at      = NULL
           WHERE id = %s""",
        (call_id, attempt_id, job_id),
    )


def mark_retry_wait(
    conn,
    job_id: int,
    attempts: int,
    max_attempts: int,
    error_code: str,
    safe_message: str,
) -> None:
    """Schedule a retry after a temporary dispatch failure.

    Backoff schedule (DISPATCH_BACKOFF_SECONDS) with 25% jitter:
      attempt 1: ~15 s
      attempt 2: ~60 s
      attempt 3: ~5 min
      attempt 4+: ~15 min
    """
    idx = min(attempts - 1, len(DISPATCH_BACKOFF_SECONDS) - 1)
    base = DISPATCH_BACKOFF_SECONDS[idx]
    jitter = random.uniform(0, base * 0.25)
    available_at = datetime.datetime.now(_UTC) + datetime.timedelta(seconds=base + jitter)

    conn.execute(
        """UPDATE call_jobs
           SET job_status         = 'retry_wait',
               attempts           = %s,
               max_attempts       = %s,
               last_error_code    = %s,
               last_error_summary = %s,
               available_at       = %s,
               updated_at         = now(),
               leased_by          = NULL,
               leased_at          = NULL,
               lease_expires_at   = NULL
           WHERE id = %s""",
        (attempts, max_attempts, error_code, safe_message[:500], available_at, job_id),
    )
    logger.info(
        "mark_retry_wait: job_id=%s attempts=%s/%s code=%s available_at=%s",
        job_id, attempts, max_attempts, error_code, available_at.isoformat(),
    )


def mark_failed(conn, job_id: int, error_code: str, safe_message: str) -> None:
    """Permanently fail a job after exhausting dispatch attempts or hitting a
    permanent error condition. Caller is responsible for transitioning the
    order to a safe non-CALL_QUEUED status."""
    conn.execute(
        """UPDATE call_jobs
           SET job_status         = 'failed',
               last_error_code    = %s,
               last_error_summary = %s,
               completed_at       = now(),
               updated_at         = now(),
               leased_by          = NULL,
               leased_at          = NULL,
               lease_expires_at   = NULL
           WHERE id = %s""",
        (error_code, safe_message[:500], job_id),
    )
    logger.warning(
        "mark_failed: job_id=%s code=%s msg=%s",
        job_id, error_code, safe_message[:120],
    )


def mark_cancelled(conn, job_id: int) -> None:
    """Cancel a job (e.g. order already terminal, duplicate detected)."""
    conn.execute(
        """UPDATE call_jobs
           SET job_status    = 'cancelled',
               completed_at  = now(),
               updated_at    = now(),
               leased_by     = NULL,
               leased_at     = NULL,
               lease_expires_at = NULL
           WHERE id = %s""",
        (job_id,),
    )


# ── Lease management ──────────────────────────────────────────────────────────

def release_expired_leases(conn) -> int:
    """Reset leased jobs whose lease_expires_at has passed back to 'queued'.

    This is the crash-recovery mechanism: if a worker dies after leasing
    a job but before completing it, the lease expires and any live worker
    can claim it on the next poll. Returns the count of recovered leases.
    """
    result = conn.execute(
        """UPDATE call_jobs
           SET job_status       = 'queued',
               leased_by        = NULL,
               leased_at        = NULL,
               lease_expires_at = NULL,
               updated_at       = now()
           WHERE job_status = 'leased'
             AND lease_expires_at < now()""",
    )
    count = result.rowcount
    if count > 0:
        logger.info("release_expired_leases: recovered %d expired lease(s)", count)
    return count


def cancel_jobs_for_order(conn, order_id: int, tenant_id: int) -> int:
    """Cancel all non-terminal jobs for an order.

    Called when an order is manually cancelled or enters a terminal status
    that makes any pending dispatch meaningless.
    """
    result = conn.execute(
        """UPDATE call_jobs
           SET job_status    = 'cancelled',
               completed_at  = now(),
               updated_at    = now()
           WHERE order_id  = %s
             AND tenant_id = %s
             AND job_status NOT IN ('completed', 'failed', 'cancelled')""",
        (order_id, tenant_id),
    )
    return result.rowcount


# ── Metrics / health ──────────────────────────────────────────────────────────

def get_queue_metrics() -> dict:
    """Return per-status job counts for health endpoints and Prometheus."""
    try:
        rows = db.query(
            """SELECT job_status, COUNT(*) AS n
               FROM call_jobs
               WHERE job_status NOT IN ('completed', 'failed', 'cancelled')
               GROUP BY job_status"""
        )
        counts = {r["job_status"]: int(r["n"]) for r in rows}
        return {
            "queued":     counts.get("queued", 0),
            "leased":     counts.get("leased", 0),
            "retry_wait": counts.get("retry_wait", 0),
        }
    except Exception as exc:
        logger.warning("get_queue_metrics: %s", exc)
        return {}


def get_stale_lease_count() -> int:
    """Count leased jobs whose lease has already expired (health signal)."""
    try:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM call_jobs WHERE job_status='leased' AND lease_expires_at < now()"
        )
        return int(row["n"]) if row else 0
    except Exception:
        return 0
