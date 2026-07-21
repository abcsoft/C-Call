from __future__ import annotations

import logging
import secrets
from decimal import Decimal, InvalidOperation

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user

from app.blueprints.admin import admin_bp
from app.blueprints.auth.decorators import require_super_admin
from app.db import db
from app.models.tenant import get_all_tenants, get_tenant_by_id
from app.models.user import User, get_user_by_id, get_users_by_tenant
from app.models.wallet import credit_balance, deduct_balance, get_wallet
from app.services.admin_service import (
    create_tenant_manual,
    end_impersonation,
    get_failed_webhooks_last_hour,
    get_low_balance_tenants,
    get_platform_stats,
    get_sip_provider_status,
    impersonate_tenant,
    is_impersonation_active,
)

logger = logging.getLogger("ccall")


@admin_bp.route("/dashboard", methods=["GET"])
@require_super_admin
def dashboard():
    stats = get_platform_stats()
    low_balance_tenants = get_low_balance_tenants()
    failed_webhooks_count = get_failed_webhooks_last_hour()
    sip_providers = get_sip_provider_status()

    daily_revenue = db.query(
        """
        SELECT DATE(created_at) AS day, COALESCE(SUM(amount), 0) AS revenue
        FROM wallet_transactions
        WHERE type = 'debit' AND created_at >= CURRENT_DATE - INTERVAL '30 days'
        GROUP BY DATE(created_at)
        ORDER BY day ASC
        """
    )

    no_number_tenants = db.query(
        """
        SELECT id, business_name FROM tenants
        WHERE status = 'active' AND id NOT IN (
            SELECT DISTINCT tenant_id FROM customer_number_assignments
        )
        """
    )

    return render_template(
        "admin/dashboard.html",
        stats=stats,
        low_balance_tenants=low_balance_tenants,
        failed_webhooks_count=failed_webhooks_count,
        sip_providers=sip_providers,
        daily_revenue=daily_revenue,
        no_number_tenants=no_number_tenants,
    )


@admin_bp.route("/customers", methods=["GET", "POST"])
@require_super_admin
def customers_list():
    if request.method == "POST":
        try:
            business_name = request.form.get("business_name", "").strip()
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            plan_name = request.form.get("plan_name", "free")
            initial_credit = Decimal(request.form.get("initial_credit", "0") or "0")

            if not business_name or not email or len(password) < 8:
                flash("Business name, email, and an 8+ character password are required.", "error")
                return redirect(url_for("admin.customers_list"))

            tenant_id = create_tenant_manual(business_name, email, password, plan_name, initial_credit)
            flash(f"Tenant '{business_name}' created (id {tenant_id}).", "success")
        except (InvalidOperation, ValueError) as exc:
            flash(f"Failed to create tenant: {exc}", "error")
        except Exception as exc:
            flash(f"Failed to create tenant: {exc}", "error")
        return redirect(url_for("admin.customers_list"))

    status_filter = request.args.get("status")
    plan_filter = request.args.get("plan_id")
    search = request.args.get("search")

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    result = get_all_tenants(page=page, per_page=20, search=search)
    tenants = result["data"] if isinstance(result, dict) else result
    total = result["total"] if isinstance(result, dict) else len(tenants)

    if status_filter:
        tenants = [t for t in tenants if t["status"] == status_filter]
    if plan_filter:
        tenants = [t for t in tenants if str(t.get("plan_id")) == plan_filter]

    for tenant in tenants:
        wallet = get_wallet(tenant["id"])
        tenant["balance"] = Decimal(str(wallet["balance"])) if wallet else Decimal("0.0")
        calls_mtd = db.query_one(
            "SELECT COUNT(*) AS n FROM calls WHERE tenant_id = %s AND started_at >= date_trunc('month', CURRENT_DATE)",
            (tenant["id"],),
        )
        tenant["calls_mtd"] = calls_mtd["n"] if calls_mtd else 0

    plans = db.query("SELECT * FROM plans ORDER BY id ASC")

    return render_template(
        "admin/customers/list.html",
        tenants=tenants,
        total=total,
        page=page,
        status_filter=status_filter,
        plan_filter=plan_filter,
        search=search,
        plans=plans,
    )


