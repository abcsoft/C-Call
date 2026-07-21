from __future__ import annotations

import csv
import json
import logging
import datetime
import secrets
import hashlib
from io import StringIO
from decimal import Decimal
from flask import render_template, redirect, url_for, flash, request, jsonify, g, make_response, send_file
from flask_login import current_user
from app.blueprints.customer import customer_bp
from app.blueprints.auth.decorators import require_login, require_role
from app.models.tenant import get_tenant_by_id, update_tenant_settings
from app.models.order import get_orders, get_orders_count, get_order_by_id, get_call_attempts_for_order
from app.models.wallet import get_wallet, get_transactions, get_transactions_count, credit_balance, update_low_balance_threshold
from app.db import db
from app.services.order_service import manual_recall
from app.services.tts_service import generate_order_audio
from app.services.dashboard_service import get_dashboard_stats, get_chart_data

logger = logging.getLogger("ccall")

@customer_bp.context_processor
def inject_tenant_info():
    from app.services.admin_service import is_impersonation_active
    from flask import session

    context: dict = {
        "impersonation_active": is_impersonation_active(),
        "impersonation_target_tenant_id": session.get("_impersonation_target_tenant_id"),
    }

    if current_user and current_user.is_authenticated and not current_user.is_super_admin:
        tenant_id = current_user.tenant_id
        if tenant_id:
            tenant = get_tenant_by_id(tenant_id)
            wallet = get_wallet(tenant_id)
            balance = Decimal(str(wallet.get("balance", "0"))) if wallet else Decimal("0.0")
            context.update({
                "current_tenant": tenant,
                "current_balance": balance,
            })
    return context


@customer_bp.route("/", methods=["GET"])
def index():
    return redirect(url_for("customer.dashboard"))

