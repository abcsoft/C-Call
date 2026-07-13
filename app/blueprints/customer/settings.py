from __future__ import annotations

import json
import logging
import secrets
import hashlib
import hmac
import urllib.request
import urllib.error
import datetime
from typing import Any
from urllib.parse import urlparse

from flask import render_template, redirect, url_for, flash, request, jsonify, g, make_response
from flask_login import current_user

from app.blueprints.customer import customer_bp
from app.blueprints.auth.decorators import require_login, require_role
from app.models.tenant import get_tenant_by_id, update_tenant_settings
from app.db import db
from app.services.tts_service import generate_order_audio
from app.exceptions import TTSCredentialMissingError

logger = logging.getLogger("ccall")


def _list_elevenlabs_models(api_key: str) -> list[dict]:
    """Return the tenant's real available ElevenLabs TTS models straight from
    ElevenLabs' own API (GET /v1/models) — not a hardcoded guess. Lives here
    (not in call_engine/audio.py) because that file is read-only per
    CLAUDE.md and this is UI-only concern, mirroring list_elevenlabs_voices()."""
    if not api_key:
        raise ValueError("ElevenLabs API key is not configured.")
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/models",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        return [
            {
                "model_id": m.get("model_id", ""),
                "name": m.get("name", ""),
                "description": m.get("description", ""),
                "languages": [lang.get("name", "") for lang in (m.get("languages") or [])],
            }
            for m in data
            if m.get("can_do_text_to_speech")
        ]
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"ElevenLabs model list failed ({exc.code}): {body_text}") from exc
    except Exception as exc:
        raise RuntimeError(f"ElevenLabs model list failed: {exc}") from exc


@customer_bp.route("/integration", methods=["GET", "POST"])
@require_login
@require_role(["admin"])
def integration_panel():
    tenant_id = g.tenant_id
    tenant = get_tenant_by_id(tenant_id)
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}

    if request.method == "POST":
        # Handle saving webhook URL
        webhook_url = request.form.get("webhook_url", "").strip()
        if webhook_url:
            try:
                parsed = urlparse(webhook_url)
                if parsed.scheme != "https":
                    flash("Webhook URL must use HTTPS scheme.", "error")
                    return redirect(url_for("customer.integration_panel"))
            except Exception as e:
                flash(f"Invalid Webhook URL: {e}", "error")
                return redirect(url_for("customer.integration_panel"))
        
        settings["webhook_url"] = webhook_url
        update_tenant_settings(tenant_id, settings)
        flash("Webhook URL updated successfully.", "success")
        return redirect(url_for("customer.integration_panel"))

    api_keys = db.query(
        "SELECT id, name, key_prefix, created_at, last_used_at, is_active FROM api_keys WHERE tenant_id = %s ORDER BY created_at DESC",
        (tenant_id,)
    )
    
    # Check if a new webhook secret was generated in session (shown once)
    from flask import session
    webhook_secret_once = session.pop("webhook_secret_once", None)
    
    webhook_secret = settings.get("webhook_secret") or str(tenant["uuid"])

    return render_template(
        "customer/integration.html",
        api_keys=api_keys,
        settings=settings,
        webhook_secret=webhook_secret,
        webhook_secret_once=webhook_secret_once
    )

@customer_bp.route("/api/integration/keys", methods=["POST"])
@require_login
@require_role(["admin"])
def key_generate():
    """Generate a new secure API Key, deactivating/revoking any old ones."""
    tenant_id = g.tenant_id
    name = request.form.get("name", "Default Key").strip() or "Default Key"
    
    # 1. Generate API key: cc_live_ + 32-bytes token
    raw_key = f"cc_live_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    key_prefix = raw_key[:12]  # e.g., "cc_live_abc12"

    with db.transaction() as conn:
        # Revoke/delete existing keys first to enforce "Old key will stop working"
        conn.execute("DELETE FROM api_keys WHERE tenant_id = %s", (tenant_id,))
        
        # Insert new key
        conn.execute(
            """
            INSERT INTO api_keys (tenant_id, name, key_hash, key_prefix, is_active)
            VALUES (%s, %s, %s, %s, true)
            """,
            (tenant_id, name, key_hash, key_prefix)
        )

    from app.middleware.audit import audit_log
    audit_log("api_key_generate", current_user.id, "tenant", tenant_id, {"key_prefix": key_prefix})

    # Return key HTML fragment showing the key ONCE to the user
    return f"""
    <div class="p-4 bg-emerald-950/60 border border-emerald-500 rounded-xl text-emerald-300 mb-6 flex flex-col gap-2">
        <div class="flex items-center gap-2">
            <svg class="w-5 h-5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"></path></svg>
            <h4 class="font-bold text-sm">New API Key Generated!</h4>
        </div>
        <p class="text-xs text-emerald-400">Copy this key now. For security, it will not be shown again.</p>
        <div class="flex items-center justify-between gap-2 bg-black/40 p-2.5 rounded-xl font-mono border border-emerald-500/30 text-xs overflow-x-auto">
            <span class="select-all break-all">{raw_key}</span>
            <button onclick="navigator.clipboard.writeText('{raw_key}'); alert('API Key copied!');"
                    class="py-1 px-2.5 bg-emerald-500 text-slate-950 font-bold rounded-lg text-[10px] shrink-0 hover:bg-emerald-400 transition-colors">
                Copy
            </button>
        </div>
    </div>
    """

