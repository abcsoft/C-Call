# E:\c-call-ivr\app\services\retry_service.py
"""Retry engine — schedules and processes order re-call attempts.

Called from:
  - app/scheduler.py: process_due_retries() (every 30s), via the
    PostgreSQL advisory lock so only one Flask worker/process runs it.
  - app/services/order_service.py: schedule_retry() after a call result
    comes back as no_answer/busy/failed.
  - customer panel "Cancel order": cancel_pending_retries().
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

import pytz

from app.db import db
from app.models.order import (
    get_due_retry_orders,
    get_order_by_id_system,
    increment_order_attempts,
    set_order_next_retry,
    update_order_status_system,
)

logger = logging.getLogger("ccall")

DEFAULT_TZ = pytz.timezone("Asia/Dhaka")
ADVISORY_LOCK_KEY = 12345


def _apply_dnd(
    next_retry_at: datetime.datetime,
    dnd_start: datetime.time,
    dnd_end: datetime.time,
    tz: datetime.tzinfo = DEFAULT_TZ,
) -> datetime.datetime:
    """If next_retry_at falls inside the tenant's DND window, push it to
    dnd_end (+5 min buffer) in the tenant's local timezone. Handles
    overnight windows (e.g. 21:00 -> 09:00) where dnd_start > dnd_end.
    Returns a UTC-aware datetime (same tzinfo convention as the input)."""
    local_time = next_retry_at.astimezone(tz)
    current_time = local_time.time()

    if dnd_start > dnd_end:
        # Overnight window: in-DND if at/after start, OR before end next morning.
        in_dnd = current_time >= dnd_start or current_time < dnd_end
    else:
        in_dnd = dnd_start <= current_time < dnd_end

    if not in_dnd:
        return next_retry_at

    wake_date = local_time.date()
    if dnd_start > dnd_end and current_time >= dnd_start:
        # Evening side of an overnight window — DND ends tomorrow morning.
        wake_date += datetime.timedelta(days=1)
    # else: already past midnight, before dnd_end (or same-day window) — DND ends later today.

    wake_time = tz.localize(datetime.datetime.combine(wake_date, dnd_end)) + datetime.timedelta(minutes=5)
    return wake_time.astimezone(datetime.timezone.utc)


def adjust_for_dnd(
    next_retry_at: datetime.datetime,
    dnd_start: datetime.time | str,
    dnd_end: datetime.time | str,
    tz: datetime.tzinfo = DEFAULT_TZ,
) -> datetime.datetime:
    """Wrapper around _apply_dnd to support string or datetime.time parameters."""
    if isinstance(dnd_start, str):
        h, m = map(int, dnd_start.split(":"))
        dnd_start = datetime.time(h, m)
    if isinstance(dnd_end, str):
        h, m = map(int, dnd_end.split(":"))
        dnd_end = datetime.time(h, m)
    return _apply_dnd(next_retry_at, dnd_start, dnd_end, tz)


def schedule_retry(
    order_id: int,
    tenant_id: int,
    current_attempt: int | None = None,
    attempt_no: int | None = None,
    call_result: str | None = None,
) -> bool:
    """Schedule the next retry attempt after current_attempt failed/went
    unanswered. Returns True on success, or False if the order was marked
    UNREACHABLE instead.

    Each attempt's own delay/retry_on/DND config — the "retryRules array"
    from the Retry & DND Settings API/UI — lives one row per attempt_no
    in the retry_rules table (saved by save_retry_rules(), each row's
    delay_minutes already converted from the UI's {delayValue, delayUnit}
    pair at save time). We look up exactly one row here: the rule for
    whichever attempt comes next, since that's the only one relevant to
    *this* scheduling decision.

    call_result: the raw outcome of the attempt that just ended (e.g.
    "no_answer", "busy") — checked against the next attempt's
    retry_rules.retry_on array (requirement: "check whether retry is
    allowed for that attempt" before scheduling one). If the result isn't
    in that list, the order goes straight to UNREACHABLE instead of being
    rescheduled, even if attempts remain. Pass None to skip this filter
    (back-compat for any caller that doesn't have a call_result to give,
    e.g. the scheduler's own re-dispatch path).

    Duplicate-schedule safety: this function only flips orders.order_status
    to RETRY_SCHEDULED — it never creates a standalone "job" row. The
    actual dispatch dedup happens in process_due_retries() below, via an
    atomic claim (UPDATE ... WHERE order_status = 'RETRY_SCHEDULED') before
    the call is placed — see that function's docstring. So even if
    schedule_retry() were somehow invoked twice for the same order (e.g. a
    retried webhook handler calling update_status_from_call() again), the
    second call just overwrites next_retry_at with the same/later time;
    it can't cause two calls to actually go out, because there's only one
    "due" row to claim per order and the claim is atomic.
    """
    order = get_order_by_id_system(order_id)
    if not order:
        logger.warning("schedule_retry: order %s not found", order_id)
        return False

    # Which attempt are we scheduling? Prefer an explicit attempt_no (the
    # scheduler's re-dispatch path knows this already), else derive it
    # from current_attempt + 1, else fall back to the order's own counter.
    if attempt_no is not None:
        next_attempt = attempt_no
    elif current_attempt is not None:
        next_attempt = current_attempt + 1
    else:
        next_attempt = (order.get("attempt_count") or 0) + 1

    # Pull this one attempt's config — delay, retry_on conditions, and
    # this tenant's DND window — from the retryRules row for next_attempt.
    rule = db.query_one(
        "SELECT * FROM retry_rules WHERE tenant_id = %s AND attempt_no = %s",
        (tenant_id, next_attempt),
    )

    no_rule = rule is None
    rule_disabled = bool(rule) and not rule["is_enabled"]
    # orders.max_attempts is the per-order cap (snapshotted at order-creation time
    # from the tenant's retry settings). The rule row also carries max_attempts but
    # orders.max_attempts is the authoritative limit so that changing retry settings
    # mid-flight doesn't retroactively shorten or extend in-progress orders.
    max_attempts = int(order.get("max_attempts") or 3)
    # Requirement: stop retrying once max attempts are exhausted.
    attempts_exhausted = next_attempt > max_attempts
    # Requirement: only retry on the outcomes this attempt is configured
    # for (e.g. attempt 2 might be no_answer-only, skipping busy results).
    result_not_retryable = (
        call_result is not None and rule is not None and call_result not in (rule["retry_on"] or [])
    )

    if no_rule or rule_disabled or attempts_exhausted or result_not_retryable:
        _mark_unreachable(order, tenant_id)
        return False

    # Requirement: next retry time = previous call time (now) + this
    # attempt's delayValue/delayUnit (already normalized to delay_minutes).
    delay_minutes = int(rule["delay_minutes"])
    dnd_start: datetime.time = rule["dnd_start"]
    dnd_end: datetime.time = rule["dnd_end"]

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    next_retry_at = now_utc + datetime.timedelta(minutes=delay_minutes)
    # Requirement: if that lands inside DND (including overnight windows
    # like 22:00->08:00), push it to the first valid time after DND ends.
    next_retry_at = _apply_dnd(next_retry_at, dnd_start, dnd_end, DEFAULT_TZ)

    db.execute(
        "UPDATE orders SET order_status = 'RETRY_SCHEDULED', next_retry_at = %s, updated_at = now() "
        "WHERE tenant_id = %s AND id = %s",
        (next_retry_at, tenant_id, order_id),
    )

    logger.info("Order %s retry %s scheduled at %s", order_id, next_attempt, next_retry_at.isoformat())
    return True


def _mark_unreachable(order: dict[str, Any], tenant_id: int) -> None:
    order_id = order["id"]
    db.execute(
        "UPDATE orders SET order_status = 'UNREACHABLE', next_retry_at = NULL, updated_at = now() "
        "WHERE tenant_id = %s AND id = %s",
        (tenant_id, order_id),
    )

    from app.services.webhook_service import deliver_async
    deliver_async(order_id)

    try:
        from app.models.tenant import get_tenant_by_id
        from app.services.email_service import send_order_unreachable_alert
        tenant = get_tenant_by_id(tenant_id)
        if tenant:
            send_order_unreachable_alert(tenant, order)
            
            # SMS Fallback Trigger
            import json
            import threading
            settings = tenant.get("settings") or {}
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except Exception:
                    settings = {}
            if settings.get("enable_sms_fallback"):
                from app.services.sms_service import send_order_fallback_sms
                threading.Thread(target=send_order_fallback_sms, args=(order_id, tenant_id), daemon=True).start()
    except Exception:
        logger.warning("Failed to send unreachable alert or trigger SMS fallback for order %s", order_id, exc_info=True)

    logger.info("Order %s marked UNREACHABLE.", order_id)


def process_due_retries() -> int:
    """Find orders due for a retry call (RETRY_SCHEDULED, next_retry_at <=
    now()), claim each by flipping it to CALL_QUEUED (prevents double-
    processing if this runs concurrently), and dispatch the call. Capped
    at 50 per run. Returns the number of orders processed.

    This is also wrapped in a PostgreSQL advisory lock by app/scheduler.py
    so only one Flask worker/process runs it at a time — but that lock
    alone isn't a complete guarantee (e.g. the lock holder could itself
    be re-entered if the job fires again before the previous run finished,
    or a stale lock from a crashed process). The atomic claim below is the
    real dedup mechanism: requirement 7 ("avoid duplicate retry jobs for
    the same call record") is enforced by actually checking the claim
    succeeded, not just assuming it did.
    """
    due_orders = get_due_retry_orders(limit=50)
    if not due_orders:
        return 0

    from app.services.call_job_service import enqueue_order_call

    processed = 0
    for order in due_orders:
        order_id  = order["id"]
        tenant_id = order["tenant_id"]

        # Subscription enforcement: skip tenants that cannot place calls.
        from app.services.subscription_access_service import check_feature_access
        sub_access = check_feature_access(tenant_id, "process_retry")
        if not sub_access.allowed:
            logger.info(
                "Skipping retry for order %s: tenant %s subscription blocked (%s)",
                order_id, tenant_id, sub_access.outcome,
            )
            continue

        # Derive the next attempt number from the order's current counter.
        next_attempt_no = (order.get("attempt_count") or 0) + 1

        # Atomically claim the order AND insert the durable job in one transaction.
        # If the UPDATE matches zero rows (another scheduler already claimed it),
        # the enqueue never happens and the transaction rolls back cleanly.
        try:
            with db.transaction() as conn:
                claimed = conn.execute(
                    "UPDATE orders SET order_status = 'CALL_QUEUED', updated_at = now() "
                    "WHERE id = %s AND order_status = 'RETRY_SCHEDULED' RETURNING id",
                    (order_id,),
                ).fetchone()

                if claimed is None:
                    logger.info(
                        "Order %s already claimed by another run — skipping duplicate enqueue.",
                        order_id,
                    )
                    # Raise to roll back the (empty) transaction — no changes were made.
                    raise _SkipOrder()

                # Insert durable job — idempotent if already queued for this attempt.
                enqueue_order_call(
                    conn,
                    order_id=order_id,
                    tenant_id=tenant_id,
                    attempt_no=next_attempt_no,
                )
                # Increment attempt counter only after job is safely committed.
                conn.execute(
                    "UPDATE orders SET attempt_count = attempt_count + 1, updated_at = now() "
                    "WHERE id = %s AND tenant_id = %s",
                    (order_id, tenant_id),
                )

            logger.info(
                "Retry job enqueued for order %s (tenant %s, attempt %s).",
                order_id, tenant_id, next_attempt_no,
            )
            processed += 1

        except _SkipOrder:
            pass  # already claimed elsewhere — normal, not an error
        except Exception:
            logger.error(
                "Failed to enqueue retry call job for order %s", order_id, exc_info=True
            )
            # The transaction rolled back — order status remains RETRY_SCHEDULED,
            # so the next scheduler tick will pick it up again.

    return processed


class _SkipOrder(Exception):
    """Internal sentinel — raised to abort a transaction without logging an error."""


def cancel_pending_retries(order_id: int, tenant_id: int) -> None:
    """Cancel any scheduled retry for an order — e.g. merchant manually
    cancels it from the panel. tenant_id is required to prevent cross-tenant
    cancellation (an order_id alone is not a tenant-scoped identifier)."""
    db.execute(
        "UPDATE orders SET order_status = 'CANCELLED', next_retry_at = NULL, updated_at = now()"
        " WHERE id = %s AND tenant_id = %s",
        (order_id, tenant_id),
    )


def get_retry_rules(tenant_id: int) -> list[dict[str, Any]]:
    """All retry rules for a tenant, ordered by attempt number."""
    return db.query(
        "SELECT * FROM retry_rules WHERE tenant_id = %s ORDER BY attempt_no ASC",
        (tenant_id,),
    )


def save_retry_rules(tenant_id: int, rules_list: list[dict[str, Any]]) -> None:
    """Upsert all retry rules for a tenant in a single transaction.

    rules_list: [{attempt_no, delay_minutes, retry_on, dnd_start, dnd_end,
                  max_attempts, is_enabled}, ...]
    """
    with db.transaction() as conn:
        if rules_list:
            max_attempts = rules_list[0].get("max_attempts", 3)
            conn.execute(
                "DELETE FROM retry_rules WHERE tenant_id = %s AND attempt_no > %s",
                (tenant_id, max_attempts),
            )
        for rule in rules_list:
            conn.execute(
                """
                INSERT INTO retry_rules
                    (tenant_id, attempt_no, delay_minutes, retry_on, dnd_start, dnd_end, max_attempts, is_enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, attempt_no) DO UPDATE SET
                    delay_minutes = EXCLUDED.delay_minutes,
                    retry_on      = EXCLUDED.retry_on,
                    dnd_start     = EXCLUDED.dnd_start,
                    dnd_end       = EXCLUDED.dnd_end,
                    max_attempts  = EXCLUDED.max_attempts,
                    is_enabled    = EXCLUDED.is_enabled
                """,
                (
                    tenant_id,
                    rule["attempt_no"],
                    rule.get("delay_minutes", 30),
                    rule.get("retry_on", ["no_answer", "busy"]),
                    rule.get("dnd_start", datetime.time(21, 0)),
                    rule.get("dnd_end", datetime.time(9, 0)),
                    rule.get("max_attempts", 3),
                    rule.get("is_enabled", True),
                ),
            )
