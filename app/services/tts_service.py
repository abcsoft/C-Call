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
    "আপনার অর্ডারটি নিশ্চিত করতে ১ চাপুন অথবা কাস্টমার কেয়ার "
    "প্রতিনিধির সাথে কথা বলতে ২ চাপুন।"
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

    # Extract tenant settings or fall back to system defaults / env vars
    provider = settings.get("tts_provider", "edge")
    lang = settings.get("tts_lang", "bn")
    voice_name = settings.get("gemini_voice_name", "Puck")
    voice_id = settings.get("elevenlabs_voice_id", "")
    elevenlabs_model = settings.get("elevenlabs_model", "eleven_v3")
    gemini_model = settings.get("gemini_model", "gemini-2.5-flash-preview-tts")
    edge_voice = settings.get("edge_tts_voice", "bn-BD-NabanitaNeural")
    openvoice_voice = settings.get("openvoice_voice", "")

    # Cache key includes tenant_id + model so that changing the ElevenLabs/Gemini
    # model busts the cache and generates fresh audio instead of reusing old audio
    # from a different model. audio.py's tts_cache_hash() only covers
    # text+provider+voice \u2014 model is not in that hash, so we embed it here.
    model_tag = ""
    if provider == "elevenlabs" and elevenlabs_model:
        model_tag = f"\u200c{elevenlabs_model}"
    elif provider == "gemini" and gemini_model:
        model_tag = f"\u200c{gemini_model}"
    text_with_tenant = text + ("\u200b" * tenant_id) + model_tag

    # Resolve tenant-owned API keys.
    # settings_override may carry an unsaved key typed live in the preview form —
    # that path is BYOK too, just not yet persisted.
    # Never fall back to os.environ so one tenant's key cannot bleed into another.
    from app.models.tenant_secrets import get_secret_plaintext
    elevenlabs_api_key = (
        settings_override.get("elevenlabs_api_key", "").strip() if settings_override else ""
    ) or get_secret_plaintext(tenant_id, "elevenlabs_api_key") or ""
    gemini_api_key = (
        settings_override.get("gemini_api_key", "").strip() if settings_override else ""
    ) or get_secret_plaintext(tenant_id, "gemini_api_key") or ""

    # Platform-managed mode: only when tenant explicitly opts in AND platform key configured
    tts_credential_mode = settings.get("tts_credential_mode", "byok")
    if tts_credential_mode == "platform_managed":
        if provider == "elevenlabs" and not elevenlabs_api_key:
            elevenlabs_api_key = os.environ.get("PLATFORM_ELEVENLABS_API_KEY", "")
        if provider == "gemini" and not gemini_api_key:
            gemini_api_key = os.environ.get("PLATFORM_GEMINI_API_KEY", "")

    # --- Strict provider credential guard ---
    # Raise BEFORE calling audio.py so the dispatcher never silently
    # falls through to a different provider on missing credentials.
    CREDENTIAL_REQUIRED = {"elevenlabs", "gemini"}
    if provider in CREDENTIAL_REQUIRED:
        if provider == "elevenlabs" and not elevenlabs_api_key:
            raise TTSCredentialMissingError(
                provider="elevenlabs",
                message="ElevenLabs API key is not configured for this account.",
            )
        if provider == "gemini" and not gemini_api_key:
            raise TTSCredentialMissingError(
                provider="gemini",
                message="Gemini API key is not configured for this account.",
            )

    logger.info(f"Generating TTS for tenant {tenant_id} using provider {provider}")

    # Generate the audio file (audio.py handles cache lookup and disk storage)
    import time
    start_t = time.monotonic()
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
    duration = time.monotonic() - start_t
    TTS_DURATION.labels(provider=provider).observe(duration)

    # [SAAS STEP 7] TTS audio generated or failed
    if audio_filename:
        logger.info(f"[SAAS STEP 7] TTS audio generated successfully: {audio_filename}.ulaw")
    else:
        logger.error(f"[SAAS STEP 7] TTS audio generation failed for text: {text}")

    return audio_filename