@customer_bp.route("/api/integration/keys/<int:key_id>", methods=["DELETE"])
@require_login
@require_role(["admin"])
def key_revoke(key_id: int):
    tenant_id = g.tenant_id
    db.execute("DELETE FROM api_keys WHERE id = %s AND tenant_id = %s", (key_id, tenant_id))

    from app.middleware.audit import audit_log
    audit_log("api_key_revoke", current_user.id, "tenant", tenant_id, {"key_id": key_id})

    return ""  # Remove from DOM via HTMX hx-swap="outerHTML"

@customer_bp.route("/api/integration/webhook-secret", methods=["GET"])
@require_login
@require_role(["admin"])
def get_webhook_secret():
    tenant_id = g.tenant_id
    tenant = get_tenant_by_id(tenant_id)
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
    secret = settings.get("webhook_secret") or ""
    if secret:
        masked = secret[:8] + "•" * (len(secret) - 8)
    else:
        masked = ""
    return jsonify({"secret": masked})

@customer_bp.route("/api/integration/webhook-secret/regenerate", methods=["POST"])
@require_login
@require_role(["admin"])
def regenerate_webhook_secret():
    """Regenerate the webhook secret, saving to settings."""
    tenant_id = g.tenant_id
    tenant = get_tenant_by_id(tenant_id)
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
            
    new_secret = f"whsec_{secrets.token_hex(32)}"
    settings["webhook_secret"] = new_secret
    update_tenant_settings(tenant_id, settings)
    
    from flask import session
    session["webhook_secret_once"] = new_secret
    
    flash("Webhook Secret regenerated successfully. Make sure to copy the new secret. Existing plugin integration will break until updated.", "warning")
    return redirect(url_for("customer.integration_panel"))

@customer_bp.route("/api/integration/webhook/secret", methods=["POST"])
@require_login
@require_role(["admin"])
def generate_webhook_secret():
    """Backward-compatible webhook secret regeneration route."""
    return regenerate_webhook_secret()

@customer_bp.route("/api/integration/webhook-test", methods=["GET", "POST"])
@require_login
@require_role(["admin"])
def webhook_test():
    """Send a test order confirmation webhook to the tenant's configured URL."""
    tenant_id = g.tenant_id
    tenant = get_tenant_by_id(tenant_id)
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}

    webhook_url = settings.get("webhook_url")
    if not webhook_url:
        return """
        <div class="p-3.5 bg-rose-950/60 border border-rose-500/80 rounded-xl text-rose-300 text-xs">
            <strong>Error:</strong> Webhook URL is not configured. Please configure and save a URL first.
        </div>
        """, 400

    secret = settings.get("webhook_secret") or str(tenant["uuid"])

    # Construct test payload
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "event": "order.confirmed",
        "order_id": "TEST-1001",
        "internal_id": 999999,
        "order_status": "CONFIRMED",
        "dtmf_digit": "1",
        "customer_name": "Test Customer",
        "phone": "017****1234",
        "amount": "1500.00",
        "talk_seconds": 15,
        "attempt_no": 1,
        "confirmed_at": timestamp,
        "timestamp": timestamp
    }

    # Sign raw payload
    payload_json_str = json.dumps(payload, sort_keys=True)
    signature = f"sha256={hmac.new(secret.encode('utf-8'), payload_json_str.encode('utf-8'), hashlib.sha256).hexdigest()}"

    # Sign payload inside body for backwards compatibility
    payload_with_sig = dict(payload)
    payload_with_sig["signature"] = signature
    payload_json_str_with_sig = json.dumps(payload_with_sig, sort_keys=True)

    # Log attempt in DB
    from app.models.webhook_log import create_log, update_log
    log_id = create_log({
        "tenant_id": tenant_id,
        "order_id": None,
        "url": webhook_url,
        "payload": payload_with_sig,
        "status_code": None,
        "response_body": None,
        "attempt_no": 1,
        "success": False
    })

    req_body = payload_json_str_with_sig.encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=req_body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "C-Call-Webhook-Test/1.0",
            "X-CCAll-Signature": signature
        },
        method="POST"
    )

    status_code = None
    response_body = ""
    success = False
    start_time = datetime.datetime.now()

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status_code = resp.status
            response_body = resp.read(1000).decode("utf-8", errors="ignore")[:500]
            if 200 <= status_code < 300:
                success = True
    except urllib.error.HTTPError as err:
        status_code = err.code
        try:
            response_body = err.read(1000).decode("utf-8", errors="ignore")[:500]
        except Exception:
            response_body = str(err)[:500]
    except Exception as exc:
        status_code = 0
        response_body = str(exc)[:500]

    elapsed = (datetime.datetime.now() - start_time).total_seconds()
    update_log(log_id, status_code, response_body, success)

    if success:
        return f"""
        <div class="p-3.5 bg-emerald-950/60 border border-emerald-500 rounded-xl text-emerald-300 text-xs flex flex-col gap-1.5">
            <span class="font-bold flex items-center gap-1.5 text-emerald-400">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>
                Webhook Sent Successfully!
            </span>
            <div><strong>Status Code:</strong> <span class="font-mono">{status_code}</span></div>
            <div><strong>Response Time:</strong> <span class="font-mono">{elapsed:.2f}s</span></div>
            <div class="mt-1">
                <strong>Response Body:</strong>
                <pre class="bg-black/30 p-2 rounded border border-emerald-500/20 font-mono mt-1 text-[10px] overflow-x-auto max-h-24 select-all">{response_body}</pre>
            </div>
        </div>
        """
    else:
        return f"""
        <div class="p-3.5 bg-rose-950/60 border border-rose-500/80 rounded-xl text-rose-300 text-xs flex flex-col gap-1.5">
            <span class="font-bold flex items-center gap-1.5 text-rose-400">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
                Webhook Delivery Failed!
            </span>
            <div><strong>Status Code:</strong> <span class="font-mono">{status_code if status_code is not None else "Connection Error"}</span></div>
            <div><strong>Response Time:</strong> <span class="font-mono">{elapsed:.2f}s</span></div>
            <div class="mt-1">
                <strong>Error / Response:</strong>
                <pre class="bg-black/30 p-2 rounded border border-rose-500/20 font-mono mt-1 text-[10px] overflow-x-auto max-h-24 select-all">{response_body}</pre>
            </div>
        </div>
        """

