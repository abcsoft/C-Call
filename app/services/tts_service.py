from __future__ import annotations

import hashlib
import ipaddress
import os
import json
import logging
import re
import subprocess
import tempfile
import urllib.request
import urllib.error
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from app.models.tenant import get_tenant_by_id
from app.exceptions import TTSCredentialMissingError
from call_engine.audio import create_tts_audio
from app.monitoring import TTS_DURATION

logger = logging.getLogger("ccall")

# ── New Bangla default template with full e-commerce variables ────────────────
DEFAULT_VOICE_SCRIPT = (
    "আসসালামু আলাইকুম। আমি {site_name} থেকে বলছি। "
    "আপনি {product_name} অর্ডার করেছেন। "
    "{product_name}-এর মূল্য {price} টাকা এবং ডেলিভারি চার্জ {delivery_charge} টাকা। "
    "সর্বমোট পরিশোধযোগ্য মূল্য {grand_total} টাকা। "
    "আপনার অর্ডারটি নিশ্চিত করতে ১ চাপুন অথবা বাতিল করতে ২ চাপুন।"
)

# All supported template variables with their resolution paths
TEMPLATE_VARIABLES = [
    "site_name", "store_name", "customer_name", "order_id",
    "product_name", "products", "product_price", "price",
    "subtotal", "delivery_charge", "grand_total", "amount",
    "payment_method", "callback_number",
]


def render_order_voice_script(template: str, order: dict, tenant: dict) -> str:
    """Replace {var} and {{var}} placeholders with order/tenant values.

    Supports both single-brace ``{var}`` and double-brace ``{{var}}`` syntax.
    All numeric values are formatted as integer strings (no decimals) for
    natural TTS pronunciation.
    """
    metadata = order.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}

    def _fmt_amount(val) -> str:
        """Format a numeric value as an integer string for TTS (e.g. '1200')."""
        if val is None:
            return "0"
        try:
            return str(int(float(str(val).strip())))
        except (ValueError, TypeError):
            return str(val)

    # Resolve all variables with fallback chains
    store_name = (
        metadata.get("store_name")
        or order.get("store_name")
        or tenant.get("business_name")
        or "আমাদের স্টোর"
    )

    customer_name = order.get("customer_name") or ""
    order_id_val = order.get("external_order_id") or order.get("order_id") or ""

    product_name = (
        metadata.get("product_name")
        or order.get("product_name")
        or metadata.get("items")
        or order.get("items")
        or ""
    )

    # Products: join array if present
    products_val = metadata.get("products") or order.get("products")
    if products_val and isinstance(products_val, list):
        products_str = ", ".join(
            p.get("name", "") if isinstance(p, dict) else str(p)
            for p in products_val
        )
    else:
        products_str = product_name

    price = _fmt_amount(
        metadata.get("price") or order.get("price") or order.get("amount")
    )
    product_price = _fmt_amount(
        metadata.get("product_price")
        or metadata.get("price")
        or order.get("product_price")
        or order.get("price")
        or order.get("amount")
    )
    subtotal = _fmt_amount(
        metadata.get("subtotal")
        or order.get("subtotal")
        or metadata.get("price")
        or order.get("price")
        or order.get("amount")
    )
    delivery_charge = _fmt_amount(
        metadata.get("delivery_charge") or order.get("delivery_charge") or "0"
    )
    grand_total = _fmt_amount(
        metadata.get("grand_total") or order.get("grand_total") or order.get("amount")
    )
    amount = _fmt_amount(order.get("amount"))
    payment_method = (
        metadata.get("payment_method") or order.get("payment_method") or ""
    )
    callback_number = (
        metadata.get("callback_number")
        or order.get("callback_number")
        or order.get("caller_id")
        or ""
    )

    replacements = {
        "site_name": store_name,
        "store_name": store_name,
        "customer_name": customer_name,
        "order_id": str(order_id_val),
        "product_name": product_name,
        "products": products_str,
        "product_price": product_price,
        "price": price,
        "subtotal": subtotal,
        "delivery_charge": delivery_charge,
        "grand_total": grand_total,
        "amount": amount,
        "payment_method": payment_method,
        "callback_number": callback_number,
    }

    text = template
    for key, val in replacements.items():
        # Replace both {{key}} and {key}
        text = text.replace("{{" + key + "}}", val)
        text = text.replace("{" + key + "}", val)

    return text


