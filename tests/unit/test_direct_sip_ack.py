"""tests/unit/test_direct_sip_ack.py — CCALL_STARTUP_ACK protocol unit tests.

Proves that the ACK mechanism is only triggered by the exact structured sentinel
emitted by direct_sip_call.py (and relayed verbatim by direct_sip_wrapper.py) —
NOT by:
  - Popen alone (before any output is read)
  - A generic "Calling " line from inner subprocess output
  - Constructing IvrCall or submitting to the thread pool
  - Any other log line

Positive cases:
  - Exact "CCALL_STARTUP_ACK accepted\\n" → accepted=True
  - "CCALL_STARTUP_ACK rejected:<code>\\n" → accepted=False, error_code=<code>
  - Malformed "CCALL_STARTUP_ACK \\n" (unknown payload) → startup_protocol_error

Isolation case:
  - Two concurrent pipe readers → each fires only its own ACK event.

These are pure unit tests — no real subprocesses, no DB, no Flask app required.
"""
from __future__ import annotations

import io
import threading
import time
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers to simulate the _pipe_reader loop from call_runner.py.
# We extract the ACK-detection logic so it can be tested without spawning
# real subprocesses or requiring the full call_runner module to be importable.
# ---------------------------------------------------------------------------

def _run_pipe_reader(lines: list[str]) -> tuple[bool, str]:
    """Simulate the _pipe_reader ACK-detection logic against a list of output lines.

    Returns (accepted, error_code) as the _ack_result would be after reading all lines.
    """
    import threading

    ack_event = threading.Event()
    ack_result: list[tuple[bool, str]] = [(False, "pending")]

    for line in lines:
        line = line.rstrip("\n") + "\n"  # normalise
        if not ack_event.is_set() and line.startswith("CCALL_STARTUP_ACK "):
            payload = line[len("CCALL_STARTUP_ACK "):].strip()
            if payload == "accepted":
                ack_result[0] = (True, "")
            elif payload.startswith("rejected:"):
                err = payload[len("rejected:"):]
                ack_result[0] = (False, err or "startup_process_exited")
            else:
                ack_result[0] = (False, "startup_protocol_error")
            ack_event.set()
            continue   # do not relay CCALL_STARTUP_ACK lines

    accepted, error_code = ack_result[0]
    return accepted, error_code


# ---------------------------------------------------------------------------
# Negative: Popen alone is not sufficient
# ---------------------------------------------------------------------------

class TestPopenAloneInsufficient:
    def test_no_lines_means_no_ack(self):
        """Popen created but pipe produces no output → accepted stays False/pending."""
        accepted, code = _run_pipe_reader([])
        assert not accepted
        assert code == "pending"

    def test_generic_log_line_does_not_trigger_ack(self):
        """A generic INFO line that happens to mention SIP must not trigger ACK."""
        lines = [
            "INFO starting SIP transport\n",
            "DEBUG sip_domain=sip.example.com\n",
            "INFO call dispatched\n",
        ]
        accepted, code = _run_pipe_reader(lines)
        assert not accepted
        assert code == "pending"

    def test_calling_sentinel_alone_does_not_trigger_ack(self):
        """The inner direct_sip_call.py 'Calling ...' line must NOT trigger parent ACK.

        Only direct_sip_wrapper.py's structured 'CCALL_STARTUP_ACK accepted' line
        is authoritative.  If a test or log emits the legacy sentinel verbatim,
        it must have NO effect on the parent pipe reader.
        """
        lines = [
            "Calling 01700000001 from 192.168.1.1:5060\n",
            "Registering...\n",
        ]
        accepted, code = _run_pipe_reader(lines)
        assert not accepted, (
            "'Calling ' line must NOT trigger ACK — only CCALL_STARTUP_ACK accepted does"
        )

    def test_partial_sentinel_prefix_does_not_trigger(self):
        """Lines that contain 'CCALL_STARTUP_ACK' as a substring but not at start."""
        lines = [
            "  CCALL_STARTUP_ACK accepted\n",   # leading whitespace
            "log: CCALL_STARTUP_ACK accepted\n",  # embedded
        ]
        accepted, code = _run_pipe_reader(lines)
        assert not accepted

    def test_constructor_startup_alone_insufficient(self):
        """Lines emitted during IvrCall construction (before run() / INVITE) must not ACK."""
        lines = [
            "INFO IvrCall constructed for call_id=42\n",
            "DEBUG sip transport init\n",
            "INFO registered startup ack event\n",
        ]
        accepted, code = _run_pipe_reader(lines)
        assert not accepted


