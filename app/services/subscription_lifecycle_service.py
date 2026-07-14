# app/services/subscription_lifecycle_service.py
"""Subscription lifecycle transitions — all state changes go through here.

Design rules:
- All monetary calculations use Decimal.
- All dates timezone-aware UTC.
- Never hardcode plan names or prices — read from plans table.
- Existing active tenants never accidentally expired.
- All transitions emit subscription_events entries.
- Admin access never revoked regardless of tenant status.
- Extension ≠ Reactivation — these are separate explicit actions.
- Security suspensions are not bypassed by adding subscription days.
- Duplicate transitions are idempotent.
- Duplicate payments prevented by idempotency_key uniqueness.

State machine (tenant.status):
  trial       ──[subscribe]──────────────────► active
  trial       ──[trial_ends_at passed]────────► expired
  active      ──[payment missed]──────────────► grace_period
  active      ──[admin/policy]────────────────► suspended
  active      ──[cancel]──────────────────────► cancelled
  active      ──[policy]──────────────────────► expired
  grace_period ──[paid]──────────────────────► active
  grace_period ──[grace_ends_at passed]───────► past_due
  grace_period ──[admin/policy]───────────────► suspended
  grace_period ──[cancel]─────────────────────► cancelled
  past_due    ──[paid]──────────────────────► active
  past_due    ──[past_due_ends_at passed]──────► expired
  past_due    ──[admin]────────────────────────► suspended
  past_due    ──[cancel]───────────────────────► cancelled
  suspended   ──[admin explicit reactivate]────► active
  suspended   ──[cancel]───────────────────────► cancelled
  cancelled   ──[resubscribe]──────────────────► active
  expired     ──[resubscribe/pay]───────────────► active
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import os
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

# ── Canonical transition table ────────────────────────────────────────────
# Any transition not in this map is invalid and raises ValueError.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "trial":        {"active", "expired", "cancelled"},
    "active":       {"grace_period", "suspended", "cancelled", "expired"},
    "grace_period": {"active", "past_due", "suspended", "cancelled"},
    "past_due":     {"active", "suspended", "cancelled", "expired"},
    "suspended":    {"active", "cancelled"},
    "cancelled":    {"active"},
    "expired":      {"active"},
}

# States from which payment/renewal re-activates (excludes security-suspended)
_RENEWABLE_FROM = {"grace_period", "past_due", "expired", "cancelled", "trial"}

# Past-due auto-expiry window (configurable via env)
_PAST_DUE_DAYS_DEFAULT = 14


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(_UTC)


def _tz_safe(dt: datetime.datetime | None) -> datetime.datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_UTC)


def _compute_period_end(start: datetime.datetime, billing_cycle: str) -> datetime.datetime:
    """Return end of billing period. Calendar-aware, handles month/year edge days."""
    if billing_cycle == "yearly":
        new_year = start.year + 1
        new_day = min(start.day, _days_in_month(new_year, start.month))
        return start.replace(year=new_year, day=new_day)
    # monthly: +1 calendar month, clamped to last valid day
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
    """Raise ValueError on invalid transition. Idempotent: from==to is allowed."""
    if from_status == to_status:
        return  # idempotent no-op
    allowed = _VALID_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise ValueError(
            f"Invalid subscription transition: {from_status!r} → {to_status!r}. "
            f"Allowed from {from_status!r}: {sorted(allowed)}"
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


def _get_past_due_days() -> int:
    return int(os.environ.get("PAST_DUE_DAYS", str(_PAST_DUE_DAYS_DEFAULT)))


def _get_grace_days() -> int:
    return int(os.environ.get("GRACE_PERIOD_DAYS", "7"))


# ── Public lifecycle functions ─────────────────────────────────────────────

def start_trial(tenant_id: int, plan_id: int | None = None) -> None:
    """Stamp trial_started_at / trial_ends_at. Idempotent — safe to call repeatedly."""
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")
    if tenant["status"] not in ("trial",):
        return

    effective_plan_id = plan_id or tenant.get("plan_id")
    trial_days = 14
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
    """Activate a paid subscription from trial/expired/cancelled.
    Returns new subscription_id. Does NOT accept suspended tenants — use
    admin_reactivate_subscription() for those.
    """
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    if tenant["status"] == "suspended":
        raise ValueError(
            "Cannot activate subscription for a suspended tenant. "
            "Use admin_reactivate_subscription() instead."
        )

    from_status = tenant["status"]
    _assert_transition(from_status, "active")

    plan = _get_plan(plan_id)
    if plan.get("archived_at") is not None:
        raise ValueError(f"Plan {plan['name']!r} is archived and cannot be assigned to new subscriptions.")

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

    if payment_intent_id:
        price = Decimal(str(
            plan["price_yearly"]
            if billing_cycle == "yearly" and plan.get("price_yearly")
            else plan["price_monthly"]
        ))
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
    """active → grace_period when renewal payment missed. Idempotent."""
    tenant = get_tenant_by_id(tenant_id)
    if not tenant or tenant["status"] != "active":
        return

    grace_days = _get_grace_days()
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
    """grace_period → past_due. Sets past_due_ends_at for scheduler expiry. Idempotent."""
    tenant = get_tenant_by_id(tenant_id)
    if not tenant or tenant["status"] != "grace_period":
        return

    past_due_days = _get_past_due_days()
    now = _now_utc()
    past_due_ends_at = now + datetime.timedelta(days=past_due_days)

    sub = get_current_subscription(tenant_id)
    if sub:
        update_subscription(sub["id"], status="past_due", past_due_ends_at=past_due_ends_at)

    _update_tenant_status(tenant_id, "past_due")

    record_subscription_event(
        tenant_id=tenant_id,
        subscription_id=sub["id"] if sub else None,
        event_type="past_due",
        from_status="grace_period",
        to_status="past_due",
        actor_type=actor_type,
        metadata={"past_due_ends_at": past_due_ends_at.isoformat()},
    )

    _send_past_due_notification(tenant_id)


def expire_subscription(
    tenant_id: int,
    from_status: str | None = None,
    actor_type: str = "system",
) -> None:
    """trial/active/past_due → expired. Idempotent if already expired."""
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        return

    actual_from = from_status or tenant["status"]
    if tenant["status"] == "expired":
        return  # already expired — idempotent

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


def suspend_subscription(
    tenant_id: int,
    reason: str,
    actor_id: int,
    suspension_type: str = "security",
) -> None:
    """Admin security/compliance suspension. Separate from billing suspension.
    Does not affect payment status — billing state is preserved on the subscription.
    Requires explicit admin_reactivate_subscription() to lift.
    """
    if not reason or not reason.strip():
        raise ValueError("Suspension reason is required")

    valid_types = ("security", "billing", "compliance")
    if suspension_type not in valid_types:
        raise ValueError(f"suspension_type must be one of {valid_types}")

    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    if tenant["status"] == "suspended":
        # Already suspended — idempotent, just log
        record_subscription_event(
            tenant_id=tenant_id,
            event_type="suspend_duplicate",
            from_status="suspended",
            to_status="suspended",
            actor_type="admin",
            actor_id=actor_id,
            reason=reason,
            metadata={"suspension_type": suspension_type},
        )
        return

    _assert_transition(tenant["status"], "suspended")

    now = _now_utc()
    sub = get_current_subscription(tenant_id)
    if sub:
        update_subscription(
            sub["id"],
            suspended_at=now,
            suspension_reason=reason,
            suspension_type=suspension_type,
        )

    _update_tenant_status(tenant_id, "suspended")

    record_subscription_event(
        tenant_id=tenant_id,
        subscription_id=sub["id"] if sub else None,
        event_type="suspended",
        from_status=tenant["status"],
        to_status="suspended",
        actor_type="admin",
        actor_id=actor_id,
        reason=reason,
        metadata={"suspension_type": suspension_type},
    )


def admin_reactivate_subscription(
    tenant_id: int,
    reason: str,
    actor_id: int,
    extend_days: int = 0,
) -> None:
    """Explicit admin reactivation from suspended/cancelled/expired.
    This is the ONLY way to lift a security suspension.
    Payment status is NOT assumed — admin is overriding billing state.
    Optionally extends period by extend_days.
    """
    if not reason or not reason.strip():
        raise ValueError("Reactivation reason is required")

    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    from_status = tenant["status"]
    _assert_transition(from_status, "active")

    now = _now_utc()
    sub = get_current_subscription(tenant_id)

    sub_id: int | None = None
    if sub:
        new_end: datetime.datetime | None = None
        if extend_days > 0:
            current_end = _tz_safe(sub.get("current_period_end")) or now
            new_end = current_end + datetime.timedelta(days=extend_days)

        update_kwargs: dict = {
            "status": "active",
            "suspended_at": None,
            "suspension_reason": None,
            "suspension_type": "none",
        }
        if new_end:
            update_kwargs["current_period_end"] = new_end

        update_subscription(sub["id"], **update_kwargs)
        sub_id = sub["id"]
    else:
        # No subscription exists — create minimal one if tenant has a plan
        plan_id = tenant.get("plan_id")
        if not plan_id:
            raise ValueError("Tenant has no plan — assign a plan before reactivating")
        period_end = now + datetime.timedelta(days=max(extend_days, 30))
        sub_id = create_subscription(
            tenant_id=tenant_id,
            plan_id=plan_id,
            billing_cycle="monthly",
            status="active",
            started_at=now,
            current_period_start=now,
            current_period_end=period_end,
        )

    _update_tenant_status(tenant_id, "active", sub_id=sub_id)
    invalidate_tenant_caches(tenant_id)

    record_subscription_event(
        tenant_id=tenant_id,
        subscription_id=sub_id,
        event_type="subscription_reactivated",
        from_status=from_status,
        to_status="active",
        actor_type="admin",
        actor_id=actor_id,
        reason=reason,
        metadata={"extend_days": extend_days},
    )


def admin_extend_subscription(
    tenant_id: int,
    days: int,
    reason: str,
    actor_id: int,
) -> None:
    """Admin: extend current_period_end by N days ONLY.
    Does NOT change tenant status.
    Does NOT reactivate suspended/cancelled/expired tenants.
    Use admin_reactivate_subscription() for status changes.
    Emits subscription_extended event.
    """
    if days <= 0:
        raise ValueError("Extension days must be positive")
    if not reason or not reason.strip():
        raise ValueError("Extension reason is required")

    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    now = _now_utc()
    sub = get_current_subscription(tenant_id)

    if sub:
        current_end = _tz_safe(sub.get("current_period_end")) or now
        new_end = current_end + datetime.timedelta(days=days)
        update_subscription(sub["id"], current_period_end=new_end)

        record_subscription_event(
            tenant_id=tenant_id,
            subscription_id=sub["id"],
            event_type="subscription_extended",
            from_status=tenant["status"],
            to_status=tenant["status"],  # status unchanged
            actor_type="admin",
            actor_id=actor_id,
            reason=reason,
            metadata={
                "days_extended": days,
                "old_period_end": (sub.get("current_period_end") or "").isoformat()
                    if hasattr(sub.get("current_period_end"), "isoformat") else None,
                "new_period_end": new_end.isoformat(),
                "note": "Extension only. Tenant status unchanged.",
            },
        )
    else:
        # No subscription — cannot extend
        raise ValueError("No active subscription found to extend. Use reactivate instead.")

    invalidate_tenant_caches(tenant_id)


def cancel_subscription(
    tenant_id: int,
    immediate: bool = False,
    reason: str | None = None,
    actor_type: str = "customer",
    actor_id: int | None = None,
) -> None:
    """Cancel subscription immediately or at period end. Idempotent if already cancelled."""
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    if tenant["status"] == "cancelled":
        return  # already cancelled — idempotent

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
        # End-of-period: mark on subscription, status unchanged until scheduler runs
        update_subscription(sub["id"], cancel_at_period_end=True, cancellation_reason=reason)
        period_end_str = None
        if sub.get("current_period_end") and hasattr(sub["current_period_end"], "isoformat"):
            period_end_str = sub["current_period_end"].isoformat()
        record_subscription_event(
            tenant_id=tenant_id,
            subscription_id=sub["id"],
            event_type="cancel_scheduled",
            from_status=tenant["status"],
            to_status=tenant["status"],
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            metadata={"cancel_at_period_end": True, "period_end": period_end_str},
        )


def renew_subscription(
    tenant_id: int,
    payment_intent_id: int,
    actor_type: str = "customer",
    actor_id: int | None = None,
) -> int:
    """Re-activate from grace_period, past_due, expired, cancelled, or trial.
    Suspended tenants CANNOT self-renew — they need admin_reactivate_subscription().
    Returns subscription_id.
    Idempotent: duplicate payment_intent_id returns existing sub_id.
    """
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    from_status = tenant["status"]

    if from_status == "suspended":
        raise ValueError(
            "Suspended tenants cannot self-renew. "
            "Contact support — admin must explicitly reactivate the account."
        )

    if from_status == "active":
        # Already active — idempotent
        sub = get_current_subscription(tenant_id)
        return sub["id"] if sub else -1

    if from_status not in _RENEWABLE_FROM:
        raise ValueError(f"Renewal not permitted from status {from_status!r}")

    _assert_transition(from_status, "active")

    sub = get_current_subscription(tenant_id)
    plan_id = (sub["plan_id"] if sub else None) or tenant.get("plan_id")
    billing_cycle = (sub["billing_cycle"] if sub else None) or "monthly"

    if not plan_id:
        raise ValueError("No plan configured for renewal")

    # Idempotency: same payment_intent → same result
    ikey = f"renew:{tenant_id}:{payment_intent_id}"
    existing = get_subscription_payment_by_idempotency(ikey)
    if existing:
        logger.info(
            "renew_subscription: duplicate payment_intent %s for tenant %s — idempotent return",
            payment_intent_id, tenant_id,
        )
        if tenant["status"] != "active":
            _update_tenant_status(tenant_id, "active")
        return sub["id"] if sub else existing.get("subscription_id", -1)

    plan = _get_plan(plan_id)
    now = _now_utc()
    period_end = _compute_period_end(now, billing_cycle)

    if sub and sub["status"] in ("grace_period", "past_due"):
        renewal_count = (sub.get("renewal_count") or 0) + 1
        update_subscription(
            sub["id"],
            status="active",
            current_period_start=now,
            current_period_end=period_end,
            grace_ends_at=None,
            past_due_ends_at=None,
            renewal_count=renewal_count,
        )
        sub_id = sub["id"]
    else:
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

    price = Decimal(str(
        plan["price_yearly"]
        if billing_cycle == "yearly" and plan.get("price_yearly")
        else plan["price_monthly"]
    ))
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
        metadata={
            "plan_id": plan_id,
            "billing_cycle": billing_cycle,
            "period_end": period_end.isoformat(),
        },
    )

    _send_renewal_confirmation(tenant_id)
    return sub_id


# ── Helpers ────────────────────────────────────────────────────────────────

def _payment_idempotency_key(tenant_id: int, sub_id: int, period_start: datetime.datetime) -> str:
    raw = f"sub:{tenant_id}:{sub_id}:{period_start.strftime('%Y%m%d')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Notification stubs ─────────────────────────────────────────────────────

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