# ── Custom Audio Download ─────────────────────────────────────────────────────

def _is_private_ip(hostname: str) -> bool:
    """Check if a hostname resolves to a private or loopback IP (SSRF guard)."""
    try:
        import socket
        for info in socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
                return True
    except Exception:
        return True  # If we can't resolve, treat as private
    return False


def download_custom_audio(url: str, tenant_id: int) -> str | None:
    """Download audio from an HTTPS URL and convert to 8kHz mono μ-law WAV.

    Safety guards:
    - HTTPS only
    - No private/loopback IPs (SSRF protection)
    - Max 10MB file size
    - 10 second timeout
    - Content-Type must be audio/*
    - ffmpeg conversion to match the call engine pipeline

    Returns the output filename on success, None on failure.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            logger.warning(f"Rejected custom_audio_url (not HTTPS): {url[:80]}")
            return None

        if _is_private_ip(parsed.hostname):
            logger.warning(f"Rejected custom_audio_url (private IP): {url[:80]}")
            return None

        req = urllib.request.Request(url, headers={
            "User-Agent": "C-Call-AudioFetch/1.0",
            "Accept": "audio/*",
        })

        with urllib.request.urlopen(req, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("audio/"):
                logger.warning(f"Rejected custom_audio_url (bad content-type: {content_type}): {url[:80]}")
                return None

            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > 10 * 1024 * 1024:
                logger.warning(f"Rejected custom_audio_url (too large: {content_length}): {url[:80]}")
                return None

            # Download to temp file (streaming with size limit)
            data = bytearray()
            max_bytes = 10 * 1024 * 1024
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    logger.warning(f"Aborted custom_audio_url download (exceeded 10MB): {url[:80]}")
                    return None

        if not data:
            return None

        # Write to temp file
        url_hash = hashlib.sha256(f"{tenant_id}:{url}".encode()).hexdigest()[:16]
        from pathlib import Path
        audio_dir = Path(__file__).resolve().parent.parent.parent / "instance" / "sounds"
        audio_dir.mkdir(parents=True, exist_ok=True)

        # Determine source extension from content-type
        ext_map = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/ogg": ".ogg",
                    "audio/x-wav": ".wav", "audio/mp4": ".m4a", "audio/aac": ".aac"}
        src_ext = ext_map.get(content_type.split(";")[0].strip(), ".audio")

        src_path = audio_dir / f"custom_{url_hash}{src_ext}"
        out_path = audio_dir / f"custom_{url_hash}.ulaw"

        with open(src_path, "wb") as f:
            f.write(data)

        # Convert to 8kHz mono μ-law using ffmpeg
        ffmpeg_path = os.environ.get("FFMPEG_PATH", "ffmpeg")
        cmd = [
            ffmpeg_path, "-y", "-i", str(src_path),
            "-ar", "8000", "-ac", "1", "-acodec", "pcm_mulaw",
            "-f", "wav", str(out_path)
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            logger.error(f"ffmpeg conversion failed for custom audio: {result.stderr[:200]}")
            return None

        # Clean up source file
        try:
            src_path.unlink()
        except Exception:
            pass

        return f"custom_{url_hash}"

    except urllib.error.URLError as e:
        logger.warning(f"Failed to download custom_audio_url: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error downloading custom audio: {e}")
        return None


# ── Main TTS Generation ──────────────────────────────────────────────────────

def generate_order_audio(tenant_id: int, order_data: dict[str, Any] | str, settings_override: dict[str, Any] | None = None) -> str:
    """Generate TTS audio for the given order_data using tenant script template and TTS settings.
    If order_data is a string, it is treated as pre-parsed text (for backward compatibility).
    Returns the filename of the generated audio (e.g., 'ivr-abc123xyz').

    Priority chain for audio source:
    1. order.custom_audio_url → download and convert external audio
    2. order.custom_text → use as literal TTS text (bypass tenant template)
    3. tenant voice_script template → render with order variables
    """
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        raise ValueError(f"Tenant {tenant_id} not found")

    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}

    if settings_override:
        settings.update(settings_override)

    if isinstance(order_data, str):
        # Backward-compatible pre-parsed text string (e.g., from preview route)
        text = order_data
    else:
        # Priority 1: Custom audio URL (download and convert)
        custom_audio_url = order_data.get("custom_audio_url")
        if custom_audio_url:
            audio_file = download_custom_audio(custom_audio_url, tenant_id)
            if audio_file:
                logger.info(f"Using custom audio URL for tenant {tenant_id}")
                return audio_file
            # Fall through to TTS if download failed

        # Priority 2: Custom text override (bypass tenant template)
        # Priority 2: Custom text override (bypass tenant template)
        custom_text = order_data.get("custom_text")
        if custom_text:
            text = render_order_voice_script(custom_text, order_data, tenant)
        else:
            # Priority 3: Tenant voice script template with variable rendering
            script_template = settings.get("voice_script") or DEFAULT_VOICE_SCRIPT
            text = render_order_voice_script(script_template, order_data, tenant)

    # [SAAS STEP 6] TTS script generated
    logger.info(f"[SAAS STEP 6] TTS script generated: {text}")

    # Extract tenant settings
    provider = settings.get("tts_provider", "edge")
    lang = settings.get("tts_lang", "bn")
    voice_name = settings.get("gemini_voice_name", "Puck")
    voice_id = settings.get("elevenlabs_voice_id", "")
    elevenlabs_model = settings.get("elevenlabs_model", "eleven_v3")
    gemini_model = settings.get("gemini_model", "gemini-2.5-flash-preview-tts")
    edge_voice = settings.get("edge_tts_voice", "bn-BD-NabanitaNeural")
    openvoice_voice = settings.get("openvoice_voice", "")

    # Cache key: embed tenant_id + model so per-tenant and per-model cache is distinct.
    model_tag = ""
    if provider == "elevenlabs" and elevenlabs_model:
        model_tag = "‌" + elevenlabs_model
    elif provider == "gemini" and gemini_model:
        model_tag = "‌" + gemini_model
    text_with_tenant = text + ("​" * tenant_id) + model_tag

    # ── Entitlement check via central resolver ────────────────────────────────
    # settings_override BYOK path: unsaved key typed live in preview form takes priority.
    from app.services.tts_entitlement_service import (
        resolve_tts_entitlement,
        resolve_internal_tts_credential,
        TENANT_CREDENTIAL_MISSING,
        PROVIDER_NOT_ALLOWED,
        CREDENTIAL_MODE_NOT_ALLOWED,
        MONTHLY_TTS_QUOTA_EXCEEDED,
    )
    from app.exceptions import (
        TTSProviderNotAllowedError,
        TTSCredentialModeError,
        TTSQuotaExceededError,
    )

    purpose = (settings_override or {}).get("_purpose", "production")
    if purpose not in ("preview", "production"):
        purpose = "production"

    entitlement = resolve_tts_entitlement(tenant_id, provider, purpose)

    if not entitlement.allowed:
        code = entitlement.denial_code
        msg = entitlement.denial_message or "TTS access denied."
        if code == TENANT_CREDENTIAL_MISSING:
            raise TTSCredentialMissingError(provider=provider, message=msg)
        if code == PROVIDER_NOT_ALLOWED:
            raise TTSProviderNotAllowedError(provider=provider, tenant_id=tenant_id, message=msg)
        if code == CREDENTIAL_MODE_NOT_ALLOWED:
            raise TTSCredentialModeError(mode=entitlement.credential_mode, tenant_id=tenant_id, message=msg)
        if code == MONTHLY_TTS_QUOTA_EXCEEDED:
            raise TTSQuotaExceededError(tenant_id=tenant_id, provider=provider, message=msg)
        raise TTSCredentialMissingError(provider=provider, message=msg)

    # Resolve decrypted key — INTERNAL USE ONLY. Never log, return, or serialize.
    api_key = resolve_internal_tts_credential(entitlement)
    if settings_override:
        override_key = settings_override.get(f"{provider}_api_key", "").strip()
        if override_key:
            api_key = override_key

    elevenlabs_api_key = api_key if provider == "elevenlabs" else ""
    gemini_api_key = api_key if provider == "gemini" else ""

    logger.info(
        "[SAAS STEP 6+] TTS entitlement granted: tenant=%s provider=%s mode=%s",
        tenant_id, provider, entitlement.credential_mode,
    )

    # ── Pre-call quota reservation ────────────────────────────────────────────
    from app.services.tts_usage_service import (
        calculate_tts_billable_characters,
        reserve_tts_usage,
        commit_tts_usage,
        release_tts_usage,
        make_production_idempotency_key,
        make_preview_idempotency_key,
    )
    from app.models.plan_tts_policy import get_plan_tts_policy

    billable_chars = calculate_tts_billable_characters(text, provider)
    plan_policy = get_plan_tts_policy(entitlement.plan_id)

    if purpose == "preview":
        idem_key = make_preview_idempotency_key()
    else:
        order_id_str = ""
        attempt_no = 1
        if isinstance(order_data, dict):
            order_id_str = str(order_data.get("external_order_id") or order_data.get("id") or "")
            attempt_no = int(order_data.get("attempt_no") or 1)
        script_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        idem_key = make_production_idempotency_key(
            tenant_id, order_id_str, script_hash, provider, purpose, attempt_no
        )

    reservation_id: str | None = None
    try:
        reservation = reserve_tts_usage(
            tenant_id=tenant_id,
            provider=provider,
            credential_mode=entitlement.credential_mode,
            purpose=purpose,
            idempotency_key=idem_key,
            characters=billable_chars,
            plan_policy=plan_policy,
        )
        reservation_id = str(reservation["id"])
    except Exception as res_exc:
        # TTSQuotaExceededError and InsufficientBalanceError bubble up directly.
        # Unexpected reservation errors are logged but do not block generation —
        # we fail open on the reservation step to avoid silently dropping calls.
        from app.exceptions import InsufficientBalanceError
        if isinstance(res_exc, (TTSQuotaExceededError, InsufficientBalanceError)):
            raise
        logger.error("TTS reservation failed (non-blocking): %s", res_exc)

    import time
    start_t = time.monotonic()
    audio_filename = None
    try:
        audio_filename = create_tts_audio(
            text=text_with_tenant,
            provider=provider,
            lang=lang,
            voice_id=voice_id,
            voice_name=voice_name,
            elevenlabs_api_key=elevenlabs_api_key,
            gemini_api_key=gemini_api_key,
            elevenlabs_model=elevenlabs_model,
            gemini_model=gemini_model,
            edge_voice=edge_voice,
            openvoice_voice=openvoice_voice,
            tenant_id=tenant_id,
        )
    except Exception as gen_exc:
        if reservation_id:
            try:
                release_tts_usage(reservation_id, tenant_id, error_code=type(gen_exc).__name__)
            except Exception as re:
                logger.warning("release_tts_usage failed: %s", re)
        elif entitlement.credential_mode == "platform_managed":
            try:
                from app.models.tenant_tts_usage import record_failed_request
                record_failed_request(tenant_id, provider, "platform_managed")
            except Exception as ue:
                logger.warning("Usage tracking failed (fail path): %s", ue)
        raise

    duration = time.monotonic() - start_t
    TTS_DURATION.labels(provider=provider).observe(duration)

    if reservation_id and audio_filename:
        try:
            commit_tts_usage(
                reservation_id,
                tenant_id,
                actual_characters=billable_chars,
                actual_audio_seconds=duration,
            )
        except Exception as cu:
            logger.warning("commit_tts_usage failed (non-blocking): %s", cu)
    elif audio_filename and entitlement.credential_mode == "platform_managed":
        # Reservation was skipped due to non-blocking error — fall back to direct tracking
        try:
            from app.models.tenant_tts_usage import record_production_success
            record_production_success(
                tenant_id=tenant_id,
                provider=provider,
                credential_mode="platform_managed",
                characters=len(text),
                audio_seconds=duration,
            )
        except Exception as ue:
            logger.warning("Usage tracking failed (success path): %s", ue)

    if audio_filename:
        logger.info("[SAAS STEP 7] TTS audio generated: tenant=%s provider=%s file=%s.ulaw",
                    tenant_id, provider, audio_filename)
    else:
        logger.error("[SAAS STEP 7] TTS audio generation failed: tenant=%s provider=%s",
                     tenant_id, provider)

    return audio_filename