def _update_tenant_fields(tenant_id: int, form) -> None:
    from app.middleware.audit import audit_log

    plan_id = form.get("plan_id")
    status = form.get("status")

    if plan_id:
        old_plan = db.query_one("SELECT plan_id FROM tenants WHERE id = %s", (tenant_id,))
        new_plan_id = int(plan_id)
        db.execute("UPDATE tenants SET plan_id = %s, updated_at = now() WHERE id = %s", (new_plan_id, tenant_id))
        audit_log("tenant_plan_change", current_user.id, "tenant", tenant_id,
                  {"old_plan_id": old_plan["plan_id"] if old_plan else None, "new_plan_id": new_plan_id})
    if status:
        db.execute("UPDATE tenants SET status = %s, updated_at = now() WHERE id = %s", (status, tenant_id))
        audit_log("tenant_status_change", current_user.id, "tenant", tenant_id, {"new_status": status})
    
    if "plan_id" in form or "rate_per_min" in form or "billing_pulse_seconds" in form:
        # rate_per_min: blank = NULL = inherit from plan/platform
        rate_per_min = form.get("rate_per_min", "").strip()
        rate_dec: Decimal | None = Decimal(rate_per_min) if rate_per_min else None

        billing_pulse_seconds = form.get("billing_pulse_seconds", "").strip()
        pulse_val: int | None = int(billing_pulse_seconds) if billing_pulse_seconds else None

        minimum_billable_seconds = form.get("minimum_billable_seconds", "").strip()
        min_bill_val: int | None = int(minimum_billable_seconds) if minimum_billable_seconds else None

        # charge_failed_calls tri-state: "" → NULL (inherit), "1" → True, "0" → False
        cfc_raw = form.get("charge_failed_calls", "")
        if cfc_raw == "1":
            charge_failed_calls: bool | None = True
        elif cfc_raw == "0":
            charge_failed_calls = False
        else:
            charge_failed_calls = None

        db.execute(
            """
            INSERT INTO customer_rates (tenant_id, rate_per_min, billing_pulse_seconds, minimum_billable_seconds, charge_failed_calls)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                rate_per_min = EXCLUDED.rate_per_min,
                billing_pulse_seconds = EXCLUDED.billing_pulse_seconds,
                minimum_billable_seconds = EXCLUDED.minimum_billable_seconds,
                charge_failed_calls = EXCLUDED.charge_failed_calls,
                updated_at = now()
            """,
            (tenant_id, rate_dec, pulse_val, min_bill_val, charge_failed_calls),
        )
    
    from app.models.tenant import invalidate_tenant_caches
    invalidate_tenant_caches(tenant_id)
    from app.services.rate_service import invalidate_billing_policy_cache
    invalidate_billing_policy_cache(tenant_id)


