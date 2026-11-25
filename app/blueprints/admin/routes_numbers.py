from __future__ import annotations

import logging
import secrets
from decimal import Decimal
from flask import render_template, redirect, url_for, flash, request, jsonify
from flask_login import current_user

from app.blueprints.admin import admin_bp
from app.blueprints.auth.decorators import require_super_admin
from app.services.number_service import (
    assign_number,
    unassign_number,
    add_to_pool,
    get_pool,
    get_pool_stats,
    bulk_import_numbers,
)
from app.models.sip_provider import (
    get_all_providers,
    create_provider,
    update_provider,
    delete_sip_provider,
    test_connection
)
from app.models.tenant import get_all_tenants
from app.models.phone_number import update_phone_number
from app.db import db

logger = logging.getLogger("ccall")

@admin_bp.route("/numbers", methods=["GET", "POST"])
@require_super_admin
def numbers_panel():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            number = request.form.get("number", "").strip()
            provider_id = int(request.form.get("provider_id", 0))
            number_type = request.form.get("number_type", "shared")
            monthly_cost = Decimal(request.form.get("monthly_cost", "0"))
            tenant_id_str = request.form.get("tenant_id")
            try:
                if number_type == "dedicated":
                    if not tenant_id_str:
                        raise ValueError("Tenant is required when CLI Mode is Dedicated.")
                    tenant_id = int(tenant_id_str)
                    num = add_to_pool(number, provider_id, number_type, monthly_cost)
                    assign_number(tenant_id, num["id"], "dedicated")
                else:
                    add_to_pool(number, provider_id, number_type, monthly_cost)
                flash(f"Phone number {number} added successfully.", "success")
            except Exception as exc:
                flash(f"Failed to add number: {exc}", "error")
        return redirect(url_for("admin.numbers_panel"))

    # GET request - list pool
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
        
    per_page = 20
    status_filter = request.args.get("status")
    type_filter = request.args.get("number_type")
    search_filter = request.args.get("search")
    
    filters = {}
    if status_filter and status_filter != "all":
        filters["status"] = status_filter
    if type_filter and type_filter != "all":
        filters["number_type"] = type_filter
    if search_filter:
        filters["search"] = search_filter

    pool_data = get_pool(page=page, per_page=per_page, filters=filters)
    numbers = pool_data["data"]
    total = pool_data["total"]
    pages = int(total / per_page) + (1 if total % per_page > 0 else 0)

    providers = get_all_providers()
    tenants = get_all_tenants()
    stats = get_pool_stats()

    return render_template(
        "admin/numbers.html",
        numbers=numbers,
        providers=providers,
        tenants=tenants,
        page=page,
        pages=pages,
        total=total,
        stats=stats,
        current_status=status_filter or "all",
        current_type=type_filter or "all",
        search_query=search_filter or ""
    )


@admin_bp.route("/numbers/bulk-import", methods=["POST"])
@require_super_admin
def numbers_bulk_import():
    import csv
    import io

    file = request.files.get("csv_file")
    if not file or not file.filename:
        flash("No CSV file uploaded.", "error")
        return redirect(url_for("admin.numbers_panel"))

    try:
        text = file.read().decode("utf-8-sig", errors="ignore")
        reader = csv.DictReader(io.StringIO(text))
        rows = [
            {
                "number": r.get("number") or r.get("Number") or "",
                "provider_id": r.get("provider_id") or r.get("Provider ID") or "",
                "number_type": r.get("number_type") or r.get("Type") or "shared",
                "monthly_cost": r.get("monthly_cost") or r.get("Monthly Cost") or "0",
            }
            for r in reader
        ]
        result = bulk_import_numbers(rows)
        flash(
            f"Bulk import: {result['imported_count']} imported, {result['skipped_count']} skipped.",
            "success" if result["imported_count"] else "error",
        )
        if result["skipped"]:
            for s in result["skipped"][:5]:
                flash(f"Skipped {s['number']}: {s['reason']}", "error")
    except Exception as exc:
        flash(f"Bulk import failed: {exc}", "error")

    return redirect(url_for("admin.numbers_panel"))

