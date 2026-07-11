# E:\c-call-ivr\services\openvoice_service.py
"""OpenVoice V2 speech generation — backup TTS provider.

OpenVoice is only used for preview, pre-generation, and cached-audio
workflows (see audio.create_openvoice_audio). It is never invoked during
a live call: live calls only ever read from the .ulaw cache, since
OpenVoice model inference is too slow to run inline on an active SIP call.

This module does NOT silently fall back to Edge-TTS. If the OpenVoice
runtime/checkpoints are not installed, generate_openvoice_tts() raises a
clear RuntimeError. Any fallback decision belongs to the caller — see
audio.create_tts_audio's OPENVOICE_FALLBACK_TO_EDGE handling, which (when
enabled) delegates to Edge-TTS explicitly and logs
OPENVOICE_FALLBACK_TO_EDGE_USED, rather than this module quietly
substituting Edge-TTS audio under the OpenVoice label.

Memory/concurrency notes (4 vCPU / 6GB target box): the OpenVoice model is
loaded once into a module-level singleton (guarded by a lock) instead of
per-call — re-instantiating ToneColorConverter on every request would
reload weights into RAM repeatedly, which a 6GB box cannot absorb under
any real concurrency. Inference itself runs on a single dedicated worker
thread (not the Flask request thread), bounded by a timeout, so one slow
synthesis can't hang the request indefinitely and at most one model
instance is ever resident in memory.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

from call_engine.audio import WORK_DIR, ensure_dirs, validate_tts_text

logger = logging.getLogger(__name__)

# OpenVoice V2 tone-color reference voices. These are intentionally NOT
# Edge-TTS voice names (e.g. not "bn-BD-NabanitaNeural") — OpenVoice has its
# own distinct voice identity, and reusing Edge naming here is exactly what
# let an Edge-TTS fallback masquerade as "OpenVoice" in cache/UI before this
# fix. Replace with your installed checkpoint's real speaker names.
OPENVOICE_VOICES = [
    "ov-default",
    "ov-warm",
    "ov-narrator",
]

# Inference must not hang a preview/pregenerate request forever.
OPENVOICE_TIMEOUT_SECONDS = int(os.environ.get("OPENVOICE_TIMEOUT_SECONDS", "60"))

# Single dedicated worker: never run more than one OpenVoice inference (and
# never load more than one model instance) at a time, regardless of how many
# preview/pregenerate requests arrive concurrently.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openvoice")
_model_lock = threading.Lock()
_converter = None  # lazy-loaded singleton


def openvoice_installed() -> bool:
    """True if the OpenVoice package can actually be imported. Cheap check —
    safe to call from a status endpoint without triggering model load."""
    try:
        import openvoice  # noqa: F401
    except ImportError:
        return False
    return True


def _get_converter():
    """Lazily load the OpenVoice model once and reuse it for every call."""
    global _converter
    if _converter is None:
        with _model_lock:
            if _converter is None:
                from openvoice.api import ToneColorConverter  # type: ignore
                _converter = ToneColorConverter()
    return _converter


def _synthesize_with_openvoice(text: str, voice: str, wav_path) -> None:
    """Run real OpenVoice V2 inference. Raises if the runtime isn't available."""
    if not openvoice_installed():
        raise RuntimeError("OpenVoice runtime is not installed or configured.")
    converter = _get_converter()
    converter.synthesize(text=text, speaker=voice, output_path=str(wav_path))


def generate_openvoice_tts(text: str, voice: str = "ov-default") -> str:
    """Generate speech via OpenVoice V2. Returns path to a WAV file.

    Raises RuntimeError if the OpenVoice runtime is unavailable, synthesis
    fails, or inference exceeds OPENVOICE_TIMEOUT_SECONDS. Does NOT fall
    back to Edge-TTS — callers that want a fallback must do so explicitly.
    """
    text = validate_tts_text(text)
    ensure_dirs()
    audio_id = uuid.uuid4().hex[:12]
    wav_path = WORK_DIR / f"openvoice-{audio_id}.wav"
    voice = voice or "ov-default"

    if not openvoice_installed():
        raise RuntimeError("OpenVoice runtime is not installed or configured.")

    try:
        future = _executor.submit(_synthesize_with_openvoice, text, voice, wav_path)
        future.result(timeout=OPENVOICE_TIMEOUT_SECONDS)
    except FutureTimeoutError as exc:
        raise RuntimeError(
            f"OpenVoice inference exceeded {OPENVOICE_TIMEOUT_SECONDS}s."
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"OpenVoice generation failed: {exc}") from exc

    if not wav_path.exists() or wav_path.stat().st_size == 0:
        raise RuntimeError("OpenVoice produced an empty/missing WAV file.")
    return str(wav_path)
