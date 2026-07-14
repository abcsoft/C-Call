# app/services/subscription_lifecycle_service.py
"""Subscription lifecycle transitions — all state changes go through here.

Rules:
- All monetary calculations use Decimal.
- All dates are timezone-aware (UTC).
- Never hardcode plan names or prices — read from plans table.
- Existing active tenants are never accidentally expired (backfill sets safe defaults).
- All transitions emit subscription_events entries.
- Admin access is never revoked regardless of tenant status.
- Duplicate renewal charges are prevented by idempotency_key on subscription_payments.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
from decimal import Decimal

from app.db import db
from app.models.subscription import (
    create_subscription,
    get_current_subscription,
    record_subscription_event,
    update_subscription,
    create_subscription_payment,
    get_subscription_payment_by_idempotency,
)
from app.models.tenant import get_tenant_by_id, invalidate_tenant_caches

logger = logging.getLogger("ccall")
_UTC = datetime.timezone.utc

# Valid transitions: from_status → allowed to_statuses
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "trial":        {"active", "expired", "cancelled"},
    "active":       {"grace_period", "cancelled", "expired"},
    "grace_period": {"active", "past_due", "cancelled"},
    "past_due":     {"active", "suspended", "cancelled"},
    "suspended":    {"active", "cancelled"},
    "cancelled":    {"active"},
    "expired":      {"active"},
}


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(_UTC)


def _compute_period_end(start: datetime.datetime, billing_cycle: str) -> datetime.datetime:
    """Return end of billing period from start."""
    if billing_cycle == "yearly":
        new_year = start.year + 1
        new_day = min(start.day, _days_in_month(new_year, start.month))
        return start.replace(year=new_year, day=new_day)
    # monthly: +1 calendar month
    month = start.month + 1
    year = start.year
    if month > 12:
        month = 1
        year += 1
    day = min(start.day, _days_in_month(year, month))
    return start.replace(year=year, month=month, day=day)


def _days_in_month(year: int, month: int) -> int:
    import calendar
    return calendar.monthrange(year, month)[1]


def _get_plan(plan_id: int) -> dict:
    row = db.query_one("SELECT * FROM plans WHERE id = %s", (plan_id,))
    if not row:
        raise ValueError(f"Plan {plan_id} not found")
    return row


def _assert_transition(from_status: str, to_status: str) -> None:
    allowed = _VALID_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise ValueError(
            f"Invalid subscription transition: {from_status!r} → {to_status!r}. "
            f"Allowed from {from_status!r}: {allowed}"
        )


def _update_tenant_status(tenant_id: int, new_status: str, sub_id: int | None = None) -> None:
    if sub_id is not None:
        db.execute(
            "UPDATE tenants SET status = %s, current_subscription_id = %s, updated_at = now() WHERE id = %s",
            (new_status, sub_id, tenant_id),
        )
    else:
        db.execute(
            "UPDATE tenants SET status = %s, updated_at = now() WHERE id = %s",
            (new_status, tenant_id),
        )
    invalidate_tenant_caches(tenant_id)


def start_trial(tenant_id: int, plan_id: int | None = None) -> None:
    """Stamp trial_started_at / trial_ends_at when a tenant first signs up.
    Called from create_tenant flow — safe to call on 'trial' tenants only.
    """
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")
    if tenant["status"] not in ("trial",):
        # Only valid on trial tenants (or not-yet-activated)
        return

    effective_plan_id = plan_id or tenant.get("plan_id")
    trial_days = 14  # default
    if effective_plan_id:
        plan = _get_plan(effective_plan_id)
        trial_days = plan.get("trial_days") or 14

    now = _now_utc()
    ends_at = now + datetime.timedelta(days=trial_days)

    db.execute(
        """
        UPDATE tenants
        SET trial_started_at = COALESCE(trial_started_at, %s),
            trial_ends_at = COALESCE(trial_ends_at, %s),
            updated_at = now()
        WHERE id = %s
        """,
        (now, ends_at, tenant_id),
    )
    record_subscription_event(
        tenant_id=tenant_id,
        event_type="trial_started",
        from_status=None,
        to_status="trial",
        actor_type="system",
        metadata={"trial_days": trial_days, "trial_ends_at": ends_at.isoformat()},
    )
    invalidate_tenant_caches(tenant_id)


def activate_subscription(
    tenant_id: int,
    plan_id: int,
    billing_cycle: str,
    payment_intent_id: int | None = None,
    actor_type: str = "customer",
    actor_id: int | None = None,
) -> int:
    """Activate a paid subscription (from trial, expired, cancelled, or renewal).
    Returns new subscription_id.
    """
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    from_status = tenant["status"]
    _assert_transition(from_status, "active")

    plan = _get_plan(plan_id)
    now = _now_utc()
    period_end = _compute_period_end(now, billing_cycle)

    # Close any previous open subscription
    prev = get_current_subscription(tenant_id)
    if prev and prev["status"] not in ("cancelled", "expired"):
        update_subscription(prev["id"], status="cancelled", cancelled_at=now)
        record_subscription_event(
            tenant_id=tenant_id,
            subscription_id=prev["id"],
            event_type="cancelled",
            from_status=prev["status"],
            to_status="cancelled",
            actor_type=actor_type,
            actor_id=actor_id,
            reason="Superseded by new subscription",
        )

    sub_id = create_subscription(
        tenant_id=tenant_id,
        plan_id=plan_id,
        billing_cycle=billing_cycle,
        status="active",
        started_at=now,
        current_period_start=now,
        current_period_end=period_end,
    )

    _update_tenant_status(tenant_id, "active", sub_id=sub_id)
    # Also update plan_id on tenant
    db.execute("UPDATE tenants SET plan_id = %s WHERE id = %s", (plan_id, tenant_id))
    invalidate_tenant_caches(tenant_id)

    record_subscription_event(
        tenant_id=tenant_id,
        subscription_id=sub_id,
        event_type="activated",
        from_status=from_status,
        to_status="active",
        actor_type=actor_type,
        actor_id=actor_id,
        metadata={
            "plan_id": plan_id,
            "plan_name": plan["name"],
            "billing_cycle": billing_cycle,
            "period_end": period_end.isoformat(),
        },
    )

    # Record payment if provided
    if payment_intent_id:
        price = Decimal(str(plan["price_yearly"] if billing_cycle == "yearly" and plan.get("price_yearly") else plan["price_monthly"]))
        ikey = _payment_idempotency_key(tenant_id, sub_id, now)
        if not get_subscription_payment_by_idempotency(ikey):
            create_subscription_payment(
                subscription_id=sub_id,
                tenant_id=tenant_id,
                amount=price,
                billing_period_start=now,
                billing_period_end=period_end,
                idempotency_key=ikey,
                payment_intent_id=payment_intent_id,
                status="completed",
            )

    return sub_id


def enter_grace_period(tenant_id: int, actor_type: str = "system") -> None:
    """Transition active → grace_period when renewal payment missed."""
    tenant = get_tenant_by_id(tenant_id)
    if not tenant or tenant["status"] != "active":
        return

    from app.config import config as _cfg
    import os
    grace_days = int(os.environ.get("GRACE_PERIOD_DAYS", "7"))
    now = _now_utc()
    grace_ends_at = now + datetime.timedelta(days=grace_days)

    sub = get_current_subscription(tenant_id)
    if sub:
        update_subscription(sub["id"], status="grace_period", grace_ends_at=grace_ends_at)

    _update_tenant_status(tenant_id, "grace_period")

    record_subscription_event(
        tenant_id=tenant_id,
        subscription_id=sub["id"] if sub else None,
        event_type="grace_period_started",
        from_status="active",
        to_status="grace_period",
        actor_type=actor_type,
        metadata={"grace_ends_at": grace_ends_at.isoformat()},
    )

    _send_grace_period_notification(tenant_id, grace_ends_at)


def enter_past_due(tenant_id: int, actor_type: str = "system") -> None:
    """Transition grace_period → past_due when grace window expires."""
    tenant = get_tenant_by_id(tenant_id)
    if not tenant or tenant["status"] != "grace_period":
        return

    sub = get_current_subscription(tenant_id)
    if sub:
        update_subscription(sub["id"], status="past_due")

    _update_tenant_status(tenant_id, "past_due")

    record_subscription_event(
        tenant_id=tenant_id,
        subscription_id=sub["id"] if sub else None,
        event_type="past_due",
        from_status="grace_period",
        to_status="past_due",
        actor_type=actor_type,
    )

    _send_past_due_notification(tenant_id)


def expire_subscription(tenant_id: int, from_status: str | None = None, actor_type: str = "system") -> None:
    """Transition trial/active → expired."""
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        return

    actual_from = from_status or tenant["status"]
    _assert_transition(actual_from, "expired")

    sub = get_current_subscription(tenant_id)
    if sub:
        update_subscription(sub["id"], status="expired")

    _update_tenant_status(tenant_id, "expired")

    record_subscription_event(
        tenant_id=tenant_id,
        subscription_id=sub["id"] if sub else None,
        event_type="expired",
        from_status=actual_from,
        to_status="expired",
        actor_type=actor_type,
    )

    _send_expired_notification(tenant_id)


def cancel_subscription(
    tenant_id: int,
    immediate: bool = False,
    reason: str | None = None,
    actor_type: str = "customer",
    actor_id: int | None = None,
) -> None:
    """Cancel subscription — immediately or at period end."""
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    _assert_transition(tenant["status"], "cancelled")

    sub = get_current_subscription(tenant_id)
    now = _now_utc()

    if immediate or not sub:
        if sub:
            update_subscription(sub["id"], status="cancelled", cancelled_at=now, cancellation_reason=reason)
        _update_tenant_status(tenant_id, "cancelled")
        record_subscription_event(
            tenant_id=tenant_id,
            subscription_id=sub["id"] if sub else None,
            event_type="cancelled",
            from_status=tenant["status"],
            to_status="cancelled",
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
        )
    else:
        # End-of-period cancel
        update_subscription(sub["id"], cancel_at_period_end=True, cancellation_reason=reason)
        record_subscription_event(
            tenant_id=tenant_id,
            subscription_id=sub["id"],
            event_type="cancel_scheduled",
            from_status=tenant["status"],
            to_status=tenant["status"],
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            metadata={"cancel_at_period_end": True, "period_end": sub.get("current_period_end", "").isoformat() if sub.get("current_period_end") else None},
        )


def renew_subscription(
    tenant_id: int,
    payment_intent_id: int,
    actor_type: str = "customer",
    actor_id: int | None = None,
) -> int:
    """Re-activate from grace_period, past_due, expired, or cancelled.
    Returns new or updated subscription_id.
    """
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    from_status = tenant["status"]
    if from_status == "active":
        raise ValueError("Subscription already active")

    _assert_transition(from_status, "active")

    sub = get_current_subscription(tenant_id)
    plan_id = (sub["plan_id"] if sub else None) or tenant.get("plan_id")
    billing_cycle = (sub["billing_cycle"] if sub else None) or "monthly"

    if not plan_id:
        raise ValueError("No plan configured for renewal")

    plan = _get_plan(plan_id)
    now = _now_utc()

    # Idempotency: prevent duplicate charges within same payment_intent
    ikey = f"renew:{tenant_id}:{payment_intent_id}"
    if get_subscription_payment_by_idempotency(ikey):
        logger.warning("renew_subscription: duplicate renewal attempt for tenant %s intent %s", tenant_id, payment_intent_id)
        # Still re-activate if somehow not active
        if tenant["status"] != "active":
            _update_tenant_status(tenant_id, "active")
        return sub["id"] if sub else -1

    period_end = _compute_period_end(now, billing_cycle)

    if sub and sub["status"] in ("grace_period", "past_due"):
        # Update existing subscription
        renewal_count = (sub.get("renewal_count") or 0) + 1
        update_subscription(
            sub["id"],
            status="active",
            current_period_start=now,
            current_period_end=period_end,
            grace_ends_at=None,
            renewal_count=renewal_count,
        )
        sub_id = sub["id"]
    else:
        # Create fresh subscription
        sub_id = create_subscription(
            tenant_id=tenant_id,
            plan_id=plan_id,
            billing_cycle=billing_cycle,
            status="active",
            started_at=now,
            current_period_start=now,
            current_period_end=period_end,
        )

    _update_tenant_status(tenant_id, "active", sub_id=sub_id)
    db.execute("UPDATE tenants SET plan_id = %s WHERE id = %s", (plan_id, tenant_id))
    invalidate_tenant_caches(tenant_id)

    price = Decimal(str(plan["price_yearly"] if billing_cycle == "yearly" and plan.get("price_yearly") else plan["price_monthly"]))
    create_subscription_payment(
        subscription_id=sub_id,
        tenant_id=tenant_id,
        amount=price,
        billing_period_start=now,
        billing_period_end=period_end,
        idempotency_key=ikey,
        payment_intent_id=payment_intent_id,
        status="completed",
    )

    record_subscription_event(
        tenant_id=tenant_id,
        subscription_id=sub_id,
        event_type="renewed",
        from_status=from_status,
        to_status="active",
        actor_type=actor_type,
        actor_id=actor_id,
        metadata={"plan_id": plan_id, "billing_cycle": billing_cycle, "period_end": period_end.isoformat()},
    )

    _send_renewal_confirmation(tenant_id)
    return sub_id


def admin_extend_subscription(
    tenant_id: int,
    days: int,
    reason: str,
    actor_id: int,
) -> None:
    """Admin: extend current subscription period end by N days."""
    if days <= 0:
        raise ValueError("Extension days must be positive")

    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    sub = get_current_subscription(tenant_id)
    now = _now_utc()

    if sub:
        current_end = sub.get("current_period_end") or now
        if current_end.tzinfo is None:
            current_end = current_end.replace(tzinfo=_UTC)
        new_end = current_end + datetime.timedelta(days=days)
        update_subscription(sub["id"], current_period_end=new_end)

        # If tenant was expired/past_due/grace, re-activate
        if tenant["status"] in ("expired", "past_due", "grace_period"):
            _update_tenant_status(tenant_id, "active", sub_id=sub["id"])
            from_status = tenant["status"]
        else:
            from_status = tenant["status"]

        record_subscription_event(
            tenant_id=tenant_id,
            subscription_id=sub["id"],
            event_type="admin_extended",
            from_status=from_status,
            to_status=tenant["status"] if tenant["status"] != from_status else "active",
            actor_type="admin",
            actor_id=actor_id,
            reason=reason,
            metadata={"days_extended": days, "new_period_end": new_end.isoformat()},
        )
    else:
        # No subscription: create one
        plan_id = tenant.get("plan_id")
        if not plan_id:
            raise ValueError("Tenant has no plan — cannot extend")
        sub_id = create_subscription(
            tenant_id=tenant_id,
            plan_id=plan_id,
            billing_cycle="monthly",
            status="active",
            started_at=now,
            current_period_start=now,
            current_period_end=now + datetime.timedelta(days=days),
        )
        _update_tenant_status(tenant_id, "active", sub_id=sub_id)
        record_subscription_event(
            tenant_id=tenant_id,
            subscription_id=sub_id,
            event_type="admin_extended",
            from_status=tenant["status"],
            to_status="active",
            actor_type="admin",
            actor_id=actor_id,
            reason=reason,
            metadata={"days_extended": days},
        )

    invalidate_tenant_caches(tenant_id)


def _payment_idempotency_key(tenant_id: int, sub_id: int, period_start: datetime.datetime) -> str:
    raw = f"sub:{tenant_id}:{sub_id}:{period_start.strftime('%Y%m%d')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Notification stubs ─────────────────────────────────────────────────────
# Each sends an email via email_service; failures are logged, never raised.

def _send_grace_period_notification(tenant_id: int, grace_ends_at: datetime.datetime) -> None:
    try:
        from app.services.email_service import send_grace_period_email
        send_grace_period_email(tenant_id, grace_ends_at)
    except Exception as exc:
        logger.warning("Grace period email failed for tenant %s: %s", tenant_id, exc)


def _send_past_due_notification(tenant_id: int) -> None:
    try:
        from app.services.email_service import send_past_due_email
        send_past_due_email(tenant_id)
    except Exception as exc:
        logger.warning("Past-due email failed for tenant %s: %s", tenant_id, exc)


def _send_expired_notification(tenant_id: int) -> None:
    try:
        from app.services.email_service import send_subscription_expired_email
        send_subscription_expired_email(tenant_id)
    except Exception as exc:
        logger.warning("Expired email failed for tenant %s: %s", tenant_id, exc)


def _send_renewal_confirmation(tenant_id: int) -> None:
    try:
        from app.services.email_service import send_renewal_confirmation_email
        send_renewal_confirmation_email(tenant_id)
    except Exception as exc:
        logger.warning("Renewal confirmation email failed for tenant %s: %s", tenant_id, exc)
