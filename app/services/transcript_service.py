# app/services/transcript_service.py
from __future__ import annotations

from app.db import db


def get_call_transcript_timeline(call_id: int) -> list[str]:
    """Extract DTMF events from call_events table and build a human-readable timeline.
    
    Timeline format:
      "00:05 — Call answered"
      "00:12 — Customer pressed: 1 (Confirm)"
      "00:15 — Call ended"
    """
    events = db.query(
        "SELECT event_type, digit, at_offset_ms, detail FROM call_events WHERE call_id = %s ORDER BY seq ASC",
        (call_id,)
    )

    timeline = []
    for ev in events:
        offset_ms = ev.get("at_offset_ms") or 0
        secs = offset_ms // 1000
        mins = secs // 60
        sec_rem = secs % 60
        time_str = f"{mins:02d}:{sec_rem:02d}"

        ev_type = ev["event_type"]
        digit = ev.get("digit", "").strip()

        if ev_type == "answered":
            timeline.append(f"{time_str} — Call answered")
        elif ev_type == "dtmf":
            if digit == "1":
                timeline.append(f"{time_str} — Customer pressed: 1 (Confirm)")
            elif digit == "2":
                timeline.append(f"{time_str} — Customer pressed: 2 (Cancel)")
            else:
                timeline.append(f"{time_str} — Customer pressed: {digit}")
        elif ev_type == "hangup":
            timeline.append(f"{time_str} — Call ended")
        elif ev_type == "transfer":
            target = ev.get("detail") or "agent"
            timeline.append(f"{time_str} — Transferring call to {target}")

    return timeline


# TODO Phase 8+: STT Integration
# Use OpenAI Whisper API or self-hosted Whisper for Bangla transcription
# POST audio file to Whisper API → get text → store in order_transcripts table
# Display in order detail with word-level timestamps