@customer_bp.route("/script", methods=["GET"])
@require_login
@require_role(["admin"])
def voice_script():
    tenant_id = g.tenant_id
    tenant = get_tenant_by_id(tenant_id)
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}

    from services.edge_tts_service import EDGE_VOICES
    from call_engine.audio import GEMINI_VOICES
    from app.services.tts_service import DEFAULT_VOICE_SCRIPT
    from app.models.tenant_secrets import get_secret_meta
    from app.models.plan_tts_policy import get_plan_tts_policy
    from app.models.tenant_tts_usage import get_total_monthly_characters
    from app.models.platform_secrets import platform_secret_is_active
    from app.extensions import redis_ext

    flows = db.query("SELECT id, name FROM flows WHERE tenant_id = %s ORDER BY name ASC", (tenant_id,))

    # Strip any leftover plaintext keys from settings before passing to template
    safe_settings = {k: v for k, v in settings.items() if k not in ("gemini_api_key", "elevenlabs_api_key")}
    gemini_key_meta    = get_secret_meta(tenant_id, "gemini_api_key")
    elevenlabs_key_meta = get_secret_meta(tenant_id, "elevenlabs_api_key")

    # Plan policy and credential mode
    tts_policy = get_plan_tts_policy(tenant.get("plan_id"))
    tts_mode = tenant.get("tts_credential_mode") or "byok"

    # Platform provider availability (metadata only — no keys)
    platform_available = {
        p: platform_secret_is_active(p)
        for p in (tts_policy.get("platform_managed_providers") or [])
    }

    # Monthly quota tracking for platform_managed mode
    monthly_quota = tts_policy.get("monthly_character_quota")
    monthly_used = 0
    if tts_mode == "platform_managed":
        for p in (tts_policy.get("platform_managed_providers") or []):
            monthly_used += get_total_monthly_characters(tenant_id, p, "platform_managed")
    monthly_remaining = max(0, monthly_quota - monthly_used) if monthly_quota is not None else None

    # Daily preview usage via Redis (use _client to avoid raise on None)
    today_str = datetime.date.today().isoformat()
    daily_key = f"daily_preview:{tenant_id}:{today_str}"
    try:
        _rc = redis_ext._client
        _raw = _rc.get(daily_key) if _rc is not None else None
        preview_today = int(_raw) if _raw else 0
    except Exception:
        preview_today = 0
    daily_preview_limit = tts_policy.get("daily_preview_limit")

    return render_template(
        "customer/script.html",
        settings=safe_settings,
        edge_voices=EDGE_VOICES,
        gemini_voices=GEMINI_VOICES,
        flows=flows,
        default_voice_script=DEFAULT_VOICE_SCRIPT,
        gemini_key_meta=gemini_key_meta,
        elevenlabs_key_meta=elevenlabs_key_meta,
        tts_policy=tts_policy,
        tts_mode=tts_mode,
        platform_available=platform_available,
        monthly_quota=monthly_quota,
        monthly_used=monthly_used,
        monthly_remaining=monthly_remaining,
        preview_today=preview_today,
        daily_preview_limit=daily_preview_limit,
    )

