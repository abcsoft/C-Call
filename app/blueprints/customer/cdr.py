from __future__ import annotations

import csv
import os
import logging
import datetime
from decimal import Decimal
from io import StringIO
from typing import Any
from urllib.parse import urlparse

from flask import render_template, redirect, url_for, flash, request, jsonify, g, make_response, Response, stream_with_context
from flask_login import current_user

from app.blueprints.customer import customer_bp
from app.blueprints.auth.decorators import require_login, require_role
from app.db import db
from app.models.tenant import get_tenant_by_id

logger = logging.getLogger("ccall")

def get_cdr_summaries(tenant_id: int):
    # Current month start and today start in local time
    now = datetime.datetime.now()
    today_start = datetime.datetime(now.year, now.month, now.day, 0, 0, 0)
    month_start = datetime.datetime(now.year, now.month, 1, 0, 0, 0)
    
    # Query all calls for today and this month. Sum only what's actually
    # stored on calls.charge/calls.billable_seconds — no recalculating a
    # "what it would have cost" number for calls that were never billed.
    calls = db.query(
        """
        SELECT c.started_at, c.billable_seconds, c.charge
        FROM calls c
        WHERE c.tenant_id = %s AND c.started_at >= %s
        """,
        (tenant_id, month_start)
    )

    today_count = 0
    today_billable_seconds = 0
    today_charge = Decimal("0.00")

    month_count = 0
    month_billable_seconds = 0
    month_charge = Decimal("0.00")

    for c in calls:
        bill_sec = c.get("billable_seconds") or 0
        charge_val = c.get("charge") or Decimal("0.00")

        is_today = False
        if c["started_at"]:
            if c["started_at"].tzinfo:
                is_today = (c["started_at"] >= today_start.replace(tzinfo=c["started_at"].tzinfo))
            else:
                is_today = (c["started_at"] >= today_start)
        
        if is_today:
            today_count += 1
            today_billable_seconds += bill_sec
            today_charge += Decimal(str(charge_val))
            
        month_count += 1
        month_billable_seconds += bill_sec
        month_charge += Decimal(str(charge_val))
        
    return {
        "today": {
            "calls": today_count,
            "minutes": round(today_billable_seconds / 60.0, 1),
            "charged": today_charge
        },
        "month": {
            "calls": month_count,
            "minutes": round(month_billable_seconds / 60.0, 1),
            "charged": month_charge
        }
    }

def format_duration(seconds: int | None) -> str:
    """Format duration as '1m 12s' or '45s'."""
    if not seconds:
        return "0s"
    m = seconds // 60
    s = seconds % 60
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

def mask_phone(phone: str) -> str:
    """Mask phone numbers keeping the first 3 and last 4 digits, replacing middle with ****."""
    if not phone:
        return ""
    clean = phone.replace("+88", "").replace("88", "")
    if len(clean) == 11:
        return clean[:3] + "****" + clean[7:]
    elif len(clean) > 7:
        return clean[:3] + "****" + clean[-4:]
    return clean

