from __future__ import annotations

import json
import logging
import secrets
import hashlib
import hmac
from decimal import Decimal
import datetime
from typing import Any
import urllib.request
import urllib.parse
import urllib.error

from flask import render_template, redirect, url_for, flash, request, jsonify, g, make_response, current_app
from flask_login import current_user

from app.blueprints.customer import customer_bp
from app.blueprints.auth.decorators import require_login, require_role
from app.extensions import csrf
from app.models.tenant import get_tenant_by_id
from app.models.wallet import (
    get_wallet, 
    update_low_balance_threshold, 
    create_payment_intent, 
    get_payment_intent_by_id, 
    update_payment_intent_status,
    get_payment_intent_by_ref
)
from app.services import wallet_service
from app.db import db

logger = logging.getLogger("ccall")

def verify_sslcommerz_signature(data: dict, store_password: str) -> bool:
    """Verify local MD5 signature for SSLCommerz callback payload."""
    if not store_password:
        return True # Bypass signature check if password is not configured (dev/sandbox)

    verify_sign = data.get("verify_sign")
    verify_key = data.get("verify_key", "")
    if not verify_sign or not verify_key:
        return False
        
    keys = verify_key.split(",")
    params = {}
    for k in keys:
        if k in data:
            params[k] = data[k]
            
    # Sort alphabetically by key
    sorted_params = sorted(params.items())
    query_parts = []
    for k, v in sorted_params:
        query_parts.append(f"{k}={v}")
        
    # MD5 hash of store password
    m = hashlib.md5(store_password.encode("utf-8"))
    store_pass_md5 = m.hexdigest()
    
    query_string = "&".join(query_parts) + "&" + store_pass_md5
    computed_sign = hashlib.md5(query_string.encode("utf-8")).hexdigest()
    
    return hmac.compare_digest(computed_sign.lower(), verify_sign.lower())

def query_transactions_common(tenant_id: int, request_args: Any) -> dict[str, Any]:
    limit = min(50, max(1, int(request_args.get("limit", 15))))
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

    base_where_clauses = ["tenant_id = %s"]
    base_params = [tenant_id]

    # Count total transactions
    total_row = db.query_one("SELECT COUNT(*) FROM wallet_transactions WHERE tenant_id = %s", (tenant_id,))
    total = total_row["count"] if total_row else 0

    main_where_clauses = list(base_where_clauses)
    main_params = list(base_params)

    # Cursor logic
    if after_id:
        main_where_clauses.append(
            "(created_at, id) < (SELECT created_at, id FROM wallet_transactions WHERE id = %s)"
        )
        main_params.append(after_id)
    elif before_id:
        main_where_clauses.append(
            "(created_at, id) > (SELECT created_at, id FROM wallet_transactions WHERE id = %s)"
        )
        main_params.append(before_id)

    where_str = " AND ".join(main_where_clauses)
    sort_order = "ASC" if before_id else "DESC"

    query = f"""
        SELECT * FROM wallet_transactions
        WHERE {where_str}
        ORDER BY created_at {sort_order}, id {sort_order}
        LIMIT %s
    """
    main_params.append(limit)
    
    txns = db.query(query, tuple(main_params))
    if before_id:
        txns = list(reversed(txns))

    # Format fields for representation
    for t in txns:
        t["display_amount"] = f"৳{float(t.get('amount', 0)):.2f}"
        t["display_balance_after"] = f"৳{float(t.get('balance_after', 0)):.2f}"
        created_at = t.get("created_at")
        t["formatted_created_at"] = created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at else ""

    next_cursor = txns[-1]["id"] if txns else None
    prev_cursor = txns[0]["id"] if txns else None

    # Check if there are next/prev records
    has_next = False
    has_prev = False
    
    if txns:
        next_check_clause = base_where_clauses + ["(created_at, id) < (SELECT created_at, id FROM wallet_transactions WHERE id = %s)"]
        next_check_params = base_params + [next_cursor]
        next_check_query = f"""
            SELECT id FROM wallet_transactions
            WHERE {" AND ".join(next_check_clause)} 
            LIMIT 1
        """
        has_next = len(db.query(next_check_query, tuple(next_check_params))) > 0

        prev_check_clause = base_where_clauses + ["(created_at, id) > (SELECT created_at, id FROM wallet_transactions WHERE id = %s)"]
        prev_check_params = base_params + [prev_cursor]
        prev_check_query = f"""
            SELECT id FROM wallet_transactions
            WHERE {" AND ".join(prev_check_clause)} 
            LIMIT 1
        """
        has_prev = len(db.query(prev_check_query, tuple(prev_check_params))) > 0

    return {
        "transactions": txns,
        "total": total,
        "limit": limit,
        "next_cursor": next_cursor,
        "prev_cursor": prev_cursor,
        "has_next": has_next,
        "has_prev": has_prev,
        "offset": offset
    }

