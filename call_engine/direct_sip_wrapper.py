#!/usr/bin/env python3
"""call_engine/direct_sip_wrapper.py — structured startup ACK wrapper for direct_sip_call.py.

Spawns direct_sip_call.py as an inner subprocess with its stdout piped.
Monitors the inner process output for the startup sentinel and emits a
structured CCALL_STARTUP_ACK line to its own stdout before relaying further
output.

This keeps direct_sip_call.py unmodified (read-only legacy binary) while
giving the parent process an exact, unambiguous ACK sentinel that cannot be
triggered by any generic log line from any other child process.

Protocol emitted on stdout (one line, before any other output):
  CCALL_STARTUP_ACK accepted
  CCALL_STARTUP_ACK rejected:<error_code>

After the ACK line, all subsequent inner-process output is relayed verbatim
so the parent can detect "Call answered; sending audio" and hangup lines.

Error codes:
  startup_process_exited  — inner process exited without emitting sentinel
  startup_protocol_error  — internal wrapper error

The parent must actively read this pipe; Popen alone is insufficient.
Do not include phone numbers, SIP usernames, SIP passwords, or URIs in the
ACK line.
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

# Startup sentinel printed by direct_sip_call.py immediately before register()
# and invite().  It is the closest available pre-INVITE boundary in the
# read-only inner script.
_INNER_STARTUP_SENTINEL = "Calling "


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
        sys.stdout.write(f"CCALL_STARTUP_ACK rejected:startup_process_exited\n")
        sys.stdout.flush()
        sys.stderr.write(f"direct_sip_wrapper: failed to start inner process: {exc}\n")
        sys.stderr.flush()
        sys.exit(1)

    ack_emitted = False

    try:
        for raw_line in inner.stdout:
            line = raw_line.decode("utf-8", errors="replace")

            if not ack_emitted:
                if _INNER_STARTUP_SENTINEL in line:
                    # Emit structured ACK before relaying this line.
                    sys.stdout.write("CCALL_STARTUP_ACK accepted\n")
                    sys.stdout.flush()
                    ack_emitted = True

            # Relay ALL inner output verbatim (after ACK line above).
            # Parent uses this to detect "Call answered; sending audio",
            # "Remote hung up", and "Call completed (remote ended)".
            sys.stdout.write(line)
            sys.stdout.flush()
    except Exception as exc:
        if not ack_emitted:
            sys.stdout.write("CCALL_STARTUP_ACK rejected:startup_protocol_error\n")
            sys.stdout.flush()
            ack_emitted = True
        sys.stderr.write(f"direct_sip_wrapper: pipe read error: {exc}\n")
        sys.stderr.flush()

    inner.wait()

    if not ack_emitted:
        # Inner process exited (or EOF) without emitting startup sentinel.
        sys.stdout.write("CCALL_STARTUP_ACK rejected:startup_process_exited\n")
        sys.stdout.flush()

    sys.exit(inner.returncode if inner.returncode is not None else 0)


if __name__ == "__main__":
    main()