def query_calls_common(tenant_id: int, request_args: Any) -> dict[str, Any]:
    status_filter = request_args.get("status", "").strip()
    search = request_args.get("search", "").strip()
    date_from_str = request_args.get("date_from", "").strip()
    date_to_str = request_args.get("date_to", "").strip()
    limit = min(50, max(1, int(request_args.get("limit", 20))))
    after_id = request_args.get("after")
    before_id = request_args.get("before")
    
    try:
        after_id = int(after_id) if after_id else None
    except ValueError:
        after_id = None
        
    try:
        before_id = int(before_id) if before_id else None
    except ValueError:
        before_id = None
        
    try:
        offset = max(0, int(request_args.get("offset", 0)))
    except ValueError:
        offset = 0

    where_clauses = ["c.tenant_id = %s"]
    params = [tenant_id]
    
    if status_filter:
        where_clauses.append("c.status = %s")
        params.append(status_filter.lower())
    if search:
        search_like = f"%{search}%"
        where_clauses.append("(c.number ILIKE %s OR o.external_order_id ILIKE %s)")
        params.extend([search_like, search_like])
    if date_from_str:
        try:
            date_from = datetime.datetime.strptime(date_from_str, "%Y-%m-%d")
            where_clauses.append("c.started_at >= %s")
            params.append(date_from)
        except ValueError:
            pass
    if date_to_str:
        try:
            date_to = datetime.datetime.strptime(date_to_str, "%Y-%m-%d") + datetime.timedelta(days=1)
            where_clauses.append("c.started_at < %s")
            params.append(date_to)
        except ValueError:
            pass

    # Count total matching records
    where_str_count = " AND ".join(where_clauses)
    count_query = f"""
        SELECT COUNT(c.id) 
        FROM calls c
        LEFT JOIN orders o ON c.order_id = o.id
        WHERE {where_str_count}
    """
    total_row = db.query_one(count_query, tuple(params))
    total = total_row["count"] if total_row else 0

    # Cursor logic
    base_where_clauses = list(where_clauses)
    base_params = list(params)
    
    main_where_clauses = list(base_where_clauses)
    main_params = list(base_params)
    if after_id:
        main_where_clauses.append(
            "(c.started_at, c.id) < (SELECT started_at, id FROM calls WHERE id = %s)"
        )
        main_params.append(after_id)
    elif before_id:
        main_where_clauses.append(
            "(c.started_at, c.id) > (SELECT started_at, id FROM calls WHERE id = %s)"
        )
        main_params.append(before_id)

    where_str = " AND ".join(main_where_clauses)
    sort_order = "ASC" if before_id else "DESC"

    query = f"""
        SELECT c.*, o.external_order_id, a.dtmf_digit, a.result as attempt_result
        FROM calls c
        LEFT JOIN orders o ON c.order_id = o.id
        LEFT JOIN order_call_attempts a ON c.id = a.call_id AND c.tenant_id = a.tenant_id
        WHERE {where_str}
        ORDER BY c.started_at {sort_order}, c.id {sort_order}
        LIMIT %s
    """
    main_params.append(limit)
    
    calls = db.query(query, tuple(main_params))
    if before_id:
        calls = list(reversed(calls))

    # Hydrate format fields. Charge/billable_seconds are displayed exactly
    # as stored on calls — never recalculated in the UI. A completed call
    # with talk_seconds>0 but no wallet_transaction_id is a real unbilled
    # call (e.g. from before billing_service existed) and must be flagged,
    # not silently shown with a made-up charge.
    for c in calls:
        c["formatted_duration"] = format_duration(c.get("talk_seconds"))

        bill_sec = c.get("billable_seconds") or 0
        c["billable_seconds"] = bill_sec
        c["formatted_billable"] = format_duration(bill_sec)

        c["masked_phone"] = mask_phone(c.get("number", ""))

        # DTMF formatting
        dtmf = c.get("dtmf_digit")
        if dtmf == "1":
            c["formatted_dtmf"] = "1 (Confirmed)"
        elif dtmf == "2":
            c["formatted_dtmf"] = "2 (Cancelled)"
        elif dtmf:
            c["formatted_dtmf"] = f"{dtmf} (Other)"
        else:
            c["formatted_dtmf"] = "—"

        # Cost — stored value only, no fallback calculation.
        charge_val = c.get("charge") or Decimal("0.00")
        c["charge"] = charge_val
        c["display_charge"] = f"৳{float(charge_val):.2f}"

        c["is_unbilled_historical"] = (
            c.get("status") == "completed"
            and (c.get("talk_seconds") or 0) > 0
            and charge_val == 0
            and c.get("wallet_transaction_id") is None
        )

        # Started at formatting
        sat = c.get("started_at")
        c["formatted_start"] = sat.strftime("%Y-%m-%d %H:%M:%S") if sat else ""

    next_cursor = calls[-1]["id"] if calls else None
    prev_cursor = calls[0]["id"] if calls else None

    # Check if there are next/prev records
    has_next = False
    has_prev = False
    
    if calls:
        next_check_clause = base_where_clauses + ["(c.started_at, c.id) < (SELECT started_at, id FROM calls WHERE id = %s)"]
        next_check_params = base_params + [next_cursor, 1]
        next_check_query = f"""
            SELECT c.id FROM calls c 
            LEFT JOIN orders o ON c.order_id = o.id
            WHERE {" AND ".join(next_check_clause)} 
            LIMIT %s
        """
        has_next = len(db.query(next_check_query, tuple(next_check_params))) > 0

        prev_check_clause = base_where_clauses + ["(c.started_at, c.id) > (SELECT started_at, id FROM calls WHERE id = %s)"]
        prev_check_params = base_params + [prev_cursor, 1]
        prev_check_query = f"""
            SELECT c.id FROM calls c 
            LEFT JOIN orders o ON c.order_id = o.id
            WHERE {" AND ".join(prev_check_clause)} 
            LIMIT %s
        """
        has_prev = len(db.query(prev_check_query, tuple(prev_check_params))) > 0

    return {
        "calls": calls,
        "total": total,
        "limit": limit,
        "next_cursor": next_cursor,
        "prev_cursor": prev_cursor,
        "has_next": has_next,
        "has_prev": has_prev,
        "offset": offset,
        "status": status_filter,
        "search": search,
        "date_from": date_from_str,
        "date_to": date_to_str
    }