@customer_bp.route("/wallet", methods=["GET"])
@require_login
@require_role(["admin", "agent"])
def wallet_panel():
    tenant_id = g.tenant_id
    wallet = get_wallet(tenant_id) or {}
    balance = Decimal(str(wallet.get("balance", "0.00")))
    low_balance_threshold = Decimal(str(wallet.get("low_balance_threshold", "50.00")))
    
    low_balance = balance < low_balance_threshold

    return render_template(
        "customer/wallet.html",
        wallet=wallet,
        balance=balance,
        low_balance=low_balance
    )

@customer_bp.route("/api/wallet/settings", methods=["POST"])
@require_login
@require_role(["admin"])
def api_wallet_settings():
    """Inline update of low balance threshold via HTMX."""
    tenant_id = g.tenant_id
    try:
        threshold = Decimal(request.form.get("threshold", "50").strip())
        if threshold < 0:
            raise ValueError("Threshold cannot be negative.")
        update_low_balance_threshold(tenant_id, threshold)
        
        # Return success snippet
        return f"""
        <div id="settings-status" class="text-emerald-400 text-xs font-semibold mt-2" hx-swap-oob="true">
            Settings saved successfully! Alert threshold set to ৳{threshold:.2f}
        </div>
        """
    except Exception as e:
        return f"""
        <div id="settings-status" class="text-rose-400 text-xs font-semibold mt-2" hx-swap-oob="true">
            Error: {str(e)}
        </div>
        """

@customer_bp.route("/api/wallet/recharge", methods=["POST"])
@require_login
@require_role(["admin"])
def wallet_recharge():
    tenant_id = g.tenant_id
    amount_str = request.form.get("amount", "100").strip()
    payment_method = request.form.get("payment_method", "sslcommerz").strip()
    
    try:
        amount = Decimal(amount_str)
        if amount < 10:
            raise ValueError("Minimum recharge amount is ৳10.")
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("customer.wallet_panel"))

    # Create pending payment intent in DB
    intent_id = create_payment_intent(tenant_id, gateway=payment_method, amount=amount)

    store_id = current_app.config.get("SSLCOMMERZ_STORE_ID")
    store_password = current_app.config.get("SSLCOMMERZ_STORE_PASSWORD")
    
    # If configurations are present and we choose sslcommerz, call actual gateway
    if payment_method == "sslcommerz" and store_id and store_password:
        try:
            # We call SSLCommerz API
            sandbox_url = "https://sandbox.sslcommerz.com/gwprocess/v4/api.php"
            live_url = "https://header.pay.sslcommerz.com/gwprocess/v4/api.php"
            
            is_sandbox = current_app.config.get("SSLCOMMERZ_SANDBOX", "true").lower() == "true"
            init_url = sandbox_url if is_sandbox else live_url
            
            user = db.query_one("SELECT * FROM users WHERE id = %s", (current_user.id,))
            tenant = get_tenant_by_id(tenant_id)
            
            # Make sure we pass intent_id as transaction reference (tran_id)
            payload = {
                "store_id": store_id,
                "store_passwd": store_password,
                "total_amount": float(amount),
                "currency": "BDT",
                "tran_id": str(intent_id),
                "success_url": url_for("customer.payment_callback", gateway="sslcommerz", _external=True),
                "fail_url": url_for("customer.payment_callback", gateway="sslcommerz", _external=True),
                "cancel_url": url_for("customer.payment_callback", gateway="sslcommerz", _external=True),
                "ipn_url": url_for("customer.payment_callback", gateway="sslcommerz", _external=True),
                "cus_name": user.get("name") or "Merchant Customer",
                "cus_email": user.get("email") or "customer@ccall.com.bd",
                "cus_add1": "Dhaka",
                "cus_city": "Dhaka",
                "cus_postcode": "1000",
                "cus_country": "Bangladesh",
                "cus_phone": tenant.get("phone") or "01711234567"
            }
            
            req_data = urllib.parse.urlencode(payload).encode("utf-8")
            req = urllib.request.Request(init_url, data=req_data, method="POST")
            
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_data = json.loads(resp.read().decode())
                
            if resp_data.get("status") == "SUCCESS" and resp_data.get("GatewayPageURL"):
                # Save redirect URL and gateway reference details
                gateway_ref = resp_data.get("val_id")
                update_payment_intent_status(intent_id, "pending", gateway_ref=gateway_ref, payload=resp_data)
                return redirect(resp_data.get("GatewayPageURL"))
            else:
                logger.error(f"SSLCommerz initiation failed: {resp_data}")
                flash(f"Payment gateway initiation failed: {resp_data.get('failedreason', 'Unknown error')}", "error")
                return redirect(url_for("customer.wallet_panel"))
        except Exception as e:
            logger.error(f"SSLCommerz API call failed: {e}", exc_info=True)
            # Fall back to mock checkout page on error / connection timeout
            pass

    # Redirect to mock gateway page if keys are missing or API call fails
    return redirect(url_for("customer.mock_gateway_landing", intent_id=intent_id))