@admin_bp.route("/customers/<int:tenant_id>", methods=["GET", "POST", "PUT"])
@require_super_admin
def customer_detail(tenant_id: int):
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        flash("Tenant not found.", "error")
        return redirect(url_for("admin.customers_list"))

    if request.method in ("POST", "PUT"):
        try:
            _update_tenant_fields(tenant_id, request.form or request.json or {})
            if request.method == "PUT":
                return jsonify({"success": True})
            flash("Tenant details updated successfully.", "success")
        except Exception as exc:
            if request.method == "PUT":
                return jsonify({"success": False, "error": str(exc)}), 400
            flash(f"Update failed: {exc}", "error")
        return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))

    wallet = get_wallet(tenant_id) or {}
    if wallet and "balance" in wallet:
        wallet = dict(wallet)
        wallet["balance"] = Decimal(str(wallet["balance"]))
        if wallet.get("low_balance_threshold"):
            wallet["low_balance_threshold"] = Decimal(str(wallet["low_balance_threshold"]))
    users = get_users_by_tenant(tenant_id)
    transactions = db.query(
        "SELECT * FROM wallet_transactions WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 10",
        (tenant_id,),
    )
    plans = db.query("SELECT * FROM plans ORDER BY id ASC")
    rate = db.query_one("SELECT * FROM customer_rates WHERE tenant_id = %s", (tenant_id,))

    usage = db.query_one(
        """
        SELECT COUNT(*) AS calls, 
               COALESCE(SUM(talk_seconds), 0) AS talk_seconds,
               COALESCE(SUM(billable_seconds), 0) AS billable_seconds
        FROM calls WHERE tenant_id = %s AND started_at >= date_trunc('month', CURRENT_DATE)
        """,
        (tenant_id,),
    ) or {"calls": 0, "talk_seconds": 0, "billable_seconds": 0}
    revenue_mtd = db.query_one(
        "SELECT COALESCE(SUM(amount), 0) AS revenue FROM wallet_transactions "
        "WHERE tenant_id = %s AND type = 'debit' AND created_at >= date_trunc('month', CURRENT_DATE)",
        (tenant_id,),
    )
    usage["revenue"] = revenue_mtd["revenue"] if revenue_mtd else 0
    usage["minutes"] = int(Decimal(str(usage.get("billable_seconds") or 0)) / 60)

    recent_orders = db.query(
        "SELECT * FROM orders WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 20",
        (tenant_id,),
    )

    has_assigned_numbers_query = db.query_one(
        "SELECT COUNT(*) AS count FROM customer_number_assignments WHERE tenant_id = %s",
        (tenant_id,)
    )
    has_assigned_numbers = int(has_assigned_numbers_query["count"]) > 0 if has_assigned_numbers_query else False

    from app.models.plan_tts_policy import get_plan_tts_policy
    tts_policy = get_plan_tts_policy(tenant.get("plan_id")) if tenant.get("plan_id") else None

    return render_template(
        "admin/customers/detail.html",
        tenant=tenant,
        wallet=wallet,
        users=users,
        transactions=transactions,
        plans=plans,
        rate=rate,
        usage=usage,
        recent_orders=recent_orders,
        has_assigned_numbers=has_assigned_numbers,
        tts_policy=tts_policy,
    )


@admin_bp.route("/customers/<int:tenant_id>/wallet/adjust", methods=["POST"])
@require_super_admin
def wallet_adjust(tenant_id: int):
    """Credit or debit tenant wallet from admin panel. Reason is required."""
    action = request.form.get("action")  # 'credit' or 'debit'
    reason = request.form.get("description", "").strip()
    try:
        amount = Decimal(request.form.get("amount", "0"))
        if amount <= 0:
            flash("Amount must be greater than zero.", "error")
            return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))
        if not reason:
            flash("A reason is required for manual wallet adjustments.", "error")
            return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))

        from app.middleware.audit import audit_log

        if action == "credit":
            credit_balance(tenant_id, amount, reason, ref_id=f"adj_{secrets.token_hex(4)}")
            audit_log("wallet_credit", current_user.id, "tenant", tenant_id, {"amount": str(amount), "reason": reason})
            flash(f"Successfully credited ৳{amount:.2f} to tenant wallet.", "success")
        elif action == "debit":
            deduct_balance(tenant_id, amount, reason, ref_id=f"adj_{secrets.token_hex(4)}")
            audit_log("wallet_debit", current_user.id, "tenant", tenant_id, {"amount": str(amount), "reason": reason})
            flash(f"Successfully debited ৳{amount:.2f} from tenant wallet.", "success")
        else:
            flash("Invalid wallet action.", "error")
    except Exception as exc:
        flash(f"Adjustment failed: {exc}", "error")

    return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))


@admin_bp.route("/customers/<int:tenant_id>/impersonate", methods=["POST"])
@require_super_admin
def impersonate(tenant_id: int):
    """Impersonate a tenant's admin user to troubleshoot issues. Requires
    a reason; expires automatically after 1 hour (see admin_service)."""
    reason = request.form.get("reason", "").strip()
    try:
        result = impersonate_tenant(current_user.id, tenant_id, reason)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))

    login_user(User(result["target_user"]))
    flash(f"Now impersonating {result['target_user']['email']} (expires {result['expires_at'].strftime('%H:%M UTC')}).", "success")
    return redirect(url_for("customer.dashboard"))


@admin_bp.route("/impersonate", methods=["DELETE", "POST"])
def end_impersonate():
    """End impersonation, restore the original admin session. Registered
    without @require_super_admin since current_user is the impersonated
    tenant user at this point, not the admin."""
    admin_user_id = end_impersonation()
    if not admin_user_id:
        flash("No active impersonation session.", "error")
        return redirect(url_for("customer.dashboard"))

    logout_user()
    admin_user = get_user_by_id(admin_user_id)
    if admin_user:
        login_user(User(admin_user))
    flash("Exited impersonation.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/cdr", methods=["GET"])