@admin_bp.route("/numbers/<int:number_id>", methods=["POST", "PUT"])
@require_super_admin
def update_number_details(number_id: int):
    # Support PUT via _method parameter in POST forms
    is_put = request.method == "PUT" or (request.method == "POST" and request.form.get("_method") == "PUT")
    
    number_type = request.form.get("number_type")
    status = request.form.get("status")
    monthly_cost_str = request.form.get("monthly_cost")
    provider_id_str = request.form.get("provider_id")

    try:
        # Load existing number
        existing = db.query_one("SELECT * FROM phone_numbers WHERE id = %s", (number_id,))
        if not existing:
            return jsonify({"success": False, "error": "Number not found"}), 404

        new_type = number_type or existing["number_type"]
        new_status = status or existing["status"]
        new_cost = Decimal(monthly_cost_str) if monthly_cost_str is not None else existing["monthly_cost"]
        new_provider_id = int(provider_id_str) if provider_id_str else None

        if new_type == "shared":
            assign_number(None, number_id, "shared")
            update_phone_number(number_id, "shared", new_status, new_cost, provider_id=new_provider_id)
        else:
            update_phone_number(number_id, new_type, new_status, new_cost, provider_id=new_provider_id)
            
        flash("Phone number updated successfully.", "success")
        
        if request.headers.get("Accept") == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": True})
    except Exception as exc:
        flash(f"Failed to update number: {exc}", "error")
        if request.headers.get("Accept") == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"success": False, "error": str(exc)}), 400
            
    return redirect(url_for("admin.numbers_panel"))

@admin_bp.route("/numbers/<int:number_id>/assign", methods=["POST", "DELETE"])
@require_super_admin
def assign_or_unassign_number(number_id: int):
    # Support DELETE via _method parameter in POST forms
    is_delete = request.method == "DELETE" or (request.method == "POST" and request.form.get("_method") == "DELETE") or (request.method == "POST" and request.form.get("action") == "unassign")
    
    if is_delete:
        tenant_id = request.form.get("tenant_id")
        if not tenant_id:
            # Look up tenant_id from active assignments
            assignment = db.query_one(
                "SELECT tenant_id FROM customer_number_assignments WHERE phone_number_id = %s AND unassigned_at IS NULL",
                (number_id,)
            )
            if assignment:
                tenant_id = assignment["tenant_id"]
        if not tenant_id:
            if request.headers.get("Accept") == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"success": False, "error": "Tenant ID is required for unassigning"}), 400
            flash("Tenant ID is required to unassign.", "error")
            return redirect(url_for("admin.numbers_panel"))
            
        try:
            unassign_number(int(tenant_id), number_id)
            flash("Phone number unassigned successfully.", "success")
            if request.headers.get("Accept") == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"success": True})
        except Exception as exc:
            flash(f"Failed to unassign: {exc}", "error")
            if request.headers.get("Accept") == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"success": False, "error": str(exc)}), 400
    else:
        tenant_id = request.form.get("tenant_id")
        number_type = request.form.get("number_type", "dedicated")
        if not tenant_id:
            if request.headers.get("Accept") == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"success": False, "error": "Tenant ID is required for assigning"}), 400
            flash("Tenant ID is required to assign.", "error")
            return redirect(url_for("admin.numbers_panel"))
            
        try:
            assign_number(int(tenant_id), number_id, number_type)
            flash("Phone number assigned successfully.", "success")
            if request.headers.get("Accept") == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"success": True})
        except Exception as exc:
            flash(f"Failed to assign: {exc}", "error")
            if request.headers.get("Accept") == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"success": False, "error": str(exc)}), 400
                
    return redirect(url_for("admin.numbers_panel"))

