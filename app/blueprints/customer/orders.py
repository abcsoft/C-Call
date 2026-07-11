from __future__ import annotations

import csv
import logging
import datetime
import pytz
from io import StringIO
from decimal import Decimal
from typing import Any
from flask import render_template, redirect, url_for, flash, request, jsonify, g, make_response, current_app

from app.blueprints.customer import customer_bp
from app.blueprints.auth.decorators import require_login, require_role
from app.models.order import (
    get_orders,
    get_orders_count,
    get_order_by_id,
    get_order_by_external_id,
    get_call_attempts_for_order
)
from app.db import db
from app.services.order_service import manual_recall

logger = logging.getLogger("ccall")
local_tz = pytz.timezone("Asia/Dhaka")

def parse_date_range(date_from_str: str | None, date_to_str: str | None) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    date_from = None
    date_to = None
    if date_from_str:
        try:
            dt = datetime.datetime.strptime(date_from_str.strip(), "%Y-%m-%d")
            date_from = local_tz.localize(datetime.datetime.combine(dt.date(), datetime.time.min)).astimezone(datetime.timezone.utc)
        except ValueError:
            pass
    if date_to_str:
        try:
            dt = datetime.datetime.strptime(date_to_str.strip(), "%Y-%m-%d")
            date_to = local_tz.localize(datetime.datetime.combine(dt.date(), datetime.time.max)).astimezone(datetime.timezone.utc)
        except ValueError:
            pass
    return date_from, date_to

def get_orders_common(tenant_id: int) -> dict[str, Any]:
    status_filter = request.args.get("status", "").strip()
    search = request.args.get("search", "").strip()
    date_from_str = request.args.get("date_from", "").strip()
    date_to_str = request.args.get("date_to", "").strip()
    
    limit = min(50, max(1, int(request.args.get("limit", 20))))
    after_id = request.args.get("after")
    before_id = request.args.get("before")
    
    try:
        after_id = int(after_id) if after_id else None
    except ValueError:
        after_id = None
        
    try:
        before_id = int(before_id) if before_id else None
    except ValueError:
        before_id = None
        
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0

    db_status = None
    if status_filter:
        if status_filter.lower() == "retry pending":
            db_status = "RETRY_SCHEDULED"
        elif status_filter.lower() == "no answer":
            db_status = "NO_ANSWER"
        else:
            db_status = status_filter.upper()

    date_from, date_to = parse_date_range(date_from_str, date_to_str)

    orders = get_orders(
        tenant_id=tenant_id,
        status=db_status,
        search=search,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        after_id=after_id,
        before_id=before_id
    )
    total = get_orders_count(
        tenant_id=tenant_id,
        status=db_status,
        search=search,
        date_from=date_from,
        date_to=date_to
    )

    next_cursor = orders[-1]["id"] if orders else None
    prev_cursor = orders[0]["id"] if orders else None

    has_next = False
    has_prev = False
    if orders:
        has_next_row = get_orders(
            tenant_id=tenant_id, status=db_status, search=search,
            date_from=date_from, date_to=date_to, limit=1,
            after_id=next_cursor
        )
        has_next = len(has_next_row) > 0

        has_prev_row = get_orders(
            tenant_id=tenant_id, status=db_status, search=search,
            date_from=date_from, date_to=date_to, limit=1,
            before_id=prev_cursor
        )
        has_prev = len(has_prev_row) > 0

    start_idx = offset + 1 if orders else 0
    end_idx = min(total, offset + len(orders))

    return {
        "orders": orders,
        "status": status_filter,
        "search": search,
        "date_from": date_from_str,
        "date_to": date_to_str,
        "total": total,
        "offset": offset,
        "start_idx": start_idx,
        "end_idx": end_idx,
        "has_next": has_next,
        "has_prev": has_prev,
        "next_cursor": next_cursor,
        "prev_cursor": prev_cursor,
        "limit": limit
    }

