# E:\c-call-ivr\services\edge_tts_service.py
"""Edge-TTS (Microsoft) speech generation.

Generates an MP3 via the edge-tts library, then converts it to an
8kHz mono WAV with ffmpeg — matching the audio pipeline used by the
other TTS providers in audio.py.

Event loop: a single persistent background event loop (one daemon thread,
started lazily) services every generate_edge_tts() call via
asyncio.run_coroutine_threadsafe. Previous version called asyncio.run()
fresh per invocation — not unsafe on its own (each Flask request thread
had no running loop to nest into), but it pays loop-creation/teardown
overhead on every call and under concurrency multiplies short-lived event
loops across threads. A persistent loop is also a prerequisite for
sharing keep-alive connections in edge_tts in the future, and removes any
risk of asyncio.run() being called from a context that later gains a
running loop (e.g. if this gets called from async Flask code).

Timing: every call logs request-start, edge-request-start,
edge-response-received, wav-conversion-start, and wav-conversion-end
timestamps (relative ms) at INFO level, tagged with a short request id, so
a timeout can be attributed to the edge-tts network call vs. the ffmpeg
conversion step.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError

import edge_tts

from call_engine.audio import WORK_DIR, run_ffmpeg, ensure_dirs, ffmpeg_path, validate_tts_text

logger = logging.getLogger(__name__)
if not logger.handlers:
    # The app doesn't configure root logging, so without this the timing/
    # retry diagnostics below would be silently dropped. Self-contained —
    # doesn't touch root or other loggers.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# Bangla voices
EDGE_VOICES = [
    "bn-BD-NabanitaNeural",
    "bn-BD-PradeepNeural",
]

# Network call to Microsoft's edge-tts endpoint — must not hang a request
# forever on a stalled/dropped websocket. Raised from 20s -> 45s: 20s was
# occasionally too tight under concurrent load (TTS_MAX_CONCURRENT callers
# sharing CPU with ffmpeg/other requests), causing spurious timeouts during
# normal operation rather than genuine network hangs.
EDGE_TTS_TIMEOUT_SECONDS = int(os.environ.get("EDGE_TTS_TIMEOUT_SECONDS", "45"))

# One retry after a short backoff before giving up — covers transient
# websocket handshake failures / momentary network blips without making
# callers wait through two full timeout windows.
EDGE_TTS_RETRY_DELAY_SECONDS = float(os.environ.get("EDGE_TTS_RETRY_DELAY_SECONDS", "2"))
EDGE_TTS_MAX_ATTEMPTS = 2

# --- Persistent background event loop -------------------------------------
# Lazily started once, reused by every generate_edge_tts() call from any
# thread, instead of asyncio.run() spinning up/tearing down a new loop
# per call.
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    if _loop is None:
        with _loop_lock:
            if _loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(target=loop.run_forever, daemon=True, name="edge-tts-loop")
                thread.start()
                _loop, _loop_thread = loop, thread
    return _loop


def _run_coro(coro, timeout: float):
    """Run an async coroutine on the persistent loop from a sync caller,
    with a hard timeout. Cancels the in-flight task on timeout instead of
    leaving it running unbounded in the background."""
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        future.cancel()
        raise TimeoutError(f"Edge-TTS call exceeded {timeout}s")


async def _save_mp3(text: str, voice: str, mp3_path) -> None:
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(mp3_path))


def _attempt(text: str, voice: str, mp3_path, wav_path, req_id: str, t0: float) -> str:
    """One full generate-and-convert attempt. Raises on any failure;
    caller decides whether to retry."""
    def mark(label: str) -> None:
        logger.info("edge_tts[%s] %s at +%dms", req_id, label, int((time.monotonic() - t0) * 1000))

    mark("edge_request_start")
    try:
        _run_coro(_save_mp3(text, voice, mp3_path), timeout=EDGE_TTS_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        mark("edge_request_timeout")
        raise RuntimeError(f"Edge-TTS timed out after {EDGE_TTS_TIMEOUT_SECONDS}s.") from exc
    except Exception as exc:
        mark("edge_request_failed")
        raise RuntimeError(f"Edge-TTS generation failed: {exc}") from exc
    mark("edge_response_received")

    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        raise RuntimeError("Edge-TTS produced an empty/missing MP3 file.")

    mark("wav_conversion_start")
    try:
        run_ffmpeg([ffmpeg_path(), "-y", "-i", str(mp3_path),
                    "-ar", "8000", "-ac", "1", str(wav_path)])
    finally:
        try:
            mp3_path.unlink(missing_ok=True)
        except OSError:
            pass
    mark("wav_conversion_end")

    if not wav_path.exists() or wav_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced an empty/missing WAV file.")

    return str(wav_path)


def generate_edge_tts(text: str, voice: str) -> str:
    """Generate speech via Edge-TTS. Returns path to an 8kHz mono WAV file.

    Retries once (after EDGE_TTS_RETRY_DELAY_SECONDS) on any failure —
    network blip or timeout — before raising.
    """
    text = validate_tts_text(text)
    if not voice:
        raise ValueError("Edge-TTS voice is not selected.")
    ensure_dirs()

    req_id = uuid.uuid4().hex[:8]
    t0 = time.monotonic()
    logger.info("edge_tts[%s] request_start voice=%s chars=%d", req_id, voice, len(text))

    last_exc: Exception | None = None
    for attempt in range(1, EDGE_TTS_MAX_ATTEMPTS + 1):
        audio_id = uuid.uuid4().hex[:12]
        mp3_path = WORK_DIR / f"edge-{audio_id}.mp3"
        wav_path = WORK_DIR / f"edge-{audio_id}.wav"
        try:
            result = _attempt(text, voice, mp3_path, wav_path, req_id, t0)
            logger.info("edge_tts[%s] succeeded on attempt %d/%d (+%dms total)",
                        req_id, attempt, EDGE_TTS_MAX_ATTEMPTS, int((time.monotonic() - t0) * 1000))
            return result
        except Exception as exc:
            last_exc = exc
            for stale in (mp3_path, wav_path):
                try:
                    stale.unlink(missing_ok=True)
                except OSError:
                    pass
            if attempt < EDGE_TTS_MAX_ATTEMPTS:
                logger.warning(
                    "edge_tts[%s] attempt %d/%d failed (%s); retrying in %ss",
                    req_id, attempt, EDGE_TTS_MAX_ATTEMPTS, exc, EDGE_TTS_RETRY_DELAY_SECONDS,
                )
                time.sleep(EDGE_TTS_RETRY_DELAY_SECONDS)

    logger.error(
        "edge_tts[%s] failed after %d attempts (+%dms total): %s",
        req_id, EDGE_TTS_MAX_ATTEMPTS, int((time.monotonic() - t0) * 1000), last_exc,
    )
    raise last_exc