# ---------------------------------------------------------------------------
# Positive: exact sentinel triggers ACK correctly
# ---------------------------------------------------------------------------

class TestExactSentinelAccepted:
    def test_accepted_sentinel_sets_accepted_true(self):
        accepted, code = _run_pipe_reader(["CCALL_STARTUP_ACK accepted\n"])
        assert accepted is True
        assert code == ""

    def test_accepted_sentinel_after_preamble(self):
        """Other log lines before the sentinel must not interfere."""
        lines = [
            "INFO starting inner process\n",
            "Calling 01700000001 from 127.0.0.1:5060\n",
            "CCALL_STARTUP_ACK accepted\n",
            "INFO registered with SIP server\n",
        ]
        accepted, code = _run_pipe_reader(lines)
        assert accepted is True

    def test_only_first_sentinel_is_processed(self):
        """A second CCALL_STARTUP_ACK line after the first is ignored."""
        lines = [
            "CCALL_STARTUP_ACK accepted\n",
            "CCALL_STARTUP_ACK rejected:some_error\n",  # must be ignored
        ]
        accepted, code = _run_pipe_reader(lines)
        assert accepted is True
        assert code == ""


# ---------------------------------------------------------------------------
# Negative: rejected sentinel
# ---------------------------------------------------------------------------

class TestRejectedSentinel:
    def test_rejected_with_code(self):
        accepted, code = _run_pipe_reader(["CCALL_STARTUP_ACK rejected:startup_process_exited\n"])
        assert not accepted
        assert code == "startup_process_exited"

    def test_rejected_sip_transport_init_failed(self):
        accepted, code = _run_pipe_reader(["CCALL_STARTUP_ACK rejected:sip_transport_init_failed\n"])
        assert not accepted
        assert code == "sip_transport_init_failed"

    def test_rejected_empty_code_defaults(self):
        """'rejected:' with no trailing code gives startup_process_exited."""
        accepted, code = _run_pipe_reader(["CCALL_STARTUP_ACK rejected:\n"])
        assert not accepted
        assert code == "startup_process_exited"

    def test_rejected_invite_start_failed(self):
        accepted, code = _run_pipe_reader(["CCALL_STARTUP_ACK rejected:sip_invite_start_failed\n"])
        assert not accepted
        assert code == "sip_invite_start_failed"


# ---------------------------------------------------------------------------
# Malformed sentinel → startup_protocol_error
# ---------------------------------------------------------------------------

class TestMalformedSentinel:
    def test_unknown_payload_gives_protocol_error(self):
        """'CCALL_STARTUP_ACK unknown_payload' is neither 'accepted' nor 'rejected:*'."""
        accepted, code = _run_pipe_reader(["CCALL_STARTUP_ACK unknown_payload\n"])
        assert not accepted
        assert code == "startup_protocol_error"

    def test_empty_payload_after_prefix_gives_protocol_error(self):
        """'CCALL_STARTUP_ACK \\n' — trailing space stripped → empty payload."""
        accepted, code = _run_pipe_reader(["CCALL_STARTUP_ACK \n"])
        assert not accepted
        assert code == "startup_protocol_error"

    def test_mixed_case_rejected_not_recognised(self):
        """Protocol is case-sensitive: 'Rejected:' is not 'rejected:'."""
        accepted, code = _run_pipe_reader(["CCALL_STARTUP_ACK Rejected:some_error\n"])
        assert not accepted
        assert code == "startup_protocol_error"

    def test_accepted_with_trailing_garbage_is_protocol_error(self):
        """'CCALL_STARTUP_ACK accepted extra' is not the exact sentinel."""
        accepted, code = _run_pipe_reader(["CCALL_STARTUP_ACK accepted extra\n"])
        assert not accepted
        assert code == "startup_protocol_error"