@admin_bp.route("/providers", methods=["GET", "POST"])
@require_super_admin
def providers_panel():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            sip_domain = request.form.get("sip_domain", "").strip()
            sip_user = request.form.get("sip_user", "").strip()
            sip_password = request.form.get("sip_password", "").strip()
            try:
                sip_port = int(request.form.get("sip_port", 5060) or 5060)
                max_channels = int(request.form.get("max_channels", 10) or 10)
                create_provider({
                    "name": name,
                    "sip_domain": sip_domain,
                    "sip_port": sip_port,
                    "sip_user": sip_user,
                    "sip_password": sip_password,
                    "max_channels": max_channels,
                })
                flash(f"SIP provider {name} added successfully.", "success")
            except Exception as exc:
                flash(f"Failed to add provider: {exc}", "error")

        elif action == "edit":
            provider_id = int(request.form.get("provider_id", 0))
            name = request.form.get("name", "").strip()
            sip_domain = request.form.get("sip_domain", "").strip()
            sip_user = request.form.get("sip_user", "").strip()
            sip_password = request.form.get("sip_password", "").strip()
            password_plaintext = sip_password if sip_password else None
            try:
                sip_port = int(request.form.get("sip_port", 5060) or 5060)
                max_channels = int(request.form.get("max_channels", 10) or 10)
                update_provider(provider_id, {
                    "name": name,
                    "sip_domain": sip_domain,
                    "sip_port": sip_port,
                    "sip_user": sip_user,
                    "sip_password": password_plaintext,
                    "max_channels": max_channels,
                })
                flash(f"SIP provider {name} updated successfully.", "success")
            except Exception as exc:
                flash(f"Failed to update provider: {exc}", "error")

        elif action == "delete":
            provider_id = int(request.form.get("provider_id", 0))
            try:
                delete_sip_provider(provider_id)
                flash("SIP provider deleted successfully.", "success")
            except Exception as exc:
                flash(f"Failed to delete provider: {exc}", "error")

        elif action == "status":
            from app.models.sip_provider import update_sip_provider_status
            provider_id = int(request.form.get("provider_id", 0))
            status = request.form.get("status", "active")
            try:
                update_sip_provider_status(provider_id, status)
                flash(f"Provider status set to {status}.", "success")
            except Exception as exc:
                flash(f"Failed to update status: {exc}", "error")

        elif action == "reconcile_stale":
            from app.models.sip_provider import reconcile_stale_calls
            provider_id = int(request.form.get("provider_id", 0))
            try:
                closed = reconcile_stale_calls(provider_id=provider_id if provider_id > 0 else None)
                flash(f"Reconciled {closed} stale call(s).", "success")
            except Exception as exc:
                flash(f"Failed to reconcile stale calls: {exc}", "error")

        return redirect(url_for("admin.providers_panel"))

    providers = get_all_providers()
    for p in providers:
        active_row = db.query_one(
            """SELECT COUNT(*) AS n FROM calls
               WHERE (sip_gateway_id = %s OR caller_id IN (SELECT number FROM phone_numbers WHERE provider_id = %s))
                 AND status IN ('queued', 'initiated', 'preparing', 'dialing', 'calling', 'ringing', 'answered')""",
            (p["id"], p["id"]),
        )
        stale_row = db.query_one(
            """SELECT COUNT(*) AS n FROM calls
               WHERE (sip_gateway_id = %s OR caller_id IN (SELECT number FROM phone_numbers WHERE provider_id = %s))
                 AND status IN ('queued', 'initiated', 'preparing', 'dialing', 'calling', 'ringing', 'answered')
                 AND started_at IS NOT NULL
                 AND started_at < now() - interval '60 minutes'""",
            (p["id"], p["id"]),
        )
        total_row = db.query_one(
            """SELECT COUNT(*) AS n FROM calls
               WHERE (sip_gateway_id = %s OR caller_id IN (SELECT number FROM phone_numbers WHERE provider_id = %s))""",
            (p["id"], p["id"]),
        )
        p["active_calls"] = active_row["n"] if active_row else 0
        p["stale_calls"] = stale_row["n"] if stale_row else 0
        p["total_calls"] = total_row["n"] if total_row else 0
    return render_template("admin/providers.html", providers=providers)

@admin_bp.route("/providers/<int:provider_id>/test", methods=["POST"])
@require_super_admin
def test_provider_connection(provider_id: int):
    # Runs non-calling DNS + UDP OPTIONS + SIP Digest REGISTER diagnostics
    res = test_connection(provider_id)
    if request.headers.get("HX-Request"):
        return render_template("admin/_provider_test_result.html", result=res)
    return jsonify(res)
