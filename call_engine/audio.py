# E:\c-call-ivr\audio.py
"""Audio + phone-number helpers.

Generates TTS prompts (gTTS, ElevenLabs, Gemini), converts audio to 8kHz
mono mu-law (.ulaw) for RTP playback, and normalizes/formats phone numbers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from gtts import gTTS
from werkzeug.utils import secure_filename

import db

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
if BASE_DIR.name == "call_engine":
    BASE_DIR = BASE_DIR.parent
INSTANCE_DIR = BASE_DIR / "instance"
WORK_DIR = INSTANCE_DIR / "generated"
UPLOAD_DIR = INSTANCE_DIR / "uploads"
SOUNDS_DIR = INSTANCE_DIR / "sounds"
RECORDINGS_DIR = INSTANCE_DIR / "recordings"

# Hard cap on TTS input — protects ffmpeg/edge-tts/OpenVoice from pathological
# inputs (multi-MB text would blow up generation time, memory, and the
# resulting audio file). One IVR prompt or call message has no business
# being longer than this.
MAX_TTS_TEXT_LENGTH = 2000

# Bounds concurrent *generation* (network TTS call + ffmpeg subprocess) across
# all providers. Tuned for a 4 vCPU / 6GB box: ffmpeg + edge-tts + a future
# OpenVoice model load are all CPU/RAM-hungry, and SIP/RTP threads need to
# keep their timing — so we don't let TTS generation starve the box.
TTS_MAX_CONCURRENT = max(1, int(os.environ.get("TTS_MAX_CONCURRENT", "2")))
_gen_semaphore = threading.Semaphore(TTS_MAX_CONCURRENT)

# ffmpeg should never hang a request indefinitely (bad input, stuck pipe).
FFMPEG_TIMEOUT_SECONDS = int(os.environ.get("FFMPEG_TIMEOUT_SECONDS", "30"))


def validate_tts_text(text: str) -> str:
    """Raise ValueError for empty or pathologically long input. Returns the
    stripped text so callers can chain it."""
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("Prompt text is required.")
    if len(stripped) > MAX_TTS_TEXT_LENGTH:
        raise ValueError(
            f"Prompt text is too long ({len(stripped)} chars, max {MAX_TTS_TEXT_LENGTH})."
        )
    return stripped

# Gemini voices (hardcoded — no listing API yet)
GEMINI_VOICES = [
    "Aoede", "Charon", "Fenrir", "Kore", "Puck", "Zephyr",
    "Algieba", "Despina", "Erinome", "Gacrux", "Laomedeia",
    "Pulcherrima", "Rasalas", "Sadachbia", "Sadaltager",
    "Schedar", "Sulafat", "Umbriel", "Vindemiatrix",
    "Achernar", "Achird", "Algenib", "Alnilam", "Autonoe",
    "Callirrhoe", "Diphda", "Enceladus", "Iapetus", "Izar",
    "Zubenelgenubi",
]


def ensure_dirs() -> None:
    for path in (INSTANCE_DIR, WORK_DIR, UPLOAD_DIR, SOUNDS_DIR, RECORDINGS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def ffmpeg_path() -> str:
    return os.environ.get("FFMPEG_PATH", "ffmpeg")


def run_ffmpeg(args: list[str]) -> None:
    """Run ffmpeg with a timeout and a useful error on failure."""
    try:
        result = subprocess.run(
            args, check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"ffmpeg not found at '{ffmpeg_path()}'. Set FFMPEG_PATH in .env."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ffmpeg timed out after {FFMPEG_TIMEOUT_SECONDS}s (input may be corrupt/huge)."
        ) from exc
    if result.returncode != 0:
        stderr_tail = (result.stderr or b"").decode(errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode}): {stderr_tail}")


def convert_to_call_audio(source_path: Path, audio_id: str) -> str:
    """Convert any audio file to an 8kHz mono .ulaw stream. Returns basename."""
    ensure_dirs()
    if not Path(source_path).exists():
        raise FileNotFoundError(f"Source audio file missing: {source_path}")

    audio_name = f"ivr-{audio_id}"
    wav_path = SOUNDS_DIR / f"{audio_name}.wav"
    ulaw_path = SOUNDS_DIR / f"{audio_name}.ulaw"

    run_ffmpeg([ffmpeg_path(), "-y", "-i", str(source_path),
                 "-ar", "8000", "-ac", "1", str(wav_path)])
    run_ffmpeg([ffmpeg_path(), "-y", "-i", str(wav_path),
                 "-acodec", "pcm_mulaw", "-f", "mulaw", "-ar", "8000", "-ac", "1",
                 str(ulaw_path)])

    if not ulaw_path.exists() or ulaw_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced an empty/missing .ulaw file.")

    # Source (mp3/raw/intermediate wav) is no longer needed — don't let it
    # pile up on disk across thousands of calls.
    for stale in (Path(source_path), wav_path):
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass

    return audio_name


# ---------------------------------------------------------------------------
# gTTS (default / fallback)
# ---------------------------------------------------------------------------

def create_gtts_audio(text: str, lang: str = "bn", audio_id: str | None = None) -> str:
    text = validate_tts_text(text)
    ensure_dirs()
    audio_id = audio_id or uuid.uuid4().hex[:12]
    mp3_path = WORK_DIR / f"gtts-{audio_id}.mp3"
    with _gen_semaphore:
        try:
            gTTS(text=text, lang=lang).save(str(mp3_path))
        except Exception as exc:
            raise RuntimeError(f"gTTS generation failed: {exc}") from exc
        return convert_to_call_audio(mp3_path, audio_id)


# ---------------------------------------------------------------------------
# ElevenLabs v3
# ---------------------------------------------------------------------------

def list_elevenlabs_voices(api_key: str) -> list[dict]:
    """Return list of ElevenLabs voices [{voice_id, name, category, labels}]."""
    if not api_key:
        raise ValueError("ElevenLabs API key is not configured.")
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        return [
            {
                "voice_id": v.get("voice_id", ""),
                "name": v.get("name", ""),
                "category": v.get("category", ""),
                "labels": v.get("labels", {}),
                "preview_url": v.get("preview_url", ""),
            }
            for v in data.get("voices", [])
        ]
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"ElevenLabs voice list failed ({exc.code}): {body_text}") from exc
    except Exception as exc:
        raise RuntimeError(f"ElevenLabs voice list failed: {exc}") from exc


def create_elevenlabs_audio(
    text: str,
    voice_id: str,
    api_key: str,
    model_id: str = "eleven_v3",
    audio_id: str | None = None,
) -> str:
    """Generate speech via ElevenLabs v3 API. Returns ulaw basename."""
    text = validate_tts_text(text)
    if not api_key:
        raise ValueError("ElevenLabs API key is not configured.")
    if not voice_id:
        raise ValueError("ElevenLabs voice ID is not selected.")
    ensure_dirs()
    audio_id = audio_id or uuid.uuid4().hex[:12]
    mp3_path = WORK_DIR / f"elevenlabs-{audio_id}.mp3"

    body = json.dumps({
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode()

    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
        data=body,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    with _gen_semaphore:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                mp3_path.write_bytes(resp.read())
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            raise RuntimeError(f"ElevenLabs API error {exc.code}: {body_text}") from exc
        except Exception as exc:
            raise RuntimeError(f"ElevenLabs request failed: {exc}") from exc

        return convert_to_call_audio(mp3_path, audio_id)


# ---------------------------------------------------------------------------
# Google Gemini TTS
# ---------------------------------------------------------------------------

def list_gemini_voices() -> list[dict]:
    """Return hardcoded list of Gemini TTS voices."""
    return [{"voice_name": v, "language": "multi"} for v in GEMINI_VOICES]


def create_gemini_audio(
    text: str,
    voice_name: str,
    api_key: str,
    audio_id: str | None = None,
    model_id: str = "gemini-2.5-flash-preview-tts",
) -> str:
    """Generate speech via Google Gemini 2.5 Flash TTS. Returns ulaw basename."""
    text = validate_tts_text(text)
    if not api_key:
        raise ValueError("Gemini API key is not configured.")
    ensure_dirs()
    audio_id = audio_id or uuid.uuid4().hex[:12]
    voice_name = voice_name or "Puck"

    body = json.dumps({
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": voice_name}
                }
            },
        },
    }).encode()

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_id}:generateContent?key={api_key}"
    )
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with _gen_semaphore:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            raise RuntimeError(f"Gemini API error {exc.code}: {body_text}") from exc
        except Exception as exc:
            raise RuntimeError(f"Gemini request failed: {exc}") from exc

        try:
            part = data["candidates"][0]["content"]["parts"][0]
            inline_audio = part.get("inlineData") or part.get("inline_data")
            audio_b64 = inline_audio["data"]
            mime_type = inline_audio.get("mimeType") or inline_audio.get("mime_type") or ""
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected Gemini response structure: {data}") from exc

        raw_bytes = base64.b64decode(audio_b64)
        rate_match = re.search(r"rate=(\d+)", mime_type)
        channels_match = re.search(r"channels=(\d+)", mime_type)
        sample_rate = rate_match.group(1) if rate_match else "24000"
        channels = channels_match.group(1) if channels_match else "1"

        # Gemini returns raw PCM (usually 24kHz, 16-bit, mono) — save as .raw then convert
        raw_path = WORK_DIR / f"gemini-{audio_id}.raw"
        raw_path.write_bytes(raw_bytes)

        audio_name = f"ivr-{audio_id}"
        ulaw_path = SOUNDS_DIR / f"{audio_name}.ulaw"
        try:
            run_ffmpeg([
                ffmpeg_path(), "-y",
                "-f", "s16le", "-ar", sample_rate, "-ac", channels,
                "-i", str(raw_path),
                "-acodec", "pcm_mulaw", "-f", "mulaw", "-ar", "8000", "-ac", "1",
                str(ulaw_path),
            ])
        finally:
            try:
                raw_path.unlink(missing_ok=True)
            except OSError:
                pass

        if not ulaw_path.exists() or ulaw_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced an empty/missing .ulaw file.")
        return audio_name


# ---------------------------------------------------------------------------
# Edge-TTS (Microsoft)
# ---------------------------------------------------------------------------

def list_edge_voices() -> list[dict]:
    """Return hardcoded list of Edge-TTS Bangla voices."""
    from services.edge_tts_service import EDGE_VOICES
    return [{"voice_name": v, "language": "bn"} for v in EDGE_VOICES]


def create_edge_audio(
    text: str,
    voice_name: str,
    audio_id: str | None = None,
) -> str:
    """Generate speech via Edge-TTS. Returns ulaw basename."""
    from services.edge_tts_service import EDGE_VOICES, generate_edge_tts
    text = validate_tts_text(text)
    ensure_dirs()
    audio_id = audio_id or uuid.uuid4().hex[:12]
    with _gen_semaphore:
        wav_path = Path(generate_edge_tts(text, voice_name or EDGE_VOICES[0]))
        return convert_to_call_audio(wav_path, audio_id)


# ---------------------------------------------------------------------------
# TTS cache — avoids regenerating identical (text, provider, voice) audio
# ---------------------------------------------------------------------------

def tts_cache_hash(text: str, provider: str, voice: str, tenant_id: int | None = None) -> str:
    salt = f"|{tenant_id}" if tenant_id is not None else ""
    return hashlib.sha256(f"{text}|{provider}|{voice}{salt}".encode("utf-8")).hexdigest()


def get_cached_tts(text_hash: str, tenant_id: int | None = None) -> str | None:
    """Return cached .ulaw basename if present, on disk, and non-empty.

    A 0-byte or missing file means the cache row is stale (e.g. disk
    cleanup, crash mid-write) — treat it as a miss so the caller regenerates
    instead of handing back corrupt/silent audio to a live call."""
    try:
        if tenant_id is not None:
            row = db.query_one("SELECT audio_path FROM tts_cache WHERE text_hash = ? AND tenant_id = ?", (text_hash, tenant_id))
        else:
            row = db.query_one("SELECT audio_path FROM tts_cache WHERE text_hash = ?", (text_hash,))
    except Exception:
        logger.warning("tts_cache lookup failed; treating as cache miss", exc_info=True)
        return None
    if not row:
        return None
    path = ulaw_path_for(row["audio_path"])
    try:
        if path.exists() and path.stat().st_size > 0:
            return row["audio_path"]
    except OSError:
        pass
    return None


def save_tts_cache(
    provider: str,
    voice: str,
    text_hash: str,
    text: str,
    audio_path: str,
    requested_provider: str | None = None,
    actual_provider: str | None = None,
    actual_voice: str | None = None,
    tenant_id: int | None = None,
) -> None:
    """Best-effort cache write. The audio itself was already generated
    successfully by the time this runs — a cache-table failure (e.g. DB
    momentarily locked) should not turn a successful generation into a
    user-facing error.

    requested_provider/actual_provider/actual_voice record what the caller
    asked for vs. what actually produced the audio — e.g. a row with
    requested_provider="openvoice", actual_provider="edge" means OpenVoice
    was unavailable and an explicit fallback ran. Without this, a fallback
    result is indistinguishable from a genuine OpenVoice generation."""
    requested_provider = requested_provider or provider
    actual_provider = actual_provider or provider
    actual_voice = actual_voice or voice
    try:
        if tenant_id is not None:
            try:
                db.execute(
                    """
                    INSERT INTO tts_cache
                        (provider, voice, text_hash, text, audio_path,
                         requested_provider, actual_provider, actual_voice, created_at, tenant_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, now(), ?)
                    ON CONFLICT (text_hash) DO UPDATE SET
                        audio_path = excluded.audio_path,
                        requested_provider = excluded.requested_provider,
                        actual_provider = excluded.actual_provider,
                        actual_voice = excluded.actual_voice,
                        tenant_id = excluded.tenant_id
                    """,
                    (provider, voice, text_hash, text, audio_path,
                     requested_provider, actual_provider, actual_voice, tenant_id),
                )
                return
            except Exception as e:
                logger.warning("Failed to write tts_cache with tenant_id, trying fallback: %s", e)

        db.execute(
            """
            INSERT INTO tts_cache
                (provider, voice, text_hash, text, audio_path,
                 requested_provider, actual_provider, actual_voice, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())
            ON CONFLICT (text_hash) DO UPDATE SET
                audio_path = excluded.audio_path,
                requested_provider = excluded.requested_provider,
                actual_provider = excluded.actual_provider,
                actual_voice = excluded.actual_voice
            """,
            (provider, voice, text_hash, text, audio_path,
             requested_provider, actual_provider, actual_voice),
        )
    except Exception:
        logger.warning("Failed to write tts_cache row for %s/%s", provider, voice, exc_info=True)


# ---------------------------------------------------------------------------
# OpenVoice V2 (backup provider — preview / pre-generation / cache only)
# ---------------------------------------------------------------------------
# IMPORTANT: create_openvoice_audio() is the only entry point that may run
# real OpenVoice inference. It must never be called from a live call path —
# only from preview and pre-generation routes.
#
# It does NOT silently fall back to Edge-TTS: if the OpenVoice runtime is
# missing, this raises a clear RuntimeError so the caller (preview/test
# routes) can show "OpenVoice runtime is not installed or configured."
# instead of quietly handing back Edge-TTS audio under the OpenVoice label.
#
# The live dispatcher (create_tts_audio) treats "openvoice" as
# cache-read-only: on a cache miss it raises by default, and only falls
# back to Edge-TTS if OPENVOICE_FALLBACK_TO_EDGE=true is set — and when it
# does, it delegates to create_tts_audio(provider="edge", ...) directly
# rather than calling into this function, so the resulting cache row is
# correctly tagged actual_provider="edge", never mislabeled "openvoice".

def list_openvoice_voices() -> list[dict]:
    """Return hardcoded list of OpenVoice V2 voices."""
    from services.openvoice_service import OPENVOICE_VOICES
    return [{"voice_name": v, "language": "multi"} for v in OPENVOICE_VOICES]


def create_openvoice_audio(
    text: str,
    voice_name: str,
    audio_id: str | None = None,
) -> str:
    """Generate speech via OpenVoice V2 (preview / pre-generation only).

    Workflow: text -> OpenVoice -> WAV -> cache -> return ulaw basename.
    Raises RuntimeError if OpenVoice is unavailable — no silent fallback.
    """
    from services.openvoice_service import OPENVOICE_VOICES, generate_openvoice_tts
    text = validate_tts_text(text)
    ensure_dirs()
    voice_name = voice_name or OPENVOICE_VOICES[0]

    text_hash = tts_cache_hash(text, "openvoice", voice_name)
    cached = get_cached_tts(text_hash)
    if cached:
        return cached

    audio_id = audio_id or uuid.uuid4().hex[:12]
    with _gen_semaphore:
        wav_path = Path(generate_openvoice_tts(text, voice_name))
        audio_name = convert_to_call_audio(wav_path, audio_id)
    save_tts_cache(
        "openvoice", voice_name, text_hash, text, audio_name,
        requested_provider="openvoice", actual_provider="openvoice", actual_voice=voice_name,
    )
    return audio_name


def pregenerate_openvoice_audio(text: str, voice_name: str) -> str:
    """Explicit pre-generation entry point — same as create_openvoice_audio,
    named separately so call sites document intent (warm the cache ahead
    of a scheduled/bulk call instead of generating during one)."""
    return create_openvoice_audio(text, voice_name)


# ---------------------------------------------------------------------------
# Unified TTS dispatcher
# ---------------------------------------------------------------------------

def create_tts_audio(
    text: str,
    lang: str = "bn",
    provider: str | None = None,
    voice_id: str | None = None,
    voice_name: str | None = None,
    elevenlabs_api_key: str | None = None,
    gemini_api_key: str | None = None,
    elevenlabs_model: str | None = None,
    gemini_model: str | None = None,
    edge_voice: str | None = None,
    openvoice_voice: str | None = None,
    audio_id: str | None = None,
    tenant_id: int | None = None,
) -> str:
    """Dispatch TTS to the right provider, caching by (text, provider, voice).
    Falls back to gTTS on missing config.

    "openvoice" is cache-read-only here: a live call must never run
    OpenVoice inference inline. On a cache miss this raises by default
    (OPENVOICE_FALLBACK_TO_EDGE unset/false) — the call fails loudly
    instead of silently playing Edge-TTS audio under the OpenVoice label.
    Set OPENVOICE_FALLBACK_TO_EDGE=true to allow an explicit, logged
    fallback instead. Real OpenVoice generation only happens via
    create_openvoice_audio() (preview / pre-generation), which warms this
    same cache ahead of time so live calls hit it."""
    text = validate_tts_text(text)
    provider = provider or "gtts"
    voice_key = {
        "elevenlabs": voice_id,
        "gemini": voice_name or "Puck",
        "edge": edge_voice,
        "openvoice": openvoice_voice or "default",
    }.get(provider, lang)

    text_hash = tts_cache_hash(text, provider, voice_key, tenant_id=tenant_id)
    cached = get_cached_tts(text_hash, tenant_id=tenant_id)
    if cached:
        return cached

    if provider == "openvoice":
        if os.environ.get("OPENVOICE_FALLBACK_TO_EDGE", "false").strip().lower() == "true":
            logger.warning(
                "OPENVOICE_FALLBACK_TO_EDGE_USED requested_voice=%s edge_voice=%s",
                voice_key, edge_voice,
            )
            return create_tts_audio(text, provider="edge", edge_voice=edge_voice, audio_id=audio_id, tenant_id=tenant_id)
        raise RuntimeError(
            "OpenVoice audio is not pre-generated for this text/voice. "
            "Pre-generate it from the TTS Test page, or set "
            "OPENVOICE_FALLBACK_TO_EDGE=true to allow a live fallback."
        )

    if provider == "elevenlabs" and elevenlabs_api_key and voice_id:
        audio_name = create_elevenlabs_audio(
            text, voice_id, elevenlabs_api_key,
            model_id=elevenlabs_model or "eleven_v3",
            audio_id=audio_id,
        )
    elif provider == "gemini" and gemini_api_key:
        audio_name = create_gemini_audio(
            text, voice_name or "Puck", gemini_api_key,
            audio_id=audio_id,
            model_id=gemini_model or "gemini-2.5-flash-preview-tts",
        )
    elif provider == "edge":
        audio_name = create_edge_audio(text, edge_voice, audio_id=audio_id)
    else:
        audio_name = create_gtts_audio(text, lang, audio_id)

    save_tts_cache(provider, voice_key, text_hash, text, audio_name, tenant_id=tenant_id)
    return audio_name


# ---------------------------------------------------------------------------
# Upload helper
# ---------------------------------------------------------------------------

def create_uploaded_audio(file_storage) -> str:
    if not file_storage or not file_storage.filename:
        raise ValueError("Audio upload is required.")
    ensure_dirs()
    audio_id = uuid.uuid4().hex[:12]
    filename = secure_filename(file_storage.filename) or f"upload-{audio_id}"
    source_path = UPLOAD_DIR / f"{audio_id}-{filename}"
    file_storage.save(source_path)
    return convert_to_call_audio(source_path, audio_id)


def ulaw_path_for(audio_name: str) -> Path:
    return SOUNDS_DIR / f"{audio_name}.ulaw"


def ulaw_to_wav_bytes(audio_name: str) -> bytes:
    """Convert a generated .ulaw prompt to WAV bytes for in-browser playback."""
    ensure_dirs()
    ulaw = ulaw_path_for(audio_name)
    if not ulaw.exists():
        raise FileNotFoundError(f"Audio {audio_name}.ulaw was not generated.")
    wav = WORK_DIR / f"preview-{audio_name}.wav"
    run_ffmpeg([ffmpeg_path(), "-y", "-f", "mulaw", "-ar", "8000", "-ac", "1",
                 "-i", str(ulaw), str(wav)])
    data = wav.read_bytes()
    try:
        wav.unlink()
    except OSError:
        pass
    return data


def audio_duration_seconds(audio_name: str) -> int:
    path = ulaw_path_for(audio_name)
    if not path.exists():
        return 0
    return max(1, math.ceil(path.stat().st_size / 8000))


# ---------------------------------------------------------------------------
# Phone number helpers (BD-focused)
# ---------------------------------------------------------------------------

def normalize_phone_number(raw_number: str) -> str:
    number = (raw_number or "").strip()
    if not number:
        raise ValueError("Phone number is required.")
    has_plus = number.startswith("+")
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        raise ValueError("Phone number must contain digits.")
    if number.startswith("00") and len(digits) > 2:
        return f"+{digits[2:]}"
    if has_plus:
        return f"+{digits}"
    if digits.startswith("880"):
        return f"+{digits}"
    if digits.startswith("0"):
        return f"+880{digits[1:]}"
    if digits.startswith("1") and len(digits) == 10:
        return f"+880{digits}"
    return f"+880{digits}"


def format_dial_number(normalized_number: str, selected_format: str) -> str:
    number = normalize_phone_number(normalized_number)
    digits = "".join(ch for ch in number if ch.isdigit())
    selected = (selected_format or "local_bd").strip().lower()
    if selected in {"e164", "plus", "plus_e164"}:
        return f"+{digits}"
    if selected in {"local_bd", "bd_local"} and digits.startswith("880"):
        return f"0{digits[3:]}"
    if selected in {"raw", "normalized"}:
        return number
    return digits


def mask_number(number: str) -> str:
    visible = "".join(ch for ch in number if ch.isdigit())
    if len(visible) <= 4:
        return "****"
    return f"{'*' * (len(visible) - 4)}{visible[-4:]}"


def number_match_key(number: str) -> str:
    digits = "".join(ch for ch in (number or "") if ch.isdigit())
    if digits.startswith("880") and len(digits) > 3:
        digits = digits[3:]
    if digits.startswith("0") and len(digits) > 1:
        digits = digits[1:]
    return digits[-10:]