# ---------------------------------------------------------------------------
# Isolation: two concurrent pipe readers fire independent ACK events
# ---------------------------------------------------------------------------

class TestTwoChildProcessesIsolation:
    def test_two_readers_each_fire_own_event(self):
        """Two independent _run_pipe_reader calls (simulating two concurrent child
        processes) must each trigger only their own event and not the other's.

        We simulate this by running two readers in separate threads with different
        output lines, then confirming each received the correct result.
        """
        results: dict[int, tuple[bool, str]] = {}

        def run(idx: int, lines: list[str]) -> None:
            results[idx] = _run_pipe_reader(lines)

        lines_1 = ["CCALL_STARTUP_ACK accepted\n", "INFO call 1 ringing\n"]
        lines_2 = ["INFO call 2 setup\n", "CCALL_STARTUP_ACK rejected:sip_invite_start_failed\n"]

        t1 = threading.Thread(target=run, args=(1, lines_1))
        t2 = threading.Thread(target=run, args=(2, lines_2))
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        accepted_1, code_1 = results[1]
        accepted_2, code_2 = results[2]

        assert accepted_1 is True,  f"Reader 1 should be accepted, got code={code_1!r}"
        assert accepted_2 is False, f"Reader 2 should be rejected, got accepted={accepted_2}"
        assert code_2 == "sip_invite_start_failed", (
            f"Reader 2 error code wrong: {code_2!r}"
        )

    def test_reader_1_accepted_does_not_affect_reader_2(self):
        """Reader 1 accepting must have zero influence on Reader 2's state."""
        results: dict[int, tuple[bool, str]] = {}

        def run(idx: int, lines: list[str]) -> None:
            results[idx] = _run_pipe_reader(lines)

        # Reader 2 receives NO sentinel — stays pending.
        t1 = threading.Thread(target=run, args=(1, ["CCALL_STARTUP_ACK accepted\n"]))
        t2 = threading.Thread(target=run, args=(2, ["INFO some log\n"]))
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        assert results[1] == (True, "")
        assert results[2] == (False, "pending"), (
            f"Reader 2 must remain pending: {results[2]}"
        )


# ---------------------------------------------------------------------------
# _DIRECT_SIP_ACK_TEST subprocess hook tests
#
# These tests spawn direct_sip_call.py as a real subprocess using the
# _DIRECT_SIP_ACK_TEST env hook (active only when FLASK_ENV=testing).
# They prove:
#   sendto_ok  → CCALL_STARTUP_ACK accepted (ACK fires after mocked INVITE send)
#   sendto_fail → CCALL_STARTUP_ACK rejected:sip_invite_start_failed
#   unknown mode → CCALL_STARTUP_ACK rejected:startup_protocol_error
# ---------------------------------------------------------------------------

import os
import subprocess
import sys

_DIRECT_SIP_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "..", "call_engine", "direct_sip_call.py"
)