@customer_bp.route("/dashboard", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def dashboard():
    tenant_id = g.tenant_id
    from app.services.subscription_access_service import get_tenant_status_banner, check_feature_access
    tenant_row = db.query_one("SELECT status FROM tenants WHERE id = %s", (tenant_id,))
    tenant_status = tenant_row["status"] if tenant_row else "active"

    # Restricted mode: past_due and expired block new operations in UI
    restricted_states = ("past_due", "expired")
    restricted_mode = tenant_status in restricted_states
    subscription_banner = get_tenant_status_banner(tenant_id)

    stats = get_dashboard_stats(tenant_id)

    wallet = get_wallet(tenant_id) or {}
    balance = stats["wallet_balance"]
    low_balance = balance < Decimal(str(wallet.get("low_balance_threshold") or 50))

    # Check if integrated: has active API keys
    api_key_count = db.query_one(
        "SELECT COUNT(*) FROM api_keys WHERE tenant_id = %s AND is_active = true",
        (tenant_id,)
    )
    not_integrated = int(api_key_count["count"]) == 0 if api_key_count else True

    flows = db.query("SELECT id, name FROM flows WHERE tenant_id = %s ORDER BY name ASC", (tenant_id,))

    failed_caller_id_query = db.query_one(
        "SELECT COUNT(*) FROM orders WHERE tenant_id = %s AND order_status = 'FAILED' AND metadata->>'failure_reason' LIKE '%%caller ID%%'",
        (tenant_id,)
    )
    failed_caller_id = int(failed_caller_id_query["count"]) > 0 if failed_caller_id_query else False

    # TTS widget context
    from app.models.plan_tts_policy import get_plan_tts_policy
    from app.models.platform_secrets import platform_secret_is_active
    _tenant = get_tenant_by_id(tenant_id) or {}
    tts_mode = _tenant.get("tts_credential_mode") or "byok"
    tts_policy = get_plan_tts_policy(_tenant.get("plan_id"))
    monthly_quota = tts_policy.get("monthly_character_quota")
    platform_managed_providers = tts_policy.get("platform_managed_providers") or []
    platform_available = {p: platform_secret_is_active(p) for p in platform_managed_providers}

    return render_template(
        "customer/dashboard.html",
        stats=stats,
        wallet=wallet,
        balance=balance,
        low_balance=low_balance,
        not_integrated=not_integrated,
        flows=flows,
        failed_caller_id=failed_caller_id,
        tts_mode=tts_mode,
        tts_policy=tts_policy,
        monthly_quota=monthly_quota,
        platform_available=platform_available,
        subscription_banner=subscription_banner,
        restricted_mode=restricted_mode,
        tenant_status=tenant_status,
    )

@customer_bp.route("/api/dashboard/stats", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def api_dashboard_stats():
    tenant_id = g.tenant_id
    stats = get_dashboard_stats(tenant_id)
    
    # If HTMX request, render only the KPI cards template block
    if request.headers.get("HX-Request"):
        wallet = get_wallet(tenant_id) or {}
        balance = stats["wallet_balance"]
        low_balance = balance < Decimal(str(wallet.get("low_balance_threshold") or 50))
        return render_template(
            "customer/_dashboard_kpis.html",
            stats=stats,
            balance=balance,
            low_balance=low_balance
        )
        
    # Return JSON otherwise
    serialized_stats = {
        "today_orders": stats["today_orders"],
        "today_confirmed": stats["today_confirmed"],
        "today_cancelled": stats["today_cancelled"],
        "today_no_answer": stats["today_no_answer"],
        "today_pending_retry": stats["today_pending_retry"],
        "today_unreachable": stats["today_unreachable"],
        "wallet_balance": float(stats["wallet_balance"]),
        "success_rate_7d": stats["success_rate_7d"],
        "calls_today": stats["calls_today"],
        "minutes_today": stats["minutes_today"],
        "cost_today": float(stats["cost_today"])
    }
    return jsonify(serialized_stats)

@customer_bp.route("/api/dashboard/chart", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def api_dashboard_chart():
    tenant_id = g.tenant_id
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7
    chart_data = get_chart_data(tenant_id, days=days)
    return jsonify(chart_data)

@customer_bp.route("/dashboard/quick-call", methods=["POST"])
@require_login
@require_role(["admin", "agent"])
def quick_call():
    tenant_id = g.tenant_id
    phone = request.form.get("phone", "").strip()
    call_type = request.form.get("call_type", "flow")
    flow_id_str = request.form.get("flow_id")
    message_text = request.form.get("message_text", "").strip()
    
    try:
        if not phone:
            raise ValueError("Phone number is required.")
            
        from app.services.number_service import validate_pool_phone, select_caller_id, get_sip_trunk_for_caller_id, get_sip_provider_id_for_caller_id
        normalized_phone = validate_pool_phone(phone)
        caller_id = select_caller_id(tenant_id)
        sip_override = get_sip_trunk_for_caller_id(caller_id) if caller_id else None
        sip_gateway_id = get_sip_provider_id_for_caller_id(caller_id) if caller_id else None
        
        # Check wallet balance
        from app.services.rate_service import estimate_charge
        from app.services.wallet_service import check_balance
        min_charge = estimate_charge(tenant_id)
        if not check_balance(tenant_id, min_charge):
            raise ValueError("Insufficient wallet balance to place call.")

        # Resolve immutable rate snapshot before SIP dispatch
        from app.services.rate_service import resolve_billing_policy
        _billing_policy = resolve_billing_policy(tenant_id, use_cache=False)
        _rate_snapshot = _billing_policy.as_dict()

        from call_engine import call_runner
        if call_type == "flow":
            if not flow_id_str:
                default_flow = db.query_one("SELECT id FROM flows WHERE tenant_id = %s LIMIT 1", (tenant_id,))
                if not default_flow:
                    raise ValueError("No voice flow configured. Please create a flow first.")
                flow_id = default_flow["id"]
            else:
                flow_id = int(flow_id_str)

            call_id = call_runner.start_call(
                number=normalized_phone,
                flow_id=flow_id,
                tenant_id=tenant_id,
                caller_id=caller_id,
                sip_override=sip_override,
                sip_gateway_id=sip_gateway_id,
                rate_snapshot=_rate_snapshot,
            )
        else:
            if not message_text:
                raise ValueError("Message text is required for direct TTS.")

            from app.services.tts_service import generate_order_audio
            audio_name = generate_order_audio(tenant_id, message_text)

            call_id = call_runner.start_direct_call(
                number=normalized_phone,
                audio_name=audio_name,
                repeat_count=1,
                message_text=message_text,
                audio_mode="tts",
                tenant_id=tenant_id,
                caller_id=caller_id,
                sip_override=sip_override,
                sip_gateway_id=sip_gateway_id,
                rate_snapshot=_rate_snapshot,
            )
            
        return f"""
        <div class="p-3 bg-emerald-950/60 border border-emerald-500 rounded-xl text-emerald-300 text-xs">
            <strong>Success:</strong> Quick call dispatched! Call ID: <span class="font-mono font-bold">{call_id}</span>
        </div>
        """
    except Exception as e:
        return f"""
        <div class="p-3 bg-rose-950/60 border border-rose-500/80 rounded-xl text-rose-300 text-xs">
            <strong>Error:</strong> {str(e)}
        </div>
        """
@customer_bp.route("/live-calls", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def live_calls_page():
    return render_template("customer/live_calls.html")


@customer_bp.route("/api/live-calls", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def api_live_calls():
    from app.services.live_call_service import list_live_calls, get_summary
    tenant_id = g.tenant_id
    calls = list_live_calls(tenant_id=tenant_id)
    return jsonify({"success": True, "calls": calls, "summary": get_summary(tenant_id=tenant_id)})


@customer_bp.route("/api/debug/live-registry", methods=["GET"])
@require_login
def api_debug_live_registry():
    """TEMP debug endpoint — remove after live-call-monitor investigation."""
    from call_engine import active_calls as registry
    import call_engine.call_runner as call_runner_mod
    tenant_id = g.tenant_id
    all_entries = registry.list_active_calls(tenant_id=None)
    return jsonify({
        "count": len(all_entries),
        "calls": all_entries,
        "call_runner_file": call_runner_mod.__file__,
        "tenant_id": tenant_id,
        "registry_total_count": len(all_entries),
        "registry_all_call_ids": [e["call_id"] for e in all_entries],
        "registry_all_calls_raw": all_entries,
        "registry_scoped_to_tenant": registry.list_active_calls(tenant_id=tenant_id),
    })


@customer_bp.route("/api/live-calls/<int:call_id>/hangup", methods=["POST"])
@require_login
@require_role(["admin", "agent"])
def api_live_calls_hangup(call_id: int):
    from app.services.live_call_service import hangup_call
    tenant_id = g.tenant_id
    ok = hangup_call(call_id, tenant_id=tenant_id)
    if not ok:
        return jsonify({"success": False, "error": "Call not active or already ended"}), 404
    return jsonify({"success": True})


@customer_bp.route("/recordings/<filename>")
@require_login
def serve_recording(filename: str):
    """Serve call recording WAV files from the instance/recordings directory, validating tenant ownership."""
    tenant_id = g.tenant_id
    call = db.query_one(
        "SELECT id FROM calls WHERE tenant_id = %s AND recording_path LIKE %s",
        (tenant_id, f"%{filename}")
    )
    if not call:
        logger.warning("Tenant %s attempted to access unauthorized recording: %s", tenant_id, filename)
        return "Unauthorized", 403

    import os
    from flask import send_from_directory, current_app
    recordings_dir = os.path.abspath(os.path.join(current_app.root_path, "../instance/recordings"))
    if not os.path.exists(recordings_dir):
        recordings_dir = os.path.abspath("instance/recordings")
        
    return send_from_directory(recordings_dir, filename)


@customer_bp.route("/recordings/play", methods=["GET"])
def play_recording():
    """Verify signed URL and serve recording file from disk."""
    path = request.args.get("path")
    sig = request.args.get("sig")
    exp = request.args.get("exp")

    if not path or not sig or not exp:
        return "Missing signed URL parameters", 400

    from app.services.recording_service import verify_signed_url
    if not verify_signed_url(path, sig, exp):
        return "Access Denied: Invalid or expired signature", 403

    import os
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return "File not found", 404

    as_attachment = request.args.get("download") == "1"
    filename = os.path.basename(abs_path)

    return send_file(
        abs_path,
        mimetype="audio/wav",
        as_attachment=as_attachment,
        download_name=filename
    )



@customer_bp.route("/sounds/<filename>")
def serve_sound(filename: str):
    """Serve ulaw audio files as wav for browser playback on the fly!"""
    import subprocess
    sound_path = f"instance/sounds/{filename}"
    if not sound_path.endswith(".ulaw"):
        sound_path += ".ulaw"
        
    # Check if file exists
    import os
    if not os.path.exists(sound_path):
        return "Audio file not found", 404
        
    # Convert ulaw to wav on the fly using ffmpeg
    wav_path = sound_path.replace(".ulaw", ".wav")
    
    ffmpeg_path = os.environ.get("FFMPEG_PATH", "ffmpeg")
    try:
        subprocess.run(
            [ffmpeg_path, "-y", "-f", "mulaw", "-ar", "8000", "-i", sound_path, wav_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        return make_response(open(wav_path, "rb").read(), 200, {"Content-Type": "audio/wav"})
    except Exception as e:
        logger.error(f"FFmpeg conversion failed: {e}")
        return "Conversion failed", 500

# /retry and /api/retry-rules now live in app/blueprints/customer/retry.py
# (registered via app/blueprints/customer/__init__.py import-for-side-effect).