@customer_bp.route("/api/settings/voices", methods=["GET", "POST"])
@require_login
def api_settings_voices():
    """Return dropdown options for the given provider."""
    provider = request.args.get("provider") or request.args.get("tts_provider") or "edge"
    tenant_id = g.tenant_id
    tenant = get_tenant_by_id(tenant_id)
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}

    options_html = ""
    if provider == "edge":
        from services.edge_tts_service import EDGE_VOICES
        current_voice = settings.get("edge_tts_voice", "bn-BD-NabanitaNeural")
        for v in EDGE_VOICES:
            options_html += f'<option value="{v}" {"selected" if v == current_voice else ""}>{v}</option>'
    elif provider == "gemini":
        from call_engine.audio import GEMINI_VOICES
        current_voice = settings.get("gemini_voice_name", "Puck")
        for v in GEMINI_VOICES:
            options_html += f'<option value="{v}" {"selected" if v == current_voice else ""}>{v}</option>'
    elif provider == "gtts":
        current_lang = settings.get("tts_lang", "bn")
        options_html += f'<option value="bn" {"selected" if current_lang == "bn" else ""}>Bangla (Default)</option>'
        options_html += f'<option value="en" {"selected" if current_lang == "en" else ""}>English</option>'
    elif provider == "elevenlabs":
        # Key resolution (BYOK only — never fall back to os.environ).
        # An unsaved key typed live in the form is passed via POST body only
        # (never GET query string). If absent, load from tenant_secrets.
        from app.models.tenant_secrets import get_secret_plaintext
        api_key = request.form.get("elevenlabs_api_key", "").strip() or get_secret_plaintext(tenant_id, "elevenlabs_api_key") or ""
        current_voice = settings.get("elevenlabs_voice_id", "")
        
        voices = []
        fetch_error = None
        if api_key:
            try:
                from call_engine.audio import list_elevenlabs_voices
                voices = list_elevenlabs_voices(api_key)
            except Exception as e:
                # Previously this was only logger.warning()'d and silently
                # fell back to the placeholder list below — the admin had no
                # way to tell "no key set" apart from "key fetch failed" from
                # the dropdown alone. Surface it as a disabled option instead.
                logger.warning(f"Failed to fetch ElevenLabs voices: {e}")
                fetch_error = str(e)

        if voices:
            # "premade"/"default" category voices are far more likely to work
            # on a free-tier API key — voices pulled into the account from
            # the Voice Library ("cloned"/community, usually category
            # "generated" or owned-by-someone-else) return 402 on free plan.
            # Sort premade/default first so the admin sees the safer choices
            # without having to hunt through the list.
            free_friendly = {"premade", "default"}
            voices_sorted = sorted(voices, key=lambda v: 0 if v.get("category") in free_friendly else 1)
            for v in voices_sorted:
                tag = "✅ likely free-plan OK" if v.get("category") in free_friendly else "⚠ Library/cloned — may need paid plan"
                options_html += f'<option value="{v["voice_id"]}" {"selected" if v["voice_id"] == current_voice else ""}>{v["name"]} ({v["category"]}) — {tag}</option>'
        else:
            if fetch_error:
                safe_err = fetch_error.replace("<", "&lt;").replace(">", "&gt;")[:200]
                options_html += f'<option value="" disabled selected>⚠ Could not load your voices: {safe_err}</option>'
            elif not api_key:
                options_html += '<option value="" disabled selected>⚠ No ElevenLabs API key configured — showing placeholder voices</option>'
            # Fallback placeholder options
            placeholder_voices = [
                ("21m00Tcm4TlvDq8ikWAM", "Rachel (Standard)"),
                ("AZnzlk1XvdvUeBnXmlld", "Domi (Standard)"),
                ("EXAVITQu4vr4xnSDxMaL", "Bella (Standard)")
            ]
            for vid, name in placeholder_voices:
                options_html += f'<option value="{vid}" {"selected" if vid == current_voice else ""}>{name}</option>'

    return options_html

@customer_bp.route("/api/settings/elevenlabs-models", methods=["GET", "POST"])
@require_login
def api_settings_elevenlabs_models():
    """Return real ElevenLabs model <option>s fetched live from ElevenLabs'
    own /v1/models endpoint. Key loaded from tenant_secrets (BYOK only)."""
    from app.models.tenant_secrets import get_secret_plaintext
    tenant_id = g.tenant_id
    tenant = get_tenant_by_id(tenant_id)
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}

    # Accept unsaved key from POST body only (never GET query string)
    api_key = request.form.get("elevenlabs_api_key", "").strip() or get_secret_plaintext(tenant_id, "elevenlabs_api_key") or ""
    current_model = settings.get("elevenlabs_model", "eleven_v3")

    models = []
    fetch_error = None
    if api_key:
        try:
            models = _list_elevenlabs_models(api_key)
        except Exception as e:
            logger.warning(f"Failed to fetch ElevenLabs models: {e}")
            fetch_error = str(e)

    options_html = ""
    if models:
        for m in models:
            langs = ", ".join(m["languages"][:3]) if m["languages"] else ""
            label = f'{m["name"]} — {langs}' if langs else m["name"]
            options_html += f'<option value="{m["model_id"]}" {"selected" if m["model_id"] == current_model else ""}>{label}</option>'
    else:
        if fetch_error:
            safe_err = fetch_error.replace("<", "&lt;").replace(">", "&gt;")[:200]
            options_html += f'<option value="" disabled selected>⚠ Could not load real models: {safe_err}</option>'
        elif not api_key:
            options_html += '<option value="" disabled selected>⚠ No API key — showing known model IDs (not live-verified)</option>'
        # Fallback — these ARE genuine real ElevenLabs model IDs (verified against
        # ElevenLabs' public docs), just not confirmed live against this account.
        fallback_models = [
            ("eleven_multilingual_v2", "Eleven Multilingual v2"),
            ("eleven_turbo_v2_5", "Eleven Turbo v2.5"),
            ("eleven_v3", "Eleven v3 (alpha)"),
        ]
        for mid, name in fallback_models:
            options_html += f'<option value="{mid}" {"selected" if mid == current_model else ""}>{name}</option>'

    return options_html