@customer_bp.route("/cdr", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def cdr_logs():
    tenant_id = g.tenant_id
    summaries = get_cdr_summaries(tenant_id)
    return render_template("customer/cdr.html", summaries=summaries)

@customer_bp.route("/api/cdr", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def api_cdr():
    tenant_id = g.tenant_id
    context = query_calls_common(tenant_id, request.args)
    
    if "application/json" in request.headers.get("Accept", ""):
        # Serialize datetime and decimal
        serialized_calls = []
        for c in context["calls"]:
            c_copy = dict(c)
            if isinstance(c_copy.get("charge"), Decimal):
                c_copy["charge"] = float(c_copy["charge"])
            for dk in ("started_at", "answered_at", "ended_at", "cdr_date", "created_at"):
                if isinstance(c_copy.get(dk), (datetime.datetime, datetime.date)):
                    c_copy[dk] = c_copy[dk].isoformat()
            serialized_calls.append(c_copy)
        
        return jsonify({
            "success": True,
            "calls": serialized_calls,
            "total": context["total"],
            "has_next": context["has_next"],
            "has_prev": context["has_prev"],
            "next_cursor": context["next_cursor"],
            "prev_cursor": context["prev_cursor"]
        })

    # Return HTML rows partial for HTMX paging
    return render_template("customer/_cdr_table_rows.html", **context)

@customer_bp.route("/api/cdr/summary", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def api_cdr_summary():
    tenant_id = g.tenant_id
    summaries = get_cdr_summaries(tenant_id)
    return render_template("customer/_cdr_summary.html", summaries=summaries)

@customer_bp.route("/api/cdr/<int:call_id>/details", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def api_cdr_details(call_id: int):
    """Retrieve details for row expansion via HTMX."""
    tenant_id = g.tenant_id
    call = db.query_one(
        """
        SELECT c.*, o.external_order_id, o.customer_name, o.amount as order_amount
        FROM calls c
        LEFT JOIN orders o ON c.order_id = o.id
        WHERE c.id = %s AND c.tenant_id = %s
        """,
        (call_id, tenant_id)
    )
    if not call:
        return "Call not found", 404
        
    events = db.query(
        "SELECT * FROM call_events WHERE call_id = %s ORDER BY seq ASC",
        (call_id,)
    )
    
    return render_template("customer/_cdr_detail_expanded.html", call=call, events=events)

@customer_bp.route("/api/cdr/export", methods=["GET"])
@require_login
@require_role(["admin"])
def api_cdr_export():
    """Download CSV file streaming response to prevent memory spike."""
    tenant_id = g.tenant_id
    tenant = get_tenant_by_id(tenant_id)
    tenant_name = tenant.get("business_name", "merchant").replace(" ", "_").lower()
    date_str = datetime.date.today().isoformat()
    
    status_filter = request.args.get("status", "").strip()
    search = request.args.get("search", "").strip()
    date_from_str = request.args.get("date_from", "").strip()
    date_to_str = request.args.get("date_to", "").strip()

    # Build base where clauses
    where_clauses = ["c.tenant_id = %s"]
    params = [tenant_id]
    
    if status_filter:
        where_clauses.append("c.status = %s")
        params.append(status_filter.lower())
    if search:
        search_like = f"%{search}%"
        where_clauses.append("(c.number ILIKE %s OR o.external_order_id ILIKE %s)")
        params.extend([search_like, search_like])
    if date_from_str:
        try:
            date_from = datetime.datetime.strptime(date_from_str, "%Y-%m-%d")
            where_clauses.append("c.started_at >= %s")
            params.append(date_from)
        except ValueError:
            pass
    if date_to_str:
        try:
            date_to = datetime.datetime.strptime(date_to_str, "%Y-%m-%d") + datetime.timedelta(days=1)
            where_clauses.append("c.started_at < %s")
            params.append(date_to)
        except ValueError:
            pass

    where_str = " AND ".join(where_clauses)
    
    def generate():
        headers = ["Date", "Time", "Order ID", "Phone", "Duration (sec)", "DTMF", "Result", "Charge (BDT)", "Recording URL"]
        si = StringIO()
        cw = csv.writer(si)
        cw.writerow(headers)
        yield si.getvalue()
        si.seek(0)
        si.truncate(0)

        query = f"""
            SELECT c.*, o.external_order_id, a.dtmf_digit, a.result as attempt_result
            FROM calls c
            LEFT JOIN orders o ON c.order_id = o.id
            LEFT JOIN order_call_attempts a ON c.id = a.call_id AND c.tenant_id = a.tenant_id
            WHERE {where_str}
            ORDER BY c.started_at DESC, c.id DESC
        """

        with db.transaction() as conn:
            # Create a server-side cursor for low-memory streaming
            cursor_name = f"cdr_export_{tenant_id}_{int(datetime.datetime.now().timestamp())}"
            with conn.cursor(name=cursor_name) as cur:
                cur.execute(query, tuple(params))
                for c in cur:
                    started_at = c.get("started_at")
                    date_val = started_at.strftime("%Y-%m-%d") if started_at else ""
                    time_val = started_at.strftime("%H:%M:%S") if started_at else ""
                    order_id = c.get("external_order_id") or "—"
                    phone_masked = mask_phone(c.get("number", ""))
                    duration = c.get("talk_seconds") or 0
                    
                    # DTMF
                    dtmf = c.get("dtmf_digit") or "—"
                    
                    # Result
                    result = c.get("status") or "—"
                    
                    # Charge — stored value only, no fallback calculation.
                    charge_val = c.get("charge") or Decimal("0.00")
                    charge_str = f"{float(charge_val):.2f}"
                    
                    # Recording URL
                    rec_path = c.get("recording_path")
                    rec_url = "—"
                    if rec_path:
                        filename_only = os.path.basename(rec_path)
                        rec_url = url_for("customer.serve_recording", filename=filename_only, _external=True)

                    cw.writerow([
                        date_val, time_val, order_id, phone_masked, duration, dtmf, result, charge_str, rec_url
                    ])
                    yield si.getvalue()
                    si.seek(0)
                    si.truncate(0)

    response = Response(stream_with_context(generate()), mimetype="text/csv")
    filename = f"cdr-{tenant_name}-{date_str}.csv"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response
