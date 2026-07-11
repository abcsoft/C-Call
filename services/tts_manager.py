# E:\c-call-ivr\services\tts_manager.py
"""Provider abstraction layer for raw TTS generation.

Unifies the Edge-TTS and OpenVoice backends behind one call. This sits
below audio.py — it only returns a WAV file path, it does not know about
the .ulaw conversion or the tts_cache table. Any provider failure here
falls back to Edge-TTS so callers always get a WAV back.
"""
from __future__ import annotations

from services.edge_tts_service import EDGE_VOICES, generate_edge_tts
from services.openvoice_service import OPENVOICE_VOICES, generate_openvoice_tts

PROVIDERS = ("edge", "openvoice")

_DEFAULT_EDGE_VOICE = EDGE_VOICES[0]
_DEFAULT_OPENVOICE_VOICE = OPENVOICE_VOICES[0]


def generate_tts(text: str, provider: str = "edge", voice: str = "") -> str:
    """Generate speech and return a WAV file path.

    provider="edge"      -> Edge-TTS
    provider="openvoice"  -> OpenVoice V2 (already falls back to Edge-TTS
                             internally if its runtime is unavailable)

    Any unhandled failure from the chosen provider falls back to Edge-TTS,
    so this function only raises if Edge-TTS itself fails.
    """
    if not text.strip():
        raise ValueError("Prompt text is required.")

    try:
        if provider == "openvoice":
            return generate_openvoice_tts(text, voice or _DEFAULT_OPENVOICE_VOICE)
        if provider == "edge":
            return generate_edge_tts(text, voice or _DEFAULT_EDGE_VOICE)
        raise ValueError(f"Unknown TTS provider: {provider}")
    except Exception as exc:
        if provider == "edge":
            raise
        print(f"TTS provider '{provider}' failed ({exc}); falling back to Edge-TTS.", flush=True)
        return generate_edge_tts(text, _DEFAULT_EDGE_VOICE)