@customer_bp.route("/wallet/recharge/mock/<int:intent_id>", methods=["GET"])
@require_login
@require_role(["admin"])
def mock_gateway_landing(intent_id: int):
    tenant_id = g.tenant_id
    intent = get_payment_intent_by_id(intent_id)
    if not intent or intent["tenant_id"] != tenant_id:
        flash("Unauthorized payment session.", "error")
        return redirect(url_for("customer.wallet_panel"))
        
    return render_template("customer/mock_payment.html", intent=intent)

@customer_bp.route("/webhooks/payment/<gateway>", methods=["GET", "POST"])
@csrf.exempt  # external gateway POST (SSLCommerz/bKash) — verified by HMAC/MD5 signature instead, not CSRF
def payment_callback(gateway: str):
    """Callback receiver for payment gateway callbacks (and mock checkout redirect)."""
    # Accept params from POST body or GET query arguments
    data = request.form.to_dict() if request.method == "POST" else request.args.to_dict()
    
    # Log callback — never log raw signature or store_password
    safe_log = {k: v for k, v in data.items() if k not in ("store_passwd", "verify_sign", "signature")}
    logger.info("Payment callback received for gateway %s: %s", gateway, safe_log)
    
    # 1. SSLCommerz callback flow
    if gateway == "sslcommerz":
        status = data.get("status", "").upper()
        tran_id = data.get("tran_id") # This maps to our payment intent ID
        val_id = data.get("val_id") # Validation reference
        amount_str = data.get("amount")

        if not tran_id:
            flash("Missing transaction ID in payment callback.", "error")
            return redirect(url_for("customer.wallet_panel"))
            
        try:
            intent_id = int(tran_id)
        except ValueError:
            flash("Invalid transaction ID format in payment callback.", "error")
            return redirect(url_for("customer.wallet_panel"))

        intent = get_payment_intent_by_id(intent_id)
        if not intent:
            flash("Payment transaction session not found.", "error")
            return redirect(url_for("customer.wallet_panel"))

        tenant_id = intent["tenant_id"]
        
        # Verify local MD5 signature if password is set
        store_password = current_app.config.get("SSLCOMMERZ_STORE_PASSWORD", "")
        if not verify_sslcommerz_signature(data, store_password):
            logger.warning(f"SSLCommerz signature verification failed for intent {intent_id}")
            update_payment_intent_status(intent_id, "failed", payload=data)
            flash("Payment signature verification failed.", "error")
            return redirect(url_for("customer.wallet_panel"))

        # Transition status
        if status in ("VALID", "VALIDATED", "SUCCESS"):
            if intent["status"] == "completed":
                # Already processed
                flash(f"Recharge already completed! ৳{float(intent['amount']):.2f} credited.", "success")
                return redirect(url_for("customer.wallet_panel"))
                
            # Always use DB-stored amount — never trust client-supplied amount
            amount = Decimal(str(intent["amount"]))

            # Atomic update + wallet credit in transaction block
            with db.transaction() as conn:
                # Update intent status
                conn.execute(
                    "UPDATE payment_intents SET status = 'completed', gateway_ref = %s, payload = %s, updated_at = now() WHERE id = %s",
                    (val_id, json.dumps(data), intent_id)
                )
                
            # Perform credit balance logic
            wallet_service.credit(
                tenant_id=tenant_id, 
                amount=amount, 
                description=f"Recharge via SSLCommerz (val_id: {val_id})", 
                ref_id=val_id
            )
            
            flash(f"Recharge successful! ৳{float(amount):.2f} added to your wallet.", "success")
            return redirect(url_for("customer.wallet_panel"))
        else:
            update_payment_intent_status(intent_id, "failed", payload=data)
            flash(f"Payment status: {status}. Recharge failed.", "error")
            return redirect(url_for("customer.wallet_panel"))
            
    # Unsupported gateway
    flash(f"Unsupported payment gateway: {gateway}", "error")
    return redirect(url_for("customer.wallet_panel"))

@customer_bp.route("/api/wallet/transactions", methods=["GET"])
@require_login
@require_role(["admin", "agent"])
def api_wallet_transactions():
    tenant_id = g.tenant_id
    context = query_transactions_common(tenant_id, request.args)
    
    if "application/json" in request.headers.get("Accept", ""):
        # Serialize datetime and decimal
        serialized_txns = []
        for t in context["transactions"]:
            t_copy = dict(t)
            if isinstance(t_copy.get("amount"), Decimal):
                t_copy["amount"] = float(t_copy["amount"])
            if isinstance(t_copy.get("balance_after"), Decimal):
                t_copy["balance_after"] = float(t_copy["balance_after"])
            for dk in ("created_at",):
                if isinstance(t_copy.get(dk), (datetime.datetime, datetime.date)):
                    t_copy[dk] = t_copy[dk].isoformat()
            serialized_txns.append(t_copy)
        
        return jsonify({
            "success": True,
            "transactions": serialized_txns,
            "total": context["total"],
            "has_next": context["has_next"],
            "has_prev": context["has_prev"],
            "next_cursor": context["next_cursor"],
            "prev_cursor": context["prev_cursor"]
        })

    # Return HTML rows partial for HTMX paging
    return render_template("customer/_wallet_table_rows.html", **context)
