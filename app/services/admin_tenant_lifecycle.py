"""Super-admin tenant lifecycle management: edit, archive, dependency check, safe delete & synthetic cleanup."""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from app.db import db
from app.middleware.audit import audit_log
from app.models.tenant import get_tenant_by_id, invalidate_tenant_caches
from app.models.user import get_user_by_id, verify_user_password

_SYNTHETIC_PATTERN = re.compile(
    r"(?:@test\.local$|@example\.(?:com|org|net)$|"
    r"(?:^|[._\s-])(?:e2e|proctest|synth|synthetic|qa|playwright|smoke|journey|dummy|test_tenant|qa_)\b)",
    re.I,
)

ALLOWED_TENANT_STATUSES = (
    "active",
    "trial",
    "suspended",
    "cancelled",
    "grace_period",
    "past_due",
    "expired",
)


def _parse_settings(raw_settings: Any) -> dict[str, Any]:
    if isinstance(raw_settings, dict):
        return dict(raw_settings)
    if isinstance(raw_settings, str) and raw_settings.strip():
        try:
            parsed = json.loads(raw_settings)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def is_archived_tenant(tenant: dict[str, Any] | None) -> bool:
    if not tenant:
        return False
    settings = _parse_settings(tenant.get("settings"))
    return bool(settings.get("archived_at") or settings.get("archived"))


def _verify_admin_password(actor_id: int, password_plaintext: str) -> None:
    if not (password_plaintext or "").strip():
        raise ValueError("Super-admin password confirmation is required.")
    admin_user = get_user_by_id(actor_id)
    if not admin_user or admin_user.get("role") != "super_admin":
        raise ValueError("Super-admin account not found.")
    if not verify_user_password(admin_user["password_hash"], password_plaintext):
        raise ValueError("Incorrect super-admin password confirmation.")


def inspect_tenant_dependencies(tenant_id: int) -> dict[str, Any] | None:
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        return None

    wallet_row = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tenant_id,))
    wallet_balance = Decimal(str(wallet_row["balance"])) if wallet_row and wallet_row.get("balance") is not None else Decimal("0")

    def _count(table: str) -> int:
        row = db.query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = %s", (tenant_id,))
        return int(row["n"]) if row else 0

    counts = {
        "users": _count("users"),
        "orders": _count("orders"),
        "calls": _count("calls"),
        "cdr_records": _count("cdr_records"),
        "wallet_transactions": _count("wallet_transactions"),
        "payment_intents": _count("payment_intents"),
        "subscription_payments": _count("subscription_payments"),
        "subscriptions": _count("subscriptions"),
        "flows": _count("flows"),
        "contacts": _count("contacts"),
        "api_keys": _count("api_keys"),
        "assigned_numbers": _count("customer_number_assignments"),
    }

    financial_reasons: list[str] = []
    if counts["wallet_transactions"] > 0:
        financial_reasons.append(f"{counts['wallet_transactions']} wallet transaction(s)")
    if counts["payment_intents"] > 0:
        financial_reasons.append(f"{counts['payment_intents']} payment intent(s)")
    if counts["subscription_payments"] > 0:
        financial_reasons.append(f"{counts['subscription_payments']} subscription payment(s)")
    if counts["cdr_records"] > 0:
        financial_reasons.append(f"{counts['cdr_records']} CDR billing record(s)")
    if wallet_balance != Decimal("0"):
        financial_reasons.append(f"non-zero wallet balance (৳{wallet_balance:.2f})")

    has_financial_history = bool(financial_reasons)
    delete_eligible = not has_financial_history
    block_reason = (
        "Cannot hard-delete tenant with financial or billing history ("
        + ", ".join(financial_reasons)
        + "). Archive the tenant instead to preserve ledger and audit integrity."
        if has_financial_history
        else ""
    )

    return {
        "tenant_id": tenant_id,
        "business_name": tenant["business_name"],
        "email": tenant["email"],
        "status": tenant["status"],
        "is_archived": is_archived_tenant(tenant),
        "wallet_balance": float(wallet_balance),
        "counts": counts,
        "has_financial_history": has_financial_history,
        "delete_eligible": delete_eligible,
        "block_reason": block_reason,
    }


