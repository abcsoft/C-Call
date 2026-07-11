# E:\c-call-ivr\app\services\admin_service.py
"""Super admin platform stats + impersonation."""
from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from typing import Any

from flask import session

from app.db import db

logger = logging.getLogger("ccall")

IMPERSONATION_MAX_DURATION = datetime.timedelta(hours=1)


def get_platform_stats() -> dict[str, Any]:
    """Platform-wide KPIs for the admin dashboard."""
    from call_engine import call_runner

    counts = db.query_one(
        """
        SELECT
            (SELECT COUNT(*) FROM tenants) AS total_tenants,
            (SELECT COUNT(*) FROM tenants WHERE status = 'active') AS active_tenants,
            (SELECT COUNT(*) FROM tenants WHERE status = 'trial') AS trial_tenants,
            (SELECT COUNT(*) FROM calls WHERE started_at >= CURRENT_DATE) AS calls_today,
            (SELECT COUNT(*) FROM calls WHERE started_at >= date_trunc('month', CURRENT_DATE)) AS calls_mtd
        """
    ) or {}

    revenue_today_row = db.query_one(
        "SELECT COALESCE(SUM(amount), 0) AS revenue FROM wallet_transactions "
        "WHERE type = 'debit' AND created_at >= CURRENT_DATE"
    )
    revenue_mtd_row = db.query_one(
        "SELECT COALESCE(SUM(amount), 0) AS revenue FROM wallet_transactions "
        "WHERE type = 'debit' AND created_at >= date_trunc('month', CURRENT_DATE)"
    )

    top_by_volume = db.query(
        """
        SELECT t.id, t.business_name, COUNT(c.id) AS call_count
        FROM tenants t
        LEFT JOIN calls c ON c.tenant_id = t.id
        GROUP BY t.id, t.business_name
        ORDER BY call_count DESC
        LIMIT 5
        """
    )

    top_by_revenue = db.query(
        """
        SELECT t.id, t.business_name, COALESCE(SUM(wt.amount), 0) AS revenue
        FROM tenants t
        LEFT JOIN wallet_transactions wt ON wt.tenant_id = t.id AND wt.type = 'debit'
        GROUP BY t.id, t.business_name
        ORDER BY revenue DESC
        LIMIT 5
        """
    )

    return {
        "total_tenants": int(counts.get("total_tenants") or 0),
        "active_tenants": int(counts.get("active_tenants") or 0),
        "trial_tenants": int(counts.get("trial_tenants") or 0),
        "calls_today": int(counts.get("calls_today") or 0),
        "calls_mtd": int(counts.get("calls_mtd") or 0),
        "revenue_today": Decimal(str(revenue_today_row["revenue"] if revenue_today_row else 0)),
        "revenue_mtd": Decimal(str(revenue_mtd_row["revenue"] if revenue_mtd_row else 0)),
        "active_calls_now": len(call_runner.active_call_ids()),
        "top_tenants_by_volume": top_by_volume,
        "top_tenants_by_revenue": top_by_revenue,
    }


def get_low_balance_tenants(threshold: Decimal = Decimal("10")) -> list[dict[str, Any]]:
    """Tenants with wallet balance under `threshold` — for the dashboard alerts panel."""
    return db.query(
        """
        SELECT t.id, t.business_name, t.email, w.balance
        FROM tenants t
        JOIN wallets w ON w.tenant_id = t.id
        WHERE w.balance < %s AND t.status = 'active'
        ORDER BY w.balance ASC
        """,
        (threshold,),
    )


def get_failed_webhooks_last_hour() -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM webhook_logs WHERE success = false AND sent_at >= now() - interval '1 hour'"
    )
    return int(row["n"]) if row else 0


def get_sip_provider_status() -> list[dict[str, Any]]:
    """SIP providers with a green/red status flag for the dashboard."""
    providers = db.query("SELECT id, name, sip_domain, status FROM sip_providers ORDER BY name ASC")
    for p in providers:
        p["healthy"] = p["status"] == "active"
    return providers


def _log_audit(admin_user_id: int, action: str, target_tenant_id: int | None, reason: str | None, metadata: dict | None = None) -> None:
    from app.middleware.audit import audit_log
    details = dict(metadata or {})
    if reason:
        details["reason"] = reason
    audit_log(action, admin_user_id, "tenant", target_tenant_id, details)


def impersonate_tenant(admin_user_id: int, tenant_id: int, reason: str) -> dict[str, Any]:
    """Start impersonating a tenant's admin user. Logs the action to
    audit_log and stashes the original admin's identity + an expiry in the
    Flask session so end_impersonation() can restore it and so the
    impersonation auto-expires after IMPERSONATION_MAX_DURATION."""
    if not reason or not reason.strip():
        raise ValueError("A reason is required to impersonate a tenant.")

    from app.models.user import get_users_by_tenant
    users = get_users_by_tenant(tenant_id)
    target_user = next((u for u in users if u["role"] == "admin"), None)
    if not target_user:
        raise ValueError("No admin user found for this tenant to impersonate.")

    expires_at = datetime.datetime.now(datetime.timezone.utc) + IMPERSONATION_MAX_DURATION

    session["_impersonator_user_id"] = admin_user_id
    session["_impersonation_target_tenant_id"] = tenant_id
    session["_impersonation_expires_at"] = expires_at.isoformat()

    _log_audit(admin_user_id, "impersonate_start", tenant_id, reason, {"target_user_id": target_user["id"]})
    logger.warning(
        "Admin user %s started impersonating tenant %s (user %s) — reason: %s",
        admin_user_id, tenant_id, target_user["id"], reason,
    )

    return {
        "target_user": target_user,
        "expires_at": expires_at,
    }


def is_impersonation_active() -> bool:
    expires_at_str = session.get("_impersonation_expires_at")
    if not expires_at_str:
        return False
    expires_at = datetime.datetime.fromisoformat(expires_at_str)
    if datetime.datetime.now(datetime.timezone.utc) >= expires_at:
        return False
    return True


def end_impersonation(session_id: str | None = None) -> int | None:
    """Restore the original admin's identity. Returns the admin user_id to
    log back in as, or None if there was no active impersonation."""
    admin_user_id = session.pop("_impersonator_user_id", None)
    tenant_id = session.pop("_impersonation_target_tenant_id", None)
    session.pop("_impersonation_expires_at", None)

    if admin_user_id:
        _log_audit(admin_user_id, "impersonate_end", tenant_id, None)
        logger.info("Admin user %s ended impersonation of tenant %s", admin_user_id, tenant_id)

    return admin_user_id


def create_tenant_manual(business_name: str, email: str, password: str, plan_name: str = "free", initial_credit: Decimal = Decimal("0")) -> int:
    """Admin-created tenant: creates the tenant + wallet + default retry
    rules (via create_tenant), an admin login user, and an optional initial
    wallet credit."""
    from app.models.tenant import create_tenant
    from app.models.user import create_user
    from app.services.wallet_service import credit as credit_wallet

    business_name = (business_name or "").strip()
    email = (email or "").strip().lower()
    if not business_name or not email:
        raise ValueError("Business name and email are required.")

    tenant_id = create_tenant({
        "business_name": business_name,
        "email": email,
        "plan_name": plan_name,
        "status": "active",
    })
    create_user(tenant_id, email=email, password_plaintext=password, role="admin", email_verified=True)

    if initial_credit and Decimal(str(initial_credit)) > 0:
        credit_wallet(tenant_id, Decimal(str(initial_credit)), "Initial credit (admin-created tenant)")

    return tenant_id