@customer_bp.route("/api/settings/script", methods=["POST"])
@require_login
@require_role(["admin"])
def save_script_settings():
    tenant_id = g.tenant_id
    
    voice_script = request.form.get("voice_script", "").strip()
    if not voice_script:
        flash("Voice script template cannot be empty.", "error")
        return redirect(url_for("customer.voice_script"))

    tts_provider = request.form.get("tts_provider", "edge")
    edge_tts_voice = request.form.get("edge_tts_voice", "bn-BD-NabanitaNeural")
    gemini_voice_name = request.form.get("gemini_voice_name", "Puck")
    elevenlabs_voice_id = request.form.get("elevenlabs_voice_id", "")
    elevenlabs_model = request.form.get("elevenlabs_model", "").strip()
    tts_lang = request.form.get("tts_lang", "bn")
    speaking_rate = request.form.get("speaking_rate", "normal")
    language = request.form.get("language", "Bangla")
    # API keys: blank = "leave current key unchanged". Non-blank = save encrypted.
    # Keys are stored in tenant_secrets, never in tenants.settings JSON.
    gemini_api_key = request.form.get("gemini_api_key", "").strip()
    elevenlabs_api_key = request.form.get("elevenlabs_api_key", "").strip()

    order_call_flow_id = request.form.get("order_call_flow_id", "").strip()

    # ── Server-side plan enforcement on provider ──────────────────────────────
    from app.models.plan_tts_policy import get_plan_tts_policy
    from app.models.tenant import get_tenant_by_id as _get_tenant_for_plan
    _t = _get_tenant_for_plan(tenant_id) or {}
    _policy = get_plan_tts_policy(_t.get("plan_id"))
    if tts_provider not in _policy["allowed_providers"]:
        flash(f"Provider '{tts_provider}' is not available on your current plan.", "error")
        return redirect(url_for("customer.voice_script"))

    # Reject platform_managed mode if plan is byok_only
    _requested_mode = request.form.get("tts_credential_mode", "").strip()
    if _requested_mode and _requested_mode not in ("byok", "platform_managed"):
        flash("Invalid credential mode.", "error")
        return redirect(url_for("customer.voice_script"))
    if _requested_mode == "platform_managed" and _policy["credential_policy"] == "byok_only":
        flash("Platform-managed TTS is not available on your current plan.", "error")
        return redirect(url_for("customer.voice_script"))

    # Validate BYOK key exists for providers that require it
    _effective_mode = _requested_mode or (_t.get("tts_credential_mode") or "byok")
    if _effective_mode == "byok" and tts_provider in ("gemini", "elevenlabs"):
        _key_field = "gemini_api_key" if tts_provider == "gemini" else "elevenlabs_api_key"
        _submitted_key = request.form.get(_key_field, "").strip()
        if not _submitted_key:
            from app.models.tenant_secrets import get_secret_meta as _gsm
            if not _gsm(tenant_id, _key_field):
                flash("API key is required for this provider.", "error")
                return redirect(url_for("customer.voice_script"))

    # Save credential mode to tenants table and audit-log changes
    if _requested_mode:
        if _requested_mode == "platform_managed":
            _pm_providers = _policy.get("platform_managed_providers") or []
            from app.models.platform_secrets import platform_secret_is_active as _psa
            if not any(_psa(p) for p in _pm_providers):
                flash("Platform-managed TTS is temporarily unavailable — no active platform credentials configured.", "error")
                return redirect(url_for("customer.voice_script"))
        _old_mode = _t.get("tts_credential_mode") or "byok"
        db.execute(
            "UPDATE tenants SET tts_credential_mode = %s WHERE id = %s",
            (_requested_mode, tenant_id)
        )
        if _old_mode != _requested_mode:
            from app.middleware.audit import audit_log
            audit_log(
                "tenant_tts_mode_changed",
                current_user.id,
                "tenant",
                tenant_id,
                {"old_mode": _old_mode, "new_mode": _requested_mode, "source": "customer_self_service"},
            )

    # Update non-secret settings
    update_dict = {
        "voice_script": voice_script,
        "tts_provider": tts_provider,
        "speaking_rate": speaking_rate,
        "language": language,
        "order_call_flow_id": int(order_call_flow_id) if order_call_flow_id else None
    }

    if tts_provider == "edge":
        update_dict["edge_tts_voice"] = edge_tts_voice
    elif tts_provider == "gemini":
        update_dict["gemini_voice_name"] = gemini_voice_name
    elif tts_provider == "gtts":
        update_dict["tts_lang"] = tts_lang
    elif tts_provider == "elevenlabs":
        update_dict["elevenlabs_voice_id"] = elevenlabs_voice_id
        if elevenlabs_model:
            update_dict["elevenlabs_model"] = elevenlabs_model

    update_tenant_settings(tenant_id, update_dict)

    # Save API keys encrypted in tenant_secrets (never in tenants.settings)
    from app.models.tenant_secrets import save_secret
    if gemini_api_key:
        save_secret(tenant_id, "gemini_api_key", gemini_api_key)
        logger.info("Tenant %s updated gemini_api_key", tenant_id)
    if elevenlabs_api_key:
        save_secret(tenant_id, "elevenlabs_api_key", elevenlabs_api_key)
        logger.info("Tenant %s updated elevenlabs_api_key", tenant_id)

    # Regenerate Order Confirmation Flow root node audio with the new
    # TTS provider/voice so Quick Call and retries immediately use the
    # updated voice — without waiting for the next order API call.
    try:
        from app.db import db as _db
        from app.services.tts_service import generate_order_audio as _gen
        flow = _db.query_one(
            "SELECT root_node_id FROM flows WHERE tenant_id = %s AND name = 'Order Confirmation Flow'",
            (tenant_id,)
        )
        if flow and flow["root_node_id"]:
            # Use a representative fake order so template variables render
            from app.models.tenant import get_tenant_by_id as _get_t
            import json as _json
            _t = _get_t(tenant_id)
            _s = _t.get("settings") or {}
            if isinstance(_s, str):
                _s = _json.loads(_s)
            _fake = {
                "customer_name": "গ্রাহক", "external_order_id": "PREVIEW",
                "amount": "1000.00", "phone": "01700000000", "items": "পণ্য",
                "site_name": _t.get("business_name", "আমাদের স্টোর"),
                "product_name": "পণ্য", "price": "1000.00",
                "delivery_charge": "60.00", "grand_total": "1060.00",
            }
            new_audio = _gen(tenant_id, _fake)
            _db.execute(
                "UPDATE flow_nodes SET prompt_audio = %s WHERE id = %s",
                (new_audio, flow["root_node_id"])
            )
            logger.info(f"Flow root node audio refreshed for tenant {tenant_id} after settings save: {new_audio}")
    except Exception as _e:
        logger.warning(f"Could not refresh flow audio after settings save (tenant {tenant_id}): {_e}")

    flash("Voice script settings updated successfully.", "success")
    return redirect(url_for("customer.voice_script"))