def edit_tenant_profile(
    tenant_id: int,
    actor_id: int,
    business_name: str,
    email: str,
    plan_id: int | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError("Tenant not found.")

    clean_name = (business_name or "").strip()
    clean_email = (email or "").strip().lower()
    if not clean_name:
        raise ValueError("Business name is required.")
    if not clean_email or "@" not in clean_email or "." not in clean_email.split("@", 1)[1]:
        raise ValueError("A valid business email address is required.")

    dup = db.query_one(
        "SELECT id FROM tenants WHERE LOWER(email) = LOWER(%s) AND id <> %s LIMIT 1",
        (clean_email, tenant_id),
    )
    if dup:
        raise ValueError("Another merchant account already uses that email address.")

    if status and status not in ALLOWED_TENANT_STATUSES:
        raise ValueError("Invalid tenant status.")

    with db.transaction() as conn:
        updates = ["business_name = %s", "email = %s", "updated_at = now()"]
        params: list[Any] = [clean_name, clean_email]
        if plan_id is not None:
            updates.append("plan_id = %s")
            params.append(int(plan_id))
        if status:
            updates.append("status = %s")
            params.append(status)
        params.append(tenant_id)
        conn.execute(f"UPDATE tenants SET {', '.join(updates)} WHERE id = %s", tuple(params))

    invalidate_tenant_caches(tenant_id)
    try:
        from app.services.rate_service import invalidate_billing_policy_cache
        invalidate_billing_policy_cache(tenant_id)
    except Exception:
        pass

    audit_log(
        "tenant_profile_updated",
        actor_id,
        "tenant",
        tenant_id,
        {
            "old_business_name": tenant.get("business_name"),
            "new_business_name": clean_name,
            "old_email": tenant.get("email"),
            "new_email": clean_email,
            "plan_id": plan_id,
            "status": status,
        },
    )
    return {"tenant_id": tenant_id, "business_name": clean_name, "email": clean_email}


def archive_tenant(tenant_id: int, actor_id: int, reason: str = "") -> dict[str, Any]:
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError("Tenant not found.")
    clean_reason = (reason or "").strip() or "Archived by super admin"

    with db.transaction() as conn:
        row = conn.execute("SELECT settings FROM tenants WHERE id = %s FOR UPDATE", (tenant_id,)).fetchone()
        if not row:
            raise ValueError("Tenant not found.")
        settings = _parse_settings(row["settings"])
        settings["archived"] = True
        settings["archived_by"] = actor_id
        settings["archive_reason"] = clean_reason
        settings["archived_at"] = conn.execute("SELECT now() AS ts").fetchone()["ts"].isoformat()

        conn.execute(
            """
            UPDATE tenants
            SET status = 'cancelled',
                is_dialing_paused = true,
                settings = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (json.dumps(settings), tenant_id),
        )
        conn.execute(
            """
            UPDATE api_keys
            SET is_active = false, revoked_at = COALESCE(revoked_at, now())
            WHERE tenant_id = %s
            """,
            (tenant_id,),
        )
        conn.execute(
            """
            UPDATE subscriptions
            SET status = 'cancelled', cancel_at_period_end = false, updated_at = now()
            WHERE tenant_id = %s AND status IN ('active', 'trial', 'grace_period', 'past_due')
            """,
            (tenant_id,),
        )
        conn.execute("DELETE FROM sessions WHERE tenant_id = %s", (tenant_id,))
        released = conn.execute(
            "DELETE FROM customer_number_assignments WHERE tenant_id = %s RETURNING id",
            (tenant_id,),
        ).fetchall()

    invalidate_tenant_caches(tenant_id)
    audit_log(
        "tenant_archived",
        actor_id,
        "tenant",
        tenant_id,
        {"reason": clean_reason, "released_numbers": len(released or [])},
    )
    return {"tenant_id": tenant_id, "archived": True, "released_numbers": len(released or [])}


def _hard_delete_tenant_tx(conn, tenant_id: int, actor_id: int) -> None:
    user_rows = conn.execute("SELECT id FROM users WHERE tenant_id = %s", (tenant_id,)).fetchall()
    user_ids = [int(r["id"]) for r in (user_rows or []) if int(r["id"]) != actor_id]

    conn.execute("DELETE FROM customer_number_assignments WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM order_call_attempts WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM calls WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM orders WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM flows WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM contacts WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM retry_rules WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM api_keys WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM webhook_logs WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM customer_rates WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM tts_cache WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM sessions WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM wallets WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM audit_log WHERE target_tenant_id = %s", (tenant_id,))
    if user_ids:
        conn.execute(
            "DELETE FROM audit_log WHERE actor_user_id = ANY(%s) OR admin_user_id = ANY(%s)",
            (user_ids, user_ids),
        )
    conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))


def delete_tenant_safely(
    tenant_id: int,
    actor_id: int,
    admin_password: str,
    confirm_text: str,
) -> dict[str, Any]:
    _verify_admin_password(actor_id, admin_password)
    deps = inspect_tenant_dependencies(tenant_id)
    if not deps:
        raise ValueError("Tenant not found.")
    if not deps["delete_eligible"]:
        raise ValueError(deps["block_reason"])

    expected_tokens = {
        "DELETE",
        deps["business_name"].strip().upper(),
        deps["email"].strip().upper(),
    }
    if (confirm_text or "").strip().upper() not in expected_tokens:
        raise ValueError("Confirmation text did not match. Type DELETE or the merchant email to confirm.")

    with db.transaction() as conn:
        _hard_delete_tenant_tx(conn, tenant_id, actor_id)

    invalidate_tenant_caches(tenant_id)
    audit_log(
        "tenant_deleted",
        actor_id,
        "tenant_deleted",
        tenant_id,
        {
            "deleted_tenant_id": tenant_id,
            "business_name": deps["business_name"],
            "email": deps["email"],
            "counts": deps["counts"],
        },
    )
    return {
        "deleted": True,
        "tenant_id": tenant_id,
        "business_name": deps["business_name"],
        "email": deps["email"],
    }


def list_synthetic_cleanup_candidates(limit: int = 200) -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT
            t.id,
            t.business_name,
            t.email,
            t.status,
            t.created_at,
            COALESCE(w.balance, 0) AS wallet_balance,
            (SELECT COUNT(*) FROM wallet_transactions wt WHERE wt.tenant_id = t.id) AS wallet_tx_count,
            (SELECT COUNT(*) FROM payment_intents pi WHERE pi.tenant_id = t.id) AS payment_intent_count,
            (SELECT COUNT(*) FROM subscription_payments sp WHERE sp.tenant_id = t.id) AS sub_payment_count,
            (SELECT COUNT(*) FROM cdr_records cr WHERE cr.tenant_id = t.id) AS cdr_count,
            (SELECT COUNT(*) FROM calls c WHERE c.tenant_id = t.id) AS calls_count,
            (SELECT COUNT(*) FROM orders o WHERE o.tenant_id = t.id) AS orders_count
        FROM tenants t
        LEFT JOIN wallets w ON w.tenant_id = t.id
        ORDER BY t.created_at DESC
        LIMIT %s
        """,
        (max(1, min(limit, 500)),),
    )

    candidates: list[dict[str, Any]] = []
    for r in rows:
        name = (r.get("business_name") or "").strip()
        email = (r.get("email") or "").strip()
        matches_pattern = bool(_SYNTHETIC_PATTERN.search(email) or _SYNTHETIC_PATTERN.search(name))
        has_financial = (
            Decimal(str(r.get("wallet_balance") or 0)) != Decimal("0")
            or int(r.get("wallet_tx_count") or 0) > 0
            or int(r.get("payment_intent_count") or 0) > 0
            or int(r.get("sub_payment_count") or 0) > 0
            or int(r.get("cdr_count") or 0) > 0
        )
        has_traffic = int(r.get("calls_count") or 0) > 0 or int(r.get("orders_count") or 0) > 0
        if matches_pattern and not has_financial and not has_traffic:
            candidates.append(
                {
                    "id": int(r["id"]),
                    "business_name": name,
                    "email": email,
                    "status": r["status"],
                    "created_at": r["created_at"].strftime("%Y-%m-%d") if r.get("created_at") else "—",
                }
            )
    return candidates


def bulk_cleanup_synthetic_tenants(
    tenant_ids: list[int],
    actor_id: int,
    admin_password: str,
    confirm_text: str,
) -> dict[str, Any]:
    _verify_admin_password(actor_id, admin_password)
    if (confirm_text or "").strip().upper() != "DELETE":
        raise ValueError("Type DELETE to confirm bulk synthetic cleanup.")

    requested = {int(tid) for tid in (tenant_ids or []) if str(tid).isdigit()}
    if not requested:
        raise ValueError("Select at least one synthetic test merchant to delete.")

    eligible_map = {c["id"]: c for c in list_synthetic_cleanup_candidates(limit=500)}
    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for tid in sorted(requested):
        if tid not in eligible_map:
            skipped.append({"id": tid, "reason": "Not an empty synthetic test candidate"})
            continue
        deps = inspect_tenant_dependencies(tid)
        if not deps or not deps["delete_eligible"]:
            skipped.append({"id": tid, "reason": deps["block_reason"] if deps else "Not found"})
            continue
        with db.transaction() as conn:
            _hard_delete_tenant_tx(conn, tid, actor_id)
        invalidate_tenant_caches(tid)
        deleted.append({"id": tid, "business_name": deps["business_name"], "email": deps["email"]})

    if deleted:
        audit_log(
            "tenant_bulk_synthetic_cleanup",
            actor_id,
            "tenant_bulk",
            None,
            {"deleted_count": len(deleted), "deleted_tenants": deleted, "skipped": skipped},
        )
    return {"deleted": deleted, "skipped": skipped}
