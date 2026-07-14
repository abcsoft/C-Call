# app/services/subscription_access_service.py
"""Subscription enforcement: single source of truth for what a tenant can do.

All enforcement gates call `check_feature_access(tenant_id, feature)` and
inspect the returned `SubscriptionAccess` namedtuple.  Do NOT inline status
checks in route handlers — keep all logic here so the enforcement matrix is
auditable in one place.

Enforcement matrix
──────────────────
feature          trial  active  grace_period  past_due  suspended  cancelled  expired
dashboard        allow  allow   warn          warn      deny       deny       warn
order_intake     allow  allow   allow         deny      deny       deny       deny
queue_call       allow  allow   allow         deny      deny       deny       deny
process_retry    allow  allow   allow         deny      deny       deny       deny
tts_generate     allow  allow   allow         deny      deny       deny       deny
api_access       allow  allow   allow         deny_402  deny_403   deny_403   deny_402
wallet_deduct    allow  allow   allow         allow     deny       deny       deny
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.db import db

logger = logging.getLogger("ccall")

# Canonical allowed tenant statuses per feature:
_MATRIX: dict[str, dict[str, str]] = {
    #                 trial     active    grace_period  past_due   suspended  cancelled  expired
    "dashboard":     {"trial": "warn_trial", "active": "allow", "grace_period": "warn",
                      "past_due": "warn", "suspended": "deny", "cancelled": "deny", "expired": "warn"},
    "order_intake":  {"trial": "allow", "active": "allow", "grace_period": "allow",
                      "past_due": "deny_402", "suspended": "deny_402", "cancelled": "deny_402", "expired": "deny_402"},
    "queue_call":    {"trial": "allow", "active": "allow", "grace_period": "allow",
                      "past_due": "deny", "suspended": "deny", "cancelled": "deny", "expired": "deny"},
    "process_retry": {"trial": "allow", "active": "allow", "grace_period": "allow",
                      "past_due": "deny", "suspended": "deny", "cancelled": "deny", "expired": "deny"},
    "tts_generate":  {"trial": "allow", "active": "allow", "grace_period": "allow",
                      "past_due": "deny", "suspended": "deny", "cancelled": "deny", "expired": "deny"},
    "api_access":    {"trial": "allow", "active": "allow", "grace_period": "allow",
                      "past_due": "deny_402", "suspended": "deny_403", "cancelled": "deny_403", "expired": "deny_402"},
    "wallet_deduct": {"trial": "allow", "active": "allow", "grace_period": "allow",
                      "past_due": "allow", "suspended": "deny", "cancelled": "deny", "expired": "deny"},
}

_DENY_OUTCOMES = {"deny", "deny_402", "deny_403"}

_STATUS_MESSAGES: dict[str, str] = {
    "trial": "Account is in trial period.",
    "active": "Subscription is active.",
    "grace_period": "Payment overdue — account in grace period. Please renew to avoid service interruption.",
    "past_due": "Subscription past due. Service is suspended until payment is received.",
    "suspended": "Account has been suspended. Contact support.",
    "cancelled": "Subscription has been cancelled.",
    "expired": "Trial or subscription has expired. Please subscribe to continue.",
}

_HTTP_CODES: dict[str, int] = {
    "deny_402": 402,
    "deny_403": 403,
    "deny": 403,
}


@dataclass(frozen=True)
class SubscriptionAccess:
    allowed: bool
    outcome: str           # allow | warn | warn_trial | deny | deny_402 | deny_403
    tenant_status: str
    feature: str
    message: str
    http_status: int       # 200 when allowed, else 402/403


def check_feature_access(tenant_id: int, feature: str) -> SubscriptionAccess:
    """Return SubscriptionAccess for (tenant, feature) — never raises."""
    if feature not in _MATRIX:
        # Unknown feature → allow by default (forward compatible)
        logger.warning("check_feature_access: unknown feature '%s'", feature)
        return SubscriptionAccess(
            allowed=True, outcome="allow", tenant_status="unknown",
            feature=feature, message="Unknown feature — allow by default.", http_status=200,
        )

    row = db.query_one("SELECT status FROM tenants WHERE id = %s", (tenant_id,))
    if not row:
        return SubscriptionAccess(
            allowed=False, outcome="deny_403", tenant_status="not_found",
            feature=feature, message="Tenant not found.", http_status=403,
        )

    status = row["status"]
    feature_map = _MATRIX[feature]
    outcome = feature_map.get(status, "deny")

    allowed = outcome not in _DENY_OUTCOMES
    http_status = 200 if allowed else _HTTP_CODES.get(outcome, 403)
    message = _STATUS_MESSAGES.get(status, "Account status unknown.")

    return SubscriptionAccess(
        allowed=allowed,
        outcome=outcome,
        tenant_status=status,
        feature=feature,
        message=message,
        http_status=http_status,
    )


def is_subscription_active_for_calls(tenant_id: int) -> bool:
    """Quick boolean check for call dispatch paths — avoids full dataclass overhead."""
    access = check_feature_access(tenant_id, "queue_call")
    return access.allowed


def get_tenant_status_banner(tenant_id: int) -> dict | None:
    """Return banner info for the customer dashboard, or None if no banner needed."""
    row = db.query_one(
        """
        SELECT t.status, t.trial_ends_at, s.current_period_end, s.grace_ends_at
        FROM tenants t
        LEFT JOIN subscriptions s ON s.id = t.current_subscription_id
        WHERE t.id = %s
        """,
        (tenant_id,),
    )
    if not row:
        return None

    status = row["status"]
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)

    if status == "trial":
        ends = row.get("trial_ends_at")
        if ends:
            days_left = (ends.replace(tzinfo=datetime.timezone.utc) if ends.tzinfo is None else ends) - now
            days_left_int = max(0, days_left.days)
            if days_left_int <= 3:
                return {"type": "warning", "message": f"Trial ends in {days_left_int} day(s). Subscribe to keep your service."}
        return {"type": "info", "message": "You are on a free trial."}

    elif status == "grace_period":
        ends = row.get("grace_ends_at")
        if ends:
            days_left = (ends.replace(tzinfo=datetime.timezone.utc) if ends.tzinfo is None else ends) - now
            days_left_int = max(0, days_left.days)
            return {"type": "warning", "message": f"Payment overdue — grace period ends in {days_left_int} day(s). Renew now to avoid suspension."}
        return {"type": "warning", "message": "Payment overdue — please renew your subscription."}

    elif status == "past_due":
        return {"type": "danger", "message": "Subscription past due. New orders and calls are paused. Please renew."}

    elif status == "expired":
        return {"type": "danger", "message": "Trial or subscription has expired. Subscribe to re-activate service."}

    return None