@customer_bp.route("/api/settings/tts-quota", methods=["GET"])
@require_login
def api_tts_quota():
    """Return current TTS quota summary as JSON for the customer quota card."""
    tenant_id = g.tenant_id
    from app.models.tenant import get_tenant_by_id as _get_t
    from app.models.plan_tts_policy import get_plan_tts_policy
    from app.services.tts_usage_service import get_tenant_tts_quota_summary

    _tenant = _get_t(tenant_id) or {}
    tts_mode = _tenant.get("tts_credential_mode") or "byok"

    # Quota summary is only meaningful for platform_managed mode.
    if tts_mode != "platform_managed":
        return jsonify({"mode": tts_mode, "quota_applicable": False})

    settings = _tenant.get("settings") or {}
    if isinstance(settings, str):
        import json as _json
        try:
            settings = _json.loads(settings)
        except Exception:
            settings = {}
    provider = settings.get("tts_provider", "edge")

    plan_policy = get_plan_tts_policy(_tenant.get("plan_id"))
    summary = get_tenant_tts_quota_summary(tenant_id, provider, plan_policy)
    summary["mode"] = tts_mode
    summary["quota_applicable"] = True
    # Decimal → float for JSON serialisation
    for k, v in summary.items():
        from decimal import Decimal as _D
        if isinstance(v, _D):
            summary[k] = float(v)
    return jsonify(summary)