@customer_bp.route("/orders", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def orders_list():
    context = get_orders_common(g.tenant_id)
    return render_template("customer/orders/list.html", **context)

@customer_bp.route("/api/orders", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def api_orders():
    tenant_id = g.tenant_id
    if "application/json" in request.headers.get("Accept", ""):
        # Return serialized orders for JSON requests
        status_filter = request.args.get("status", "").strip()
        search = request.args.get("search", "").strip()
        date_from_str = request.args.get("date_from", "").strip()
        date_to_str = request.args.get("date_to", "").strip()
        limit = min(50, max(1, int(request.args.get("limit", 20))))
        offset = max(0, int(request.args.get("offset", 0)))
        
        db_status = None
        if status_filter:
            if status_filter.lower() == "retry pending":
                db_status = "RETRY_SCHEDULED"
            elif status_filter.lower() == "no answer":
                db_status = "NO_ANSWER"
            else:
                db_status = status_filter.upper()

        date_from, date_to = parse_date_range(date_from_str, date_to_str)
        orders = get_orders(
            tenant_id=tenant_id, status=db_status, search=search,
            date_from=date_from, date_to=date_to, limit=limit, offset=offset
        )
        
        # Serialize decimal/datetime
        serialized = []
        for o in orders:
            o_copy = dict(o)
            if isinstance(o_copy.get("amount"), Decimal):
                o_copy["amount"] = float(o_copy["amount"])
            for date_key in ("created_at", "updated_at", "confirmed_at", "next_retry_at"):
                if isinstance(o_copy.get(date_key), (datetime.datetime, datetime.date)):
                    o_copy[date_key] = o_copy[date_key].isoformat()
            serialized.append(o_copy)
        return jsonify({"success": True, "orders": serialized})

    # Default to returning the HTML rows for HTMX updates
    context = get_orders_common(tenant_id)
    if request.args.get("template") == "raw":
        return render_template("customer/orders/_tr_rows.html", **context)
    return render_template("customer/orders/_table_rows.html", **context)

@customer_bp.route("/orders/<external_order_id>", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def order_detail(external_order_id: str):
    tenant_id = g.tenant_id
    order = get_order_by_external_id(tenant_id, external_order_id)
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("customer.orders_list"))

    attempts = get_call_attempts_for_order(tenant_id, order["id"])
    from app.services.transcript_service import get_call_transcript_timeline
    for attempt in attempts:
        if attempt.get("call_id"):
            call = db.query_one("SELECT recording_path, status, started_at, ended_at FROM calls WHERE id = %s", (attempt["call_id"],))
            if call:
                attempt["recording_path"] = call["recording_path"]
                attempt["call_status"] = call["status"]
                if call.get("recording_path"):
                    import os
                    attempt["recording_filename"] = os.path.basename(call["recording_path"])
                attempt["transcript"] = get_call_transcript_timeline(attempt["call_id"])

    webhook_attempts = db.query(
        "SELECT * FROM webhook_logs WHERE tenant_id = %s AND order_id = %s ORDER BY sent_at DESC",
        (tenant_id, order["id"])
    )

    return render_template(
        "customer/orders/detail.html",
        order=order,
        attempts=attempts,
        webhook_attempts=webhook_attempts
    )

@customer_bp.route("/orders/<external_order_id>/recall", methods=["POST"])
@require_login
@require_role(["admin", "agent"])
def order_recall(external_order_id: str):
    tenant_id = g.tenant_id
    order = get_order_by_external_id(tenant_id, external_order_id)
    if not order:
        flash("Order not found.", "error")
        return redirect(url_for("customer.orders_list"))
    try:
        call_id = manual_recall(order["id"], tenant_id)
        flash(f"Manual recall queued successfully! Call ID: {call_id}", "success")
    except Exception as exc:
        flash(f"Failed to place recall: {exc}", "error")
    return redirect(url_for("customer.order_detail", external_order_id=external_order_id))

@customer_bp.route("/api/orders/<int:order_id>/status", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def order_status_api(order_id: int):
    tenant_id = g.tenant_id
    order = get_order_by_id(tenant_id, order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
        
    return jsonify({
        "success": True,
        "status": order["order_status"],
        "attempt_count": order["attempt_count"],
        "confirmed_at": order["confirmed_at"].isoformat() if isinstance(order.get("confirmed_at"), (datetime.datetime, datetime.date)) else order.get("confirmed_at"),
        "webhook_status": order["webhook_status"]
    })

@customer_bp.route("/api/orders/export", methods=["GET"])
@require_login
@require_role(["admin"])
def orders_export():
    tenant_id = g.tenant_id
    status_filter = request.args.get("status", "").strip()
    search = request.args.get("search", "").strip()
    date_from_str = request.args.get("date_from", "").strip()
    date_to_str = request.args.get("date_to", "").strip()
    
    db_status = None
    if status_filter:
        if status_filter.lower() == "retry pending":
            db_status = "RETRY_SCHEDULED"
        elif status_filter.lower() == "no answer":
            db_status = "NO_ANSWER"
        else:
            db_status = status_filter.upper()

    date_from, date_to = parse_date_range(date_from_str, date_to_str)

    sql = "SELECT * FROM orders WHERE tenant_id = %s"
    params: list[Any] = [tenant_id]
    if db_status:
        sql += " AND order_status = %s"
        params.append(db_status)
    if search:
        sql += " AND (phone LIKE %s OR customer_name ILIKE %s OR external_order_id ILIKE %s)"
        search_param = f"%{search}%"
        params.extend([search_param, search_param, search_param])
    if date_from:
        sql += " AND created_at >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND created_at <= %s"
        params.append(date_to)
    sql += " ORDER BY created_at DESC"
    
    orders = db.query(sql, tuple(params))
    
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(["Order ID", "Customer Name", "Phone", "COD Amount", "Status", "Attempt Count", "Created At", "Confirmed At", "Webhook Status"])
    
    from app.middleware.security import mask_phone
    for o in orders:
        cw.writerow([
            o["external_order_id"],
            o["customer_name"] or "—",
            mask_phone(o["phone"]),
            o["amount"],
            o["order_status"],
            o["attempt_count"],
            o["created_at"],
            o["confirmed_at"] or "—",
            o["webhook_status"] or "—"
        ])
        
    response = make_response(si.getvalue())
    response.headers["Content-Disposition"] = "attachment; filename=ccall_orders.csv"
    response.headers["Content-Type"] = "text/csv"
    return response

@customer_bp.route("/api/orders/<int:order_id>/retry/cancel", methods=["POST"])
@require_login
@require_role(["admin", "agent"])
def cancel_retry_route(order_id: int):
    tenant_id = g.tenant_id
    order = get_order_by_id(tenant_id, order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
        
    from app.services.retry_service import cancel_pending_retries
    cancel_pending_retries(order_id, tenant_id)
    return jsonify({"success": True})

@customer_bp.route("/api/orders/<int:order_id>/webhook/retry", methods=["POST"])
@require_login
@require_role(["admin", "agent"])
def webhook_retry_route(order_id: int):
    tenant_id = g.tenant_id
    order = get_order_by_id(tenant_id, order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
        
    from app.services.webhook_service import deliver_async
    deliver_async(order_id)
    return jsonify({"success": True})


@customer_bp.route("/api/recordings/<int:attempt_id>/url", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def get_recording_signed_url(attempt_id: int):
    import os
    tenant_id = g.tenant_id
    attempt = db.query_one("SELECT * FROM order_call_attempts WHERE id = %s AND tenant_id = %s", (attempt_id, tenant_id))
    if not attempt or not attempt.get("call_id"):
        return "Call attempt not found", 404

    call = db.query_one("SELECT recording_path, talk_seconds FROM calls WHERE id = %s AND tenant_id = %s", (attempt["call_id"], tenant_id))
    if not call or not call.get("recording_path"):
        return "Call recording not found", 404

    from app.services.recording_service import get_signed_url
    try:
        signed_url = get_signed_url(call["recording_path"], tenant_id)
    except PermissionError as exc:
        return str(exc), 403

    filename = os.path.basename(call["recording_path"])

    return f"""
    <div class="pt-2 border-t border-slate-800/40 space-y-1.5 w-full">
        <span class="text-[10px] text-slate-500 uppercase tracking-wider block">Call Recording (Duration: {call['talk_seconds'] or 0}s)</span>
        <div class="flex flex-col sm:flex-row items-center gap-3">
            <audio controls autoplay class="w-full max-w-md bg-slate-950/60 rounded-lg py-1 px-2 border border-slate-800 focus:outline-none">
                <source src="{signed_url}" type="audio/wav">
                Your browser does not support the audio element.
            </audio>
            <a href="{signed_url}&download=1" download="{filename}" 
               class="py-2 px-4 bg-slate-800 hover:bg-slate-700 text-slate-200 font-bold rounded-xl text-xs transition-all flex items-center gap-1.5 shrink-0">
               <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                   <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path>
               </svg>
               Download
            </a>
        </div>
    </div>
    """