@require_super_admin
def cdr_logs():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    per_page = 25
    offset = (page - 1) * per_page
    tenant_filter = request.args.get("tenant_id")

    if tenant_filter:
        calls = db.query(
            """
            SELECT c.*, o.external_order_id, t.business_name
            FROM calls c
            LEFT JOIN orders o ON c.order_id = o.id
            LEFT JOIN tenants t ON c.tenant_id = t.id
            WHERE c.tenant_id = %s
            ORDER BY c.started_at DESC LIMIT %s OFFSET %s
            """,
            (tenant_filter, per_page, offset),
        )
        total_row = db.query_one("SELECT COUNT(*) AS n FROM calls WHERE tenant_id = %s", (tenant_filter,))
    else:
        calls = db.query(
            """
            SELECT c.*, o.external_order_id, t.business_name
            FROM calls c
            LEFT JOIN orders o ON c.order_id = o.id
            LEFT JOIN tenants t ON c.tenant_id = t.id
            ORDER BY c.started_at DESC LIMIT %s OFFSET %s
            """,
            (per_page, offset),
        )
        total_row = db.query_one("SELECT COUNT(*) AS n FROM calls")

    total = total_row["n"] if total_row else 0
    pages = int(total / per_page) + (1 if total % per_page > 0 else 0)
    tenants = get_all_tenants()

    return render_template(
        "admin/cdr.html",
        calls=calls,
        page=page,
        pages=pages,
        total=total,
        tenants=tenants,
        selected_tenant=tenant_filter,
    )


@admin_bp.route("/live-calls", methods=["GET"])
@require_super_admin
def live_calls_page():
    return render_template("admin/live_calls.html")


@admin_bp.route("/api/live-calls", methods=["GET"])
@require_super_admin
def api_live_calls():
    from app.services.live_call_service import list_live_calls, get_summary
    calls = list_live_calls(tenant_id=None)
    return jsonify({"success": True, "calls": calls, "summary": get_summary(tenant_id=None)})


@admin_bp.route("/api/live-calls/<int:call_id>/hangup", methods=["POST"])
@require_super_admin
def api_live_calls_hangup(call_id: int):
    from app.services.live_call_service import hangup_call
    from app.middleware.audit import audit_log

    ok = hangup_call(call_id, tenant_id=None)
    audit_log("live_call_hangup", current_user.id, "calls", call_id, {"success": ok})
    if not ok:
        return jsonify({"success": False, "error": "Call not active or already ended"}), 404
    return jsonify({"success": True})


@admin_bp.route("/cdr/backfill-billing", methods=["POST"])
@require_super_admin
def cdr_backfill_billing():
    """Bill selected completed-but-unbilled calls via finalize_call_billing().
    Deliberately scoped to status='completed' only — failed/no_answer/busy
    calls are never backfilled here even if selected, since charging those
    is a rate-config decision (charge_failed_calls), not a backfill one."""
    from app.services.billing_service import finalize_call_billing

    call_ids_raw = request.form.getlist("call_ids")
    call_ids = []
    for raw in call_ids_raw:
        try:
            call_ids.append(int(raw))
        except ValueError:
            continue

    if not call_ids:
        flash("No calls selected for billing backfill.", "error")
        return redirect(url_for("admin.cdr_logs"))

    eligible_rows = db.query(
        "SELECT id FROM calls WHERE id = ANY(%s) AND status = 'completed' AND wallet_transaction_id IS NULL",
        (call_ids,),
    )
    eligible_ids = [row["id"] for row in eligible_rows]

    billed_count = 0
    results = []
    for call_id in eligible_ids:
        result = finalize_call_billing(call_id)
        results.append(result)
        if result["billed"]:
            billed_count += 1

    from app.middleware.audit import audit_log
    audit_log(
        "cdr_billing_backfill",
        current_user.id,
        "calls",
        None,
        {"requested": call_ids, "eligible": eligible_ids, "billed": billed_count},
    )

    skipped = len(call_ids) - len(eligible_ids)
    flash(
        f"Backfilled billing for {billed_count} call(s)."
        + (f" {skipped} selected call(s) skipped (not completed or already billed)." if skipped else ""),
        "success",
    )
    return redirect(url_for("admin.cdr_logs"))