@customer_bp.route("/api/settings/tts-preview", methods=["POST"])
@require_login
@require_role(["admin"])
def api_tts_preview():
    """Generates audio for script template preview using form parameters. Returns HTML audio tag."""
    tenant_id = g.tenant_id
    script = request.form.get("voice_script", "").strip()

    if not script:
        return "<span class='text-red-500 text-xs font-semibold'>Script template is empty.</span>", 400

    if len(script) > 500:
        return "<span class='text-red-500 text-xs font-semibold'>Script template exceeds 500 characters limit.</span>", 400

    # Read preview values or form selections to generate without needing to save
    tts_provider = request.form.get("tts_provider", "edge")
    edge_tts_voice = request.form.get("edge_tts_voice", "bn-BD-NabanitaNeural")
    gemini_voice_name = request.form.get("gemini_voice_name", "Puck")
    elevenlabs_voice_id = request.form.get("elevenlabs_voice_id", "")
    elevenlabs_model = request.form.get("elevenlabs_model", "").strip()
    tts_lang = request.form.get("tts_lang", "bn")

    settings_override = {
        "tts_provider": tts_provider,
        "edge_tts_voice": edge_tts_voice,
        "gemini_voice_name": gemini_voice_name,
        "elevenlabs_voice_id": elevenlabs_voice_id,
        "tts_lang": tts_lang,
        "_purpose": "preview",  # signals entitlement resolver this is a preview
    }
    if elevenlabs_model:
        settings_override["elevenlabs_model"] = elevenlabs_model
    # Only override the saved API key if the admin actually typed something in
    # the (password) field for this preview request — an empty value here
    # would otherwise blank out an already-saved key for this one call.
    gemini_api_key = request.form.get("gemini_api_key", "").strip()
    elevenlabs_api_key = request.form.get("elevenlabs_api_key", "").strip()
    if gemini_api_key:
        settings_override["gemini_api_key"] = gemini_api_key
    if elevenlabs_api_key:
        settings_override["elevenlabs_api_key"] = elevenlabs_api_key

    # Replace variables with static values for preview using render_order_voice_script
    from app.services.tts_service import render_order_voice_script
    from app.models.tenant import get_tenant_by_id as _get_tenant
    _tenant = _get_tenant(tenant_id) or {}

    preview_order = {
        "customer_name": "সাকিব হাসান",
        "external_order_id": "WC-1005",
        "amount": 970,
        "metadata": {
            "store_name": "দেশী ফ্যাশন",
            "product_name": "একটি পাঞ্জাবি",
            "products": [{"name": "একটি পাঞ্জাবি", "qty": 1, "total": "850"}],
            "price": "850",
            "subtotal": "850",
            "delivery_charge": "120",
            "grand_total": "970",
            "payment_method": "ক্যাশ অন ডেলিভারি",
            "callback_number": "09602111380"
        }
    }
    preview_text = render_order_voice_script(script, preview_order, _tenant)

    # Allowlist of valid provider IDs — reject arbitrary values from form
    VALID_PROVIDERS = {"edge", "gemini", "elevenlabs", "gtts", "openvoice"}
    if tts_provider not in VALID_PROVIDERS:
        return "<span class='text-rose-400 text-xs font-semibold'>Invalid TTS provider selected.</span>", 422

    # ── Daily preview rate limit: INCR before generation (not after) ──────────
    # Increment counter first so concurrent requests can't burst past the limit.
    # DB fallback: if Redis is unavailable, load count from tts_usage_reservations.
    from app.models.plan_tts_policy import get_plan_tts_policy
    _plan_id = (_tenant or {}).get("plan_id")
    _policy = get_plan_tts_policy(_plan_id)
    daily_limit: int = _policy.get("daily_preview_limit", 10)

    _preview_counter_incremented = False
    _daily_key = f"daily_preview:{tenant_id}:{datetime.date.today().isoformat()}"
    try:
        from app.extensions import redis_ext
        _redis = redis_ext._client
        if _redis is not None:
            _count = _redis.incr(_daily_key)
            _redis.expire(_daily_key, 90000)  # ~25h TTL
            _preview_counter_incremented = True
            if _count > daily_limit:
                _redis.decr(_daily_key)
                _preview_counter_incremented = False
                return (
                    f"<span class='text-rose-400 text-xs font-semibold'>"
                    f"Daily preview limit ({daily_limit}) reached. Resets at midnight.</span>",
                    402,
                )
    except Exception as _re:
        logger.warning("Redis daily preview counter unavailable; falling back to DB: %s", _re)
        # DB fallback: count today's preview reservations for this tenant
        try:
            from app.db import db as _db
            _today = datetime.date.today()
            _db_row = _db.query_one(
                "SELECT COUNT(*) AS cnt FROM tts_usage_reservations "
                "WHERE tenant_id = %s AND purpose = 'preview' AND created_at::date = %s",
                (tenant_id, _today),
            )
            _db_count = int(_db_row["cnt"]) if _db_row else 0
            if _db_count >= daily_limit:
                return (
                    f"<span class='text-rose-400 text-xs font-semibold'>"
                    f"Daily preview limit ({daily_limit}) reached. Resets at midnight.</span>",
                    402,
                )
        except Exception as _dbe:
            logger.warning("DB daily preview fallback also failed: %s", _dbe)
            # Fail open — allow preview to proceed

    start_time = datetime.datetime.now()
    try:
        audio_filename = generate_order_audio(tenant_id, preview_text, settings_override=settings_override)
        elapsed = (datetime.datetime.now() - start_time).total_seconds()

        voice_desc = ""
        if tts_provider == "edge":
            voice_desc = edge_tts_voice
        elif tts_provider == "gemini":
            voice_desc = gemini_voice_name
        elif tts_provider == "gtts":
            voice_desc = "Bangla" if tts_lang == "bn" else "English"
        elif tts_provider == "elevenlabs":
            voice_desc = elevenlabs_voice_id[:8] if elevenlabs_voice_id else "(default)"

        provider_names = {
            "edge": "Edge TTS",
            "gemini": "Gemini TTS",
            "gtts": "Google Translate TTS",
            "elevenlabs": "ElevenLabs",
            "openvoice": "OpenVoice",
        }
        provider_desc = provider_names.get(tts_provider, tts_provider)
        model_desc = (elevenlabs_model or "eleven_v3") if tts_provider == "elevenlabs" else ""

        return render_template(
            "customer/_audio_preview.html",
            filename=audio_filename,
            elapsed=elapsed,
            provider_desc=provider_desc,
            voice_desc=voice_desc,
            model_desc=model_desc,
        )

    except TTSCredentialMissingError as exc:
        provider_label = {"elevenlabs": "ElevenLabs", "gemini": "Gemini"}.get(exc.provider, exc.provider)
        logger.warning("TTS preview credential missing: provider=%s tenant=%s", exc.provider, tenant_id)
        return (
            f"<div class='text-amber-400 text-xs font-semibold space-y-1'>"
            f"<p>&#9888; {provider_label} API key not configured for this account.</p>"
            f"<p class='text-slate-400 font-normal'>Save your {provider_label} API key above, then try again.</p>"
            f"</div>",
            422,
        )
    except Exception as exc:
        from app.exceptions import TTSProviderNotAllowedError, TTSCredentialModeError, TTSQuotaExceededError
        if isinstance(exc, TTSProviderNotAllowedError):
            logger.warning("TTS preview provider not allowed: provider=%s tenant=%s", tts_provider, tenant_id)
            return f"<span class='text-rose-400 text-xs font-semibold'>Provider '{tts_provider}' is not available on your plan.</span>", 403
        if isinstance(exc, TTSCredentialModeError):
            logger.warning("TTS preview mode not allowed: mode=%s tenant=%s", exc.mode, tenant_id)
            return f"<span class='text-rose-400 text-xs font-semibold'>Credential mode '{exc.mode}' is not allowed on your plan.</span>", 403
        if isinstance(exc, TTSQuotaExceededError):
            logger.warning("TTS preview quota exceeded: tenant=%s provider=%s", tenant_id, tts_provider)
            # Quota denial is valid — do NOT release the daily counter
            return "<span class='text-rose-400 text-xs font-semibold'>Monthly TTS quota exhausted. Quota resets on the 1st of next month.</span>", 402
        # Infrastructure failure — release the daily counter so user can retry
        if _preview_counter_incremented:
            try:
                from app.extensions import redis_ext as _rex
                _rc = _rex._client
                if _rc is not None:
                    _rc.decr(_daily_key)
            except Exception:
                pass
        logger.error("TTS Preview failed: %s", exc, exc_info=True)
        safe_msg = str(exc)[:120].replace("<", "&lt;").replace(">", "&gt;")
        return f"<span class='text-rose-400 text-xs font-semibold'>TTS Preview failed: {safe_msg}</span>", 422

