#!/usr/bin/env python3
"""call_engine/direct_sip_wrapper.py — startup ACK relay for direct_sip_call.py.

Spawns direct_sip_call.py as an inner subprocess with its stdout piped.
Relays ALL inner output verbatim to its own stdout.

direct_sip_call.py emits the structured ACK sentinel directly after the first
SIP INVITE send operation completes — see the CCALL_STARTUP_ACK block in that
script.  This wrapper's job is:

  1. Start the inner script.
  2. Relay every output line verbatim so the parent pipe-reader sees the ACK.
  3. Emit a fallback rejected sentinel if the inner process exits without
     having emitted any CCALL_STARTUP_ACK line (crash before ACK point).

Protocol relayed on stdout:
  CCALL_STARTUP_ACK accepted            — inner emitted after INVITE send OK
  CCALL_STARTUP_ACK rejected:<code>     — inner emitted on failure
  CCALL_STARTUP_ACK rejected:startup_process_exited  — wrapper fallback

The "Calling ..." log line printed by direct_sip_call.py before REGISTER/INVITE
has NO ACK meaning and is relayed as a plain log line.  The parent must not
treat it as an ACK.

The parent must actively read this pipe; Popen alone is insufficient.
Do not include phone numbers, SIP usernames, passwords, or URIs in any
CCALL_STARTUP_ACK line.
"""
from __future__ import annotations

import os
import sys
import subprocess

# Ensure our own output is UTF-8 regardless of the Windows console encoding.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_INNER_SCRIPT = os.path.join(BASE_DIR, "direct_sip_call.py")


def main() -> None:
    args = sys.argv[1:]  # same positional args as direct_sip_call.py

    try:
        inner = subprocess.Popen(
            [sys.executable, _INNER_SCRIPT] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge inner stderr into stdout pipe
            env=os.environ.copy(),
        )
    except OSError as exc:
        sys.stdout.write("CCALL_STARTUP_ACK rejected:startup_process_exited\n")
        sys.stdout.flush()
        sys.stderr.write(f"direct_sip_wrapper: failed to start inner process: {exc}\n")
        sys.stderr.flush()
        sys.exit(1)

    ack_seen = False

    try:
        for raw_line in inner.stdout:
            line = raw_line.decode("utf-8", errors="replace")

            # Track whether inner emitted any CCALL_STARTUP_ACK so we know
            # whether to emit a fallback rejected line on EOF.
            if not ack_seen and line.startswith("CCALL_STARTUP_ACK "):
                ack_seen = True

            # Relay ALL inner output verbatim — the structured CCALL_STARTUP_ACK
            # line from direct_sip_call.py passes through here unchanged and is
            # consumed by the parent's pipe reader.  Do NOT translate or replace
            # any line: the "Calling ..." log has no ACK meaning.
            sys.stdout.write(line)
            sys.stdout.flush()
    except Exception as exc:
        sys.stderr.write(f"direct_sip_wrapper: pipe read error: {exc}\n")
        sys.stderr.flush()

    inner.wait()

    if not ack_seen:
        # Inner process exited (or crashed) without emitting CCALL_STARTUP_ACK.
        sys.stdout.write("CCALL_STARTUP_ACK rejected:startup_process_exited\n")
        sys.stdout.flush()

    sys.exit(inner.returncode if inner.returncode is not None else 0)


if __name__ == "__main__":
    main()