def _resolve_date_range():
    import datetime
    range_key = request.args.get("range", "this_month")
    today = datetime.date.today()

    if range_key == "last_month":
        first_of_this_month = today.replace(day=1)
        date_to = first_of_this_month - datetime.timedelta(days=1)
        date_from = date_to.replace(day=1)
    elif range_key == "custom":
        try:
            date_from = datetime.date.fromisoformat(request.args.get("date_from", ""))
            date_to = datetime.date.fromisoformat(request.args.get("date_to", ""))
        except ValueError:
            date_from = today.replace(day=1)
            date_to = today
    else:
        range_key = "this_month"
        date_from = today.replace(day=1)
        date_to = today

    return range_key, date_from, date_to


@admin_bp.route("/revenue", methods=["GET"])
@require_super_admin
def revenue_report():
    from app.services.revenue_service import get_revenue_report

    range_key, date_from, date_to = _resolve_date_range()
    report = get_revenue_report(date_from, date_to)

    return render_template(
        "admin/revenue.html",
        report=report,
        range_key=range_key,
        date_from=date_from,
        date_to=date_to,
    )


@admin_bp.route("/revenue/export.csv", methods=["GET"])
@require_super_admin
def revenue_export_csv():
    import csv
    from io import StringIO

    from flask import Response

    from app.services.revenue_service import get_revenue_report

    _, date_from, date_to = _resolve_date_range()
    report = get_revenue_report(date_from, date_to)

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Tenant", "Plan", "Calls", "Minutes", "Revenue", "Cost", "Profit"])
    for row in report["per_tenant"]:
        writer.writerow([row["name"], row["plan"], row["calls"], row["minutes"], row["revenue"], row["cost"], row["profit"]])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=revenue_{date_from}_{date_to}.csv"},
    )


@admin_bp.route("/cdr/export.csv", methods=["GET"])
@require_super_admin
def cdr_export_csv():
    import csv
    from io import StringIO

    from flask import Response

    tenant_filter = request.args.get("tenant_id")
    if tenant_filter:
        calls = db.query(
            """
            SELECT c.*, o.external_order_id, t.business_name
            FROM calls c
            LEFT JOIN orders o ON c.order_id = o.id
            LEFT JOIN tenants t ON c.tenant_id = t.id
            WHERE c.tenant_id = %s
            ORDER BY c.started_at DESC LIMIT 5000
            """,
            (tenant_filter,),
        )
    else:
        calls = db.query(
            """
            SELECT c.*, o.external_order_id, t.business_name
            FROM calls c
            LEFT JOIN orders o ON c.order_id = o.id
            LEFT JOIN tenants t ON c.tenant_id = t.id
            ORDER BY c.started_at DESC LIMIT 5000
            """
        )

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Call ID", "Tenant", "Order", "Phone (masked)", "Status", "Talk(s)", "Ring(s)", "Started", "Ended"])
    for c in calls:
        phone = c["number"] or ""
        masked = (phone[:3] + "****" + phone[-4:]) if len(phone) >= 11 else phone
        writer.writerow([
            c["id"], c.get("business_name") or "", c.get("external_order_id") or "",
            masked, c["status"], c["talk_seconds"], c["ring_seconds"],
            c["started_at"], c["ended_at"],
        ])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=cdr_export.csv"},
    )


@admin_bp.route("/settings", methods=["GET", "POST"])
@require_super_admin
def settings():
    if request.method == "POST":
        smslayer_api_key = request.form.get("smslayer_api_key", "").strip()
        sms_rate_str = request.form.get("sms_rate", "0").strip()

        try:
            sms_rate = Decimal(sms_rate_str)
            if sms_rate < 0:
                raise ValueError("SMS rate cannot be negative.")
        except (ValueError, InvalidOperation) as exc:
            flash(f"Invalid SMS rate: {exc}", "error")
            return redirect(url_for("admin.settings"))

        db.set_setting("PLATFORM_SMSLAYER_API_KEY", smslayer_api_key)
        db.set_setting("PLATFORM_SMS_RATE", str(sms_rate))

        flash("System settings updated successfully.", "success")
        return redirect(url_for("admin.settings"))

    smslayer_api_key = db.get_setting("PLATFORM_SMSLAYER_API_KEY") or ""
    sms_rate = db.get_setting("PLATFORM_SMS_RATE") or "0.00"

    return render_template(
        "admin/settings.html",
        smslayer_api_key=smslayer_api_key,
        sms_rate=sms_rate
    )