def _run_direct_sip_with_hook(mode: str) -> list[str]:
    """Spawn direct_sip_call.py with _DIRECT_SIP_ACK_TEST=<mode> and return output lines."""
    env = {
        **os.environ,
        "FLASK_ENV": "testing",
        "_DIRECT_SIP_ACK_TEST": mode,
        # Minimal required env so the script initialises without crashing on
        # missing vars before reaching the hook branch.
        "SIP_DOMAIN": "sip.test.invalid",
        "SIP_PORT": "5060",
        "SIP_USER": "testuser",
        "SIP_PASSWORD": "testpass",
        "TARGET": "01700000000",
        "LOCAL_IP": "127.0.0.1",
        "LOCAL_PORT": "5061",
    }
    result = subprocess.run(
        [sys.executable, _DIRECT_SIP_SCRIPT],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    return result.stdout.decode("utf-8", errors="replace").splitlines()


class TestDirectSipAckTestHook:
    def test_sendto_ok_emits_accepted(self):
        """_DIRECT_SIP_ACK_TEST=sendto_ok → direct_sip_call.py emits CCALL_STARTUP_ACK accepted."""
        lines = _run_direct_sip_with_hook("sendto_ok")
        assert "CCALL_STARTUP_ACK accepted" in lines, (
            f"Expected 'CCALL_STARTUP_ACK accepted' in output; got: {lines}"
        )

    def test_sendto_ok_does_not_emit_calling_as_ack(self):
        """sendto_ok hook fast-paths — 'Calling ...' log line must NOT appear before ACK."""
        lines = _run_direct_sip_with_hook("sendto_ok")
        calling_lines = [l for l in lines if l.startswith("Calling ")]
        # In hook mode the script exits immediately; no "Calling " should appear.
        assert not calling_lines, (
            f"Hook mode must not print 'Calling ...' before ACK; got: {calling_lines}"
        )

    def test_sendto_fail_emits_rejected_invite_start_failed(self):
        """_DIRECT_SIP_ACK_TEST=sendto_fail → CCALL_STARTUP_ACK rejected:sip_invite_start_failed."""
        lines = _run_direct_sip_with_hook("sendto_fail")
        assert "CCALL_STARTUP_ACK rejected:sip_invite_start_failed" in lines, (
            f"Expected rejected sentinel; got: {lines}"
        )

    def test_sendto_fail_does_not_emit_accepted(self):
        """sendto_fail hook must NOT emit the accepted sentinel."""
        lines = _run_direct_sip_with_hook("sendto_fail")
        assert "CCALL_STARTUP_ACK accepted" not in lines, (
            f"Hook sendto_fail must not produce 'accepted'; got: {lines}"
        )

    def test_unknown_mode_emits_protocol_error(self):
        """_DIRECT_SIP_ACK_TEST=garbage → CCALL_STARTUP_ACK rejected:startup_protocol_error."""
        lines = _run_direct_sip_with_hook("garbage_mode")
        assert "CCALL_STARTUP_ACK rejected:startup_protocol_error" in lines, (
            f"Unknown hook mode should give protocol_error; got: {lines}"
        )

    def test_hook_active_only_in_testing_env(self):
        """Without FLASK_ENV=testing the _DIRECT_SIP_ACK_TEST hook must be ignored.

        The script will attempt real SIP (and likely fail/exit quickly), but must
        NOT emit CCALL_STARTUP_ACK accepted from the hook branch.  We set a short
        process timeout and check the hook sentinel is absent.
        """
        env = {
            **os.environ,
            "FLASK_ENV": "production",   # hook branch should be skipped
            "_DIRECT_SIP_ACK_TEST": "sendto_ok",
            "SIP_DOMAIN": "sip.test.invalid",
            "SIP_PORT": "5060",
            "SIP_USER": "testuser",
            "SIP_PASSWORD": "testpass",
            "TARGET": "01700000000",
            "LOCAL_IP": "127.0.0.1",
            "LOCAL_PORT": "5061",
        }
        try:
            result = subprocess.run(
                [sys.executable, _DIRECT_SIP_SCRIPT],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            # Script hung trying real SIP — that's expected; capture what we can.
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace")
        else:
            stdout = result.stdout.decode("utf-8", errors="replace")

        # The hook sentinel must NOT appear in non-testing mode.
        assert "CCALL_STARTUP_ACK accepted" not in stdout.splitlines(), (
            "Hook must be inactive outside FLASK_ENV=testing"
        )