@customer_bp.route("/api/integration/test-order", methods=["POST"])
@require_login
@require_role(["admin", "agent"])
def test_order():
    phone = request.form.get("phone", "").strip()
    customer_name = request.form.get("customer_name", "").strip() or "Test Buyer"
    amount_str = request.form.get("amount", "100").strip()
    product_name = request.form.get("product_name", "Test Item").strip() or "Test Item"
    
    try:
        from app.services.order_service import create_order
        tenant_id = g.tenant_id
        tenant = get_tenant_by_id(tenant_id)
        # Validate and queue call
        order_data = {
            "order_id": f"TEST-{secrets.token_hex(4).upper()}",
            "phone": phone,
            "customer_name": customer_name,
            "amount": amount_str,
            "product_name": product_name,
            "store_name": tenant.get("business_name") if tenant else "Test Store"
        }
        order = create_order(tenant_id, order_data)
        return f"""
        <div class="p-3.5 bg-emerald-950/60 border border-emerald-500 rounded-xl text-emerald-300 text-xs">
            <strong>Success:</strong> Order created and call queued! Internal ID: <span class="font-mono font-bold">{order['id']}</span>. Call is being placed to {phone}...
        </div>
        """
    except Exception as e:
        return f"""
        <div class="p-3.5 bg-rose-950/60 border border-rose-500/80 rounded-xl text-rose-300 text-xs">
            <strong>Error:</strong> {str(e)}
        </div>
        """


@customer_bp.route("/settings", methods=["GET", "POST"])
@require_login
@require_role(["admin"])
def settings_page():
    tenant_id = g.tenant_id
    tenant = get_tenant_by_id(tenant_id)
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}

    if request.method == "POST":
        enable_sms_fallback = request.form.get("enable_sms_fallback") == "true"
        smslayer_api_key = request.form.get("smslayer_api_key", "").strip()
        sms_template = request.form.get("sms_template", "").strip()
        
        # Validation
        if enable_sms_fallback and not sms_template:
            flash("SMS template is required when SMS Fallback is enabled.", "error")
            return redirect(url_for("customer.settings_page"))
            
        settings["enable_sms_fallback"] = enable_sms_fallback
        settings["smslayer_api_key"] = smslayer_api_key
        settings["sms_template"] = sms_template
        
        update_tenant_settings(tenant_id, settings)
        flash("SMS settings updated successfully.", "success")
        return redirect(url_for("customer.settings_page"))

    return render_template(
        "customer/settings.html",
        settings=settings
    )


@customer_bp.route("/api/settings/tts-key/<provider>/remove", methods=["POST"])
@require_login
@require_role(["admin"])
def remove_tts_key(provider: str):
    """Remove a BYOK TTS API key for the given provider. Does not affect other settings."""
    from app.models.plan_tts_policy import KEY_REQUIRED_PROVIDERS
    if provider not in KEY_REQUIRED_PROVIDERS:
        return "<span class='text-rose-400 text-xs font-semibold'>Invalid provider.</span>", 422
    tenant_id = g.tenant_id
    from app.models.tenant_secrets import delete_secret
    delete_secret(tenant_id, f"{provider}_api_key")
    from app.middleware.audit import audit_log
    audit_log("tenant_tts_key_removed", current_user.id, "tenant", tenant_id, {"provider": provider})
    label = {"gemini": "Gemini", "elevenlabs": "ElevenLabs"}.get(provider, provider.capitalize())
    return f"<span class='text-emerald-400 text-xs font-semibold'>{label} API key removed successfully.</span>"


@customer_bp.route("/settings/subscription", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def subscription_page():
    """Read-only subscription overview — plan, TTS limits, and quota usage."""
    tenant_id = g.tenant_id
    tenant = get_tenant_by_id(tenant_id)
    from app.models.plan_tts_policy import get_plan_tts_policy
    from app.models.tenant_tts_usage import get_total_monthly_characters
    from app.models.platform_secrets import platform_secret_is_active

    plan = db.query_one("SELECT * FROM plans WHERE id = %s", (tenant.get("plan_id"),)) if tenant.get("plan_id") else None
    tts_policy = get_plan_tts_policy(tenant.get("plan_id"))
    tts_mode = tenant.get("tts_credential_mode") or "byok"

    monthly_quota = tts_policy.get("monthly_character_quota")
    monthly_used = 0
    if tts_mode == "platform_managed":
        for p in (tts_policy.get("platform_managed_providers") or []):
            monthly_used += get_total_monthly_characters(tenant_id, p, "platform_managed")

    platform_available = {
        p: platform_secret_is_active(p)
        for p in (tts_policy.get("platform_managed_providers") or [])
    }

    return render_template(
        "customer/subscription.html",
        tenant=tenant,
        plan=plan,
        tts_policy=tts_policy,
        tts_mode=tts_mode,
        monthly_quota=monthly_quota,
        monthly_used=monthly_used,
        platform_available=platform_available,
    )


@customer_bp.route("/api/settings/test-sms", methods=["POST"])
@require_login
@require_role(["admin"])
def test_sms():
    phone = request.form.get("phone", "").strip()
    if not phone:
        return """
        <div class="p-3.5 bg-rose-950/60 border border-rose-500/80 rounded-xl text-rose-300 text-xs">
            <strong>Error:</strong> Phone number is required for test SMS.
        </div>
        """, 400
        
    tenant_id = g.tenant_id
    # Construct a dummy test message
    message = "C-Call Test SMS Fallback. If you receive this, your SMS gateway configuration is working correctly."
    
    from app.services.sms_service import send_sms
    res = send_sms(phone, message, tenant_id)
    
    if res.get("success"):
        return f"""
        <div class="p-3.5 bg-emerald-950/60 border border-emerald-500 rounded-xl text-emerald-300 text-xs">
            <strong>Success:</strong> Test SMS sent successfully! Message ID: <span class="font-mono font-bold">{res.get('message_id')}</span>
        </div>
        """
    else:
        err_msg = res.get("message") or "Unknown error"
        return f"""
        <div class="p-3.5 bg-rose-950/60 border border-rose-500/80 rounded-xl text-rose-300 text-xs">
            <strong>Error:</strong> Failed to send SMS. Details: {err_msg}
        </div>
        """