# ── Subscription management admin routes ────────────────────────────────────

@admin_bp.route("/customers/<int:tenant_id>/subscription/extend", methods=["POST"])
@require_super_admin
def subscription_extend(tenant_id: int):
    """Admin: extend a tenant's subscription period by N days."""
    days_str = request.form.get("days", "").strip()
    reason = request.form.get("reason", "").strip()
    if not days_str or not reason:
        flash("Days and reason are required.", "error")
        return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))
    try:
        days = int(days_str)
        if days <= 0:
            raise ValueError
    except ValueError:
        flash("Days must be a positive integer.", "error")
        return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))

    try:
        from app.services.subscription_lifecycle_service import admin_extend_subscription
        admin_extend_subscription(tenant_id, days, reason, actor_id=current_user.id)
        from app.middleware.audit import audit_log
        audit_log("subscription_admin_extend", current_user.id, "tenant", tenant_id,
                  {"days": days, "reason": reason})
        flash(f"Subscription extended by {days} day(s).", "success")
    except Exception as exc:
        logger.error("subscription_extend failed for tenant %s: %s", tenant_id, exc)
        flash(f"Extension failed: {exc}", "error")

    return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))


@admin_bp.route("/customers/<int:tenant_id>/subscription/cancel", methods=["POST"])
@require_super_admin
def subscription_cancel(tenant_id: int):
    """Admin: cancel a tenant's subscription immediately."""
    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Reason is required.", "error")
        return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))

    try:
        from app.services.subscription_lifecycle_service import cancel_subscription
        cancel_subscription(tenant_id, immediate=True, reason=reason, actor_type="admin", actor_id=current_user.id)
        from app.middleware.audit import audit_log
        audit_log("subscription_admin_cancel", current_user.id, "tenant", tenant_id, {"reason": reason})
        flash("Subscription cancelled.", "success")
    except Exception as exc:
        logger.error("subscription_cancel failed for tenant %s: %s", tenant_id, exc)
        flash(f"Cancellation failed: {exc}", "error")

    return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))


@admin_bp.route("/customers/<int:tenant_id>/subscription/reactivate", methods=["POST"])
@require_super_admin
def subscription_reactivate(tenant_id: int):
    """Admin: reactivate a suspended/cancelled/expired tenant's subscription (no payment required)."""
    reason = request.form.get("reason", "").strip()
    days = int(request.form.get("days", "30") or "30")

    try:
        from app.services.subscription_lifecycle_service import admin_reactivate_subscription
        admin_reactivate_subscription(tenant_id, reason or "Admin reactivation", actor_id=current_user.id, extend_days=days)
        from app.middleware.audit import audit_log
        audit_log("subscription_admin_reactivate", current_user.id, "tenant", tenant_id,
                  {"days": days, "reason": reason})
        flash(f"Tenant reactivated with {days}-day extension.", "success")
    except Exception as exc:
        logger.error("subscription_reactivate failed for tenant %s: %s", tenant_id, exc)
        flash(f"Reactivation failed: {exc}", "error")

    return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))


@admin_bp.route("/customers/<int:tenant_id>/subscription/suspend", methods=["POST"])
@require_super_admin
def subscription_suspend(tenant_id: int):
    """Admin: suspend a tenant for security/compliance/billing reasons."""
    reason = request.form.get("reason", "").strip()
    suspension_type = request.form.get("suspension_type", "security").strip()

    if not reason:
        flash("Suspension reason is required.", "error")
        return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))

    try:
        from app.services.subscription_lifecycle_service import suspend_subscription
        suspend_subscription(tenant_id, reason=reason, actor_id=current_user.id, suspension_type=suspension_type)
        from app.middleware.audit import audit_log
        audit_log("subscription_admin_suspend", current_user.id, "tenant", tenant_id,
                  {"reason": reason, "suspension_type": suspension_type})
        flash("Tenant suspended.", "warning")
    except Exception as exc:
        logger.error("subscription_suspend failed for tenant %s: %s", tenant_id, exc)
        flash(f"Suspension failed: {exc}", "error")

    return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))

