# E:\c-call-ivr\call_runner.py
"""Unified Call Orchestrator.

Loads IVR flows or direct one-way configurations, executes calls using
either the in-process `ivr_engine.IvrCall` (for IVR) or `direct_sip_call.py`
as a subprocess (for direct calls), and logs outcomes/events to SQLite.
"""
from __future__ import annotations

import logging
import os
import sys
import subprocess
import threading
import time
import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("ccall")

import db
from call_engine.audio import format_dial_number, normalize_phone_number, ulaw_path_for, audio_duration_seconds
from call_engine.ivr_engine import Event, Flow, IvrCall, Node
from call_engine import active_calls as _live
from app.monitoring import ACTIVE_CALLS, CALLS_TOTAL, CALL_DURATION

BASE_DIR = Path(__file__).resolve().parent
if BASE_DIR.name == "call_engine":
    BASE_DIR = BASE_DIR.parent
INSTANCE_DIR = BASE_DIR / "instance"

# Background worker threads
_executor = ThreadPoolExecutor(max_workers=int(os.environ.get("CALL_WORKERS", "4")))
_lock = threading.Lock()

_delayed_removals = set()

# ── Startup acknowledgement channel ────────────────────────────────────────────
# Allows the queue worker (order_service.queue_confirmation_call) to wait for
# bounded confirmation that the IVR executor task actually started its SIP
# session before the job is marked completed.
#
# Lifecycle:
#   1. order_service.queue_confirmation_call() calls register_startup_ack(call_id)
#      to allocate a threading.Event keyed by call_id.
#   2. For IVR calls: _run_ivr_thread signals accepted via signal_startup_ack()
#      inside on_event("ringing") — which fires INSIDE call.run() AFTER the SIP
#      INVITE has been sent by ivr_engine.py.  Constructor alone is NOT sufficient.
#   3. For direct calls: _pipe_reader signals accepted after receiving the
#      CCALL_STARTUP_ACK accepted sentinel from direct_sip_wrapper.py.
#   4. order_service waits on the event for TELEPHONY_STARTUP_TIMEOUT_SECONDS.
#   5. Timeout → startup_state='timeout', treated as retryable.
#   6. The event is removed from the dict when the wait completes (success or timeout).

_startup_ack_lock = threading.Lock()
_startup_ack_events: dict[int, threading.Event] = {}
_startup_ack_results: dict[int, tuple[bool, str]] = {}  # call_id → (accepted, error_code)


# ── Testing-only fault injection ──────────────────────────────────────────────
# Activated by the _CALL_RUNNER_FAULT_POINT env var (set by run_call_worker.py
# when --test-fault-point names a point that fires inside the SIP dispatch path).
# Only active when FLASK_ENV=testing; silently ignored otherwise.

def _maybe_fault(point: str) -> None:
    """Exit the process at a named fault point — testing only.

    This is called from WITHIN the worker process (not a separate thread) so
    os._exit() terminates the worker immediately without cleanup, matching a
    real crash.  The fault point name is compared to _CALL_RUNNER_FAULT_POINT.
    """
    if os.environ.get("FLASK_ENV") != "testing":
        return
    if os.environ.get("_CALL_RUNNER_FAULT_POINT", "") == point:
        logger.warning("[call_runner] fault injection at point=%s — simulating crash", point)
        os._exit(1)


def register_startup_ack(call_id: int) -> threading.Event:
    """Register a startup acknowledgement event for call_id. Called by order_service before
    submitting to the executor. Returns the event so the caller can wait on it."""
    ev = threading.Event()
    with _startup_ack_lock:
        _startup_ack_events[call_id] = ev
        _startup_ack_results[call_id] = (False, "pending")
    return ev


def signal_startup_ack(call_id: int, accepted: bool, error_code: str = "") -> None:
    """Signal the startup acknowledgement event for call_id. Called from _run_ivr_thread
    when the IvrCall object is constructed (accepted=True) or when startup fails before
    that point (accepted=False). Safe to call even if no event was registered."""
    with _startup_ack_lock:
        _startup_ack_results[call_id] = (accepted, error_code)
        ev = _startup_ack_events.get(call_id)
    if ev is not None:
        ev.set()


def pop_startup_ack_result(call_id: int) -> tuple[bool, str]:
    """Return and remove the startup ack result. Returns (False, 'no_event') if
    no event was registered (e.g. direct calls that bypass the queue worker)."""
    with _startup_ack_lock:
        _startup_ack_events.pop(call_id, None)
        return _startup_ack_results.pop(call_id, (False, "no_event"))

def _remove_call_delayed(call_id: int, status: str) -> None:
    # Update status to final state
    _live.update_active_call(call_id, status=status)
    
    with _lock:
        if call_id in _delayed_removals:
            return
        _delayed_removals.add(call_id)
        
    keep_secs = int(os.environ.get("LIVE_CALLS_KEEP_COMPLETED_SECONDS", "0"))
    if keep_secs > 0:
        def delayed():
            time.sleep(keep_secs)
            _live.remove_active_call(call_id)
            with _lock:
                _delayed_removals.discard(call_id)
        threading.Thread(target=delayed, daemon=True).start()
    else:
        _live.remove_active_call(call_id)
        with _lock:
            _delayed_removals.discard(call_id)

# Currently active IVR calls (by database call_id) to allow live hang-up
_active_ivr_calls: dict[int, IvrCall] = {}

# Currently active direct-call subprocess handles (by database call_id),
# so Quick Call / Live Monitor hangup can actually terminate them — the
# old subprocess.run() blocked until exit with no handle to cancel.
# Value is (Popen, cancel_requested_event) — the event lets the waiting
# thread distinguish "we killed it" (-> status canceled) from "it crashed
# on its own" (-> status failed).
_active_direct_procs: dict[int, tuple[subprocess.Popen, threading.Event]] = {}


def cancel_call(call_id: int) -> bool:
    """Request a live call (IVR or direct) to hang up. Returns True if
    the call was active and a cancel was actually issued."""
    ivr_call = _active_ivr_calls.get(call_id)
    if ivr_call is not None:
        ivr_call.request_cancel()
        return True
    entry = _active_direct_procs.get(call_id)
    if entry is not None:
        proc, cancel_event = entry
        if proc.poll() is None:
            cancel_event.set()
            proc.terminate()
            return True
    return False


def active_call_ids() -> list[int]:
    """Return list of active live call IDs (IVR + direct)."""
    return list(_active_ivr_calls.keys()) + list(_active_direct_procs.keys())


# --- Settings & SIP helpers ---

def sip_config(tenant_id: int | None = None) -> dict[str, str]:
    tenant_settings = {}
    if tenant_id:
        row = db.query_one("SELECT settings FROM tenants WHERE id = ?", (tenant_id,))
        if row and row["settings"]:
            settings_field = row["settings"]
            if isinstance(settings_field, str):
                try:
                    tenant_settings = json.loads(settings_field)
                except Exception:
                    pass
            elif isinstance(settings_field, dict):
                tenant_settings = settings_field

    def get_val(key: str, default: str = "") -> str:
        # Check tenant settings (upper or lower case), then global app_settings, then environment
        val = tenant_settings.get(key) or tenant_settings.get(key.lower())
        if val:
            return str(val)
        return db.get_setting(key, default)

    return {
        "SIP_DOMAIN": get_val("SIP_DOMAIN"),
        "SIP_PORT": get_val("SIP_PORT", "5060"),
        "SIP_USER": get_val("SIP_USER"),
        "SIP_PASSWORD": get_val("SIP_PASSWORD"),
    }


def sip_ready(tenant_id: int | None = None) -> bool:
    cfg = sip_config(tenant_id)
    return all(cfg.get(k) for k in ("SIP_DOMAIN", "SIP_USER", "SIP_PASSWORD"))


# --- Contacts / Customers ---

def upsert_contact(number: str, name: str = "") -> int:
    """Retrieve existing contact ID or insert a new contact, returning the ID."""
    row = db.query_one("SELECT id, name FROM contacts WHERE number = ?", (number,))
    if row:
        contact_id = row["id"]
        if name and name != row["name"]:
            db.execute("UPDATE contacts SET name = ? WHERE id = ?", (name, contact_id))
        return contact_id
    return db.insert_returning_id(
        "INSERT INTO contacts (number, name) VALUES (?, ?)",
        (number, name),
    )


def _attempt_no(contact_id: int) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM calls WHERE contact_id = ?", (contact_id,)
    )
    return int(row["n"]) + 1 if row else 1


# --- IVR Flow Loader ---

def load_flow(flow_id: int) -> Flow:
    flow_row = db.query_one("SELECT * FROM flows WHERE id = ?", (flow_id,))
    if not flow_row:
        raise ValueError(f"Flow {flow_id} not found")
    node_rows = db.query("SELECT * FROM flow_nodes WHERE flow_id = ?", (flow_id,))
    trans_rows = db.query(
        "SELECT * FROM flow_transitions WHERE node_id IN "
        "(SELECT id FROM flow_nodes WHERE flow_id = ?)",
        (flow_id,),
    )
    nodes: dict[int, Node] = {}
    for r in node_rows:
        transfer_number = r["transfer_number"]
        if transfer_number and not transfer_number.lower().startswith("sip:"):
            transfer_number = format_dial_number(
                transfer_number,
                db.get_setting("ASTERISK_DIAL_FORMAT", "local_bd"),
            )
        nodes[r["id"]] = Node(
            id=r["id"], name=r["name"], node_type=r["node_type"],
            prompt_audio=r["prompt_audio"], transfer_number=transfer_number,
            gather_timeout_seconds=r["gather_timeout_seconds"],
            max_retries=r["max_retries"],
        )
    for t in trans_rows:
        if t["node_id"] in nodes and t["next_node_id"] and t["next_node_id"] in nodes:
            nodes[t["node_id"]].transitions[t["digit"]] = t["next_node_id"]

    root = flow_row["root_node_id"]
    if root is not None and root not in nodes:
        raise ValueError(f"Flow {flow_id} root_node_id={root} does not belong to flow")
    if root is None:
        root = node_rows[0]["id"] if node_rows else None
    if root is None:
        raise ValueError("Flow has no nodes")
    return Flow(id=flow_id, root_node_id=root, nodes=nodes, tenant_id=flow_row.get("tenant_id"))


# --- Background Workers ---

def _persist_event(call_id: int, ev: Event) -> None:
    db.execute(
        """
        INSERT INTO call_events
            (call_id, seq, event_type, node_id, digit, response_ms, detail, at_offset_ms)
        VALUES (?,
                (SELECT COALESCE(MAX(seq), 0) + 1 FROM call_events WHERE call_id = ?),
                ?, ?, ?, ?, ?, ?)
        """,
        (call_id, call_id, ev.type, ev.node_id, ev.digit, ev.response_ms,
         ev.detail, ev.at_offset_ms),
    )


def _run_ivr_thread(call_id: int, target_dial: str, flow: Flow, sip: dict[str, str], tenant_id: int | None = None, order_id: int | None = None) -> None:
    """Runs a single interactive IVR call in a background thread."""
    answered_at_monotonic: list[float | None] = [None]
    # Startup ACK is signalled once, in on_event("ringing"), after the SIP INVITE
    # has been sent — not at constructor time (which only binds sockets) and not
    # at executor submission time.
    _ack_done: list[bool] = [False]

    def _signal_ack(accepted: bool, error_code: str = "") -> None:
        if _ack_done[0]:
            return
        _ack_done[0] = True
        signal_startup_ack(call_id, accepted=accepted, error_code=error_code)
        if accepted:
            try:
                db.execute(
                    "UPDATE calls SET startup_state = 'accepted', startup_accepted_at = now() WHERE id = %s",
                    (call_id,),
                )
            except Exception:
                pass
        else:
            try:
                db.execute(
                    "UPDATE calls SET startup_state = 'rejected', startup_rejected_at = now(), "
                    "startup_error_code = %s WHERE id = %s",
                    (error_code, call_id),
                )
            except Exception:
                pass

    def on_event(ev: Event) -> None:
        try:
            _persist_event(call_id, ev)
            if ev.type in ("ringing", "answered"):
                db.execute(
                    "UPDATE calls SET status = ?, "
                    "answered_at = CASE WHEN ? = 'answered' THEN now() ELSE answered_at END "
                    "WHERE id = ?",
                    (ev.type, ev.type, call_id),
                )
                if ev.type == "answered":
                    answered_at_monotonic[0] = time.monotonic()
                    _live.update_active_call(call_id, status="answered", answered_at=datetime.datetime.now(datetime.timezone.utc))
                else:
                    _live.update_active_call(call_id, status="ringing")
                # "ringing" fires in IvrCall.run() immediately after the SIP INVITE
                # is sent — that is the first reliable dial-start confirmation.
                # Signal ACK here (after socket init + REGISTER + INVITE sent).
                if ev.type == "ringing":
                    _signal_ack(accepted=True)
            elif ev.type == "dtmf" and ev.digit:
                # [SAAS STEP 11] DTMF callback received
                import logging
                logging.getLogger("ccall").info(f"[SAAS STEP 11] DTMF callback received: call_id={call_id}, digit=[redacted]")
                _live.update_active_call(call_id, dtmf_digit=ev.digit, status="waiting_dtmf")

            # Live talk_seconds ticks off whatever events IvrCall emits
            # while the call is answered (node enters, audio plays, DTMF,
            # etc.) — no separate polling thread needed, and this stays
            # accurate to within one event's worth of latency, which in
            # practice fires much more often than once a second.
            if answered_at_monotonic[0] is not None:
                _live.update_active_call(call_id, talk_seconds=int(time.monotonic() - answered_at_monotonic[0]))
        except Exception:
            pass

    record = os.environ.get("RECORD_CALLS", "1") == "1"
    try:
        call = IvrCall(target_dial, flow, sip, on_event, record=record, tenant_id=tenant_id)
    except Exception as _startup_exc:
        # IvrCall() socket/transport initialization failed.
        _signal_ack(False, error_code="sip_transport_init_failed")
        raise

    # Register call globally to enable UI cancellations
    _active_ivr_calls[call_id] = call
    _live.set_cancel_handler(call_id, lambda cid=call_id: cancel_call(cid))
    ACTIVE_CALLS.inc()

    try:
        result = call.run()
        # [SAAS STEP 10] Provider response
        import logging
        logging.getLogger("ccall").info(f"[SAAS STEP 10] Provider response: call_id={call_id}, status={result.status}, SIP Final Status={result.sip_final_status}")
    except Exception as _run_exc:
        # run() raised before "ringing" was emitted — INVITE or REGISTER failed.
        _signal_ack(False, error_code="sip_invite_start_failed")
        raise
    finally:
        # Catch any path that never signalled ACK (e.g. unexpected early exit).
        _signal_ack(False, error_code="startup_internal_error")
        _active_ivr_calls.pop(call_id, None)
        ACTIVE_CALLS.dec()

    # Instrument prometheus metrics on call end
    CALLS_TOTAL.labels(result=result.status, tenant_id=str(tenant_id or "")).inc()
    CALL_DURATION.observe(result.talk_seconds)
        
    final_dtmf_digit = _extract_dtmf_digit(call_id)
    db.execute(
        """
        UPDATE calls SET
            status = ?, sip_final_status = ?, ring_seconds = ?, talk_seconds = ?,
            total_seconds = ?, digits_pressed = ?, drop_node_id = ?, last_node_id = ?,
            reached_terminal = ?, recording_path = ?, error = ?, dtmf_digit = ?, ended_at = now()
        WHERE id = ?
        """,
        (result.status, result.sip_final_status or "", result.ring_seconds, result.talk_seconds,
         result.total_seconds, result.digits_pressed, result.drop_node_id, result.last_node_id,
         True if result.reached_terminal else False, result.recording_path or "", result.error or "",
         final_dtmf_digit, call_id),
    )

    # Final status reached -> drop from the in-memory live registry, DB
    # status remains as the durable record (req. #7).
    _remove_call_delayed(call_id, result.status)

    # Bill every call here, regardless of call type — order calls used to
    # only bill inside update_status_from_call() below, which never ran
    # for direct/manual calls (no order_id), so those never deducted
    # wallet. finalize_call_billing() is idempotent, so order calls get
    # billed once here and update_status_from_call()'s own call is a no-op.
    try:
        from app.services.billing_service import finalize_call_billing
        finalize_call_billing(call_id)
    except Exception as exc:
        logger.error("[call_runner] billing failed for call %s: %s", call_id, exc)

    if order_id and tenant_id:
        # Import here to avoid circular import
        from app.services.order_service import update_status_from_call
        try:
            update_status_from_call(
                order_id=order_id,
                tenant_id=tenant_id,
                call_result=result.status,
                dtmf_digit=final_dtmf_digit,
                talk_seconds=result.talk_seconds,
                call_id=call_id
            )
        except Exception as exc:
            logger.error("[call_runner] order update failed: %s", exc)


def _extract_dtmf_digit(call_id: int) -> str | None:
    row = db.query_one(
        "SELECT digit FROM call_events WHERE call_id = ? AND event_type = 'dtmf' ORDER BY seq DESC LIMIT 1",
        (call_id,)
    )
    return row["digit"] if row else None


def _run_direct_subprocess(call_id: int, dial_target: str, audio_path: str, repeat_count: int, max_seconds: int, sip_cfg: dict, tenant_id: int | None = None) -> None:
    """Spawns direct_sip_call.py in a background thread to prevent UI blocks.
    Uses Popen (not run()) so the process handle can be stored and actually
    terminated by a Live Monitor hangup — subprocess.run() blocks until
    exit with no handle to cancel, so direct calls could never be hung up."""
    status = "failed"
    env = os.environ.copy()
    env.update(sip_cfg)

    db.execute("UPDATE calls SET status = 'ringing' WHERE id = ?", (call_id,))
    _live.update_active_call(call_id, status="ringing")

    log_file_path = INSTANCE_DIR / "direct-sip.log"
    start_time = time.monotonic()
    cancel_event = threading.Event()

    # Per-process ACK IPC: each subprocess gets an isolated stdout PIPE so a
    # concurrent call's output can never trigger this call's ACK.  A background
    # thread drains the pipe, relays each line to the shared log file for
    # persistence, and signals _ack_event when the startup sentinel appears.
    # Popen alone is insufficient — the parent must actively read the pipe.
    _ack_event = threading.Event()
    _ack_result: list[tuple[bool, str] | None] = [None]  # (accepted, error_code)
    answered_at_monotonic: float | None = None
    hangup_finalized = False

    ACTIVE_CALLS.inc()
    _direct_startup_timeout = int(os.environ.get("TELEPHONY_STARTUP_TIMEOUT_SECONDS", "10"))
    _startup_ack_signalled = False
    proc: subprocess.Popen | None = None
    try:
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(BASE_DIR / "call_engine" / "direct_sip_wrapper.py"),
                    dial_target,
                    str(audio_path),
                    str(repeat_count),
                    str(max_seconds)
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr so one thread handles all output
            )
        except OSError as _popen_err:
            signal_startup_ack(call_id, accepted=False, error_code="subprocess_start_failed")
            _startup_ack_signalled = True
            db.execute(
                "UPDATE calls SET startup_state = 'rejected', startup_rejected_at = now(), "
                "startup_error_code = 'subprocess_start_failed' WHERE id = ?", (call_id,)
            )
            raise

        _active_direct_procs[call_id] = (proc, cancel_event)
        _live.set_cancel_handler(call_id, lambda cid=call_id: cancel_call(cid))

        def _pipe_reader() -> None:
            """Drain proc.stdout, relay to log, signal ACK and track call state."""
            nonlocal answered_at_monotonic, hangup_finalized
            try:
                with log_file_path.open("ab", buffering=0) as log_f:
                    for raw_line in proc.stdout:
                        log_f.write(raw_line)
                        log_f.flush()
                        line = raw_line.decode("utf-8", errors="replace")

                        # Structured ACK from direct_sip_wrapper.py — the ONLY
                        # line that triggers the startup ACK event.  Generic log
                        # lines (including "Calling …" from the inner script) are
                        # relayed by the wrapper but do NOT reach here as ACK triggers.
                        if not _ack_event.is_set() and line.startswith("CCALL_STARTUP_ACK "):
                            payload = line[len("CCALL_STARTUP_ACK "):].strip()
                            if payload == "accepted":
                                _ack_result[0] = (True, "")
                            elif payload.startswith("rejected:"):
                                _err = payload[len("rejected:"):]
                                _ack_result[0] = (False, _err or "startup_process_exited")
                            else:
                                _ack_result[0] = (False, "startup_protocol_error")
                            _ack_event.set()
                            continue  # do not relay CCALL_STARTUP_ACK lines to log

                        # Answer detection
                        if answered_at_monotonic is None and "Call answered; sending audio" in line:
                            answered_at_monotonic = time.monotonic()
                            _live.update_active_call(
                                call_id, status="answered",
                                answered_at=datetime.datetime.now(datetime.timezone.utc),
                            )

                        # Early hangup finalization: remote BYE arrives before
                        # the subprocess's own teardown (up to ~4.5 s) completes.
                        if not hangup_finalized and (
                            "Remote hung up" in line or "Call completed (remote ended)" in line
                        ):
                            hangup_finalized = True
                            now = time.monotonic()
                            _talk = int(now - answered_at_monotonic) if answered_at_monotonic else 0
                            _elapsed = int(now - start_time)
                            db.execute(
                                """
                                UPDATE calls SET
                                    status = 'completed',
                                    talk_seconds = ?,
                                    total_seconds = ?,
                                    ended_at = now()
                                WHERE id = ?
                                """,
                                (_talk, _elapsed, call_id)
                            )
                            _remove_call_delayed(call_id, "completed")
                            CALLS_TOTAL.labels(result="completed", tenant_id=str(tenant_id or "")).inc()
                            CALL_DURATION.observe(_talk)
                            try:
                                from app.services.billing_service import finalize_call_billing
                                finalize_call_billing(call_id)
                            except Exception as _be:
                                logger.error("[call_runner] billing failed for call %s: %s", call_id, _be)
            except Exception:
                pass
            finally:
                # Process exited (or pipe closed) without emitting startup sentinel.
                if not _ack_event.is_set():
                    _ack_result[0] = (False, "startup_process_exited")
                    _ack_event.set()

        _reader = threading.Thread(target=_pipe_reader, daemon=True, name=f"pipe-reader-{call_id}")
        _reader.start()

        # Block until startup ACK arrives on the isolated pipe (or timeout).
        _acked = _ack_event.wait(timeout=_direct_startup_timeout)
        if not _acked:
            _ack_result[0] = (False, "startup_timeout")
            _ack_event.set()

        _accepted, _err_code = _ack_result[0]
        _startup_ack_signalled = True
        signal_startup_ack(call_id, accepted=_accepted, error_code=_err_code)
        if _accepted:
            db.execute(
                "UPDATE calls SET startup_state = 'accepted', startup_accepted_at = now() WHERE id = ?",
                (call_id,),
            )
        else:
            db.execute(
                "UPDATE calls SET startup_state = 'rejected', startup_rejected_at = now(), "
                "startup_error_code = ? WHERE id = ?", (_err_code, call_id)
            )

        # Poll instead of a single blocking wait() — lets us tick talk_seconds.
        # Answer/hangup state is updated by _pipe_reader; just read shared vars here.
        deadline = start_time + max_seconds + int(os.environ.get("SIP_PROCESS_TIMEOUT_SECONDS", "90")) + 30
        returncode = None
        while True:
            try:
                returncode = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if answered_at_monotonic is not None:
                    _live.update_active_call(call_id, talk_seconds=int(now - answered_at_monotonic))
                if now >= deadline:
                    raise

        elapsed = int(time.monotonic() - start_time)
        if hangup_finalized:
            # Already finalized live the instant the log showed the
            # remote hung up — proc.wait() just confirmed the subprocess's
            # own (slower) teardown actually finished too. Don't redo the
            # DB update / metrics / billing a second time.
            status = "completed"
            talk_seconds = int((answered_at_monotonic and time.monotonic() - answered_at_monotonic) or 0)
        elif cancel_event.is_set():
            status = "canceled"
            talk_seconds = 0
            db.execute(
                """
                UPDATE calls SET
                    status = 'canceled',
                    total_seconds = ?,
                    ended_at = now(),
                    error = 'Call canceled by user.'
                WHERE id = ?
                """,
                (elapsed, call_id)
            )
        elif returncode == 0:
            status = 'completed'
            talk_seconds = max_seconds
            db.execute(
                """
                UPDATE calls SET
                    status = 'completed',
                    talk_seconds = ?,
                    total_seconds = ?,
                    ended_at = now()
                WHERE id = ?
                """,
                (talk_seconds, elapsed, call_id)
            )
        else:
            status = 'failed'
            talk_seconds = 0
            db.execute(
                """
                UPDATE calls SET
                    status = 'failed',
                    total_seconds = ?,
                    ended_at = now(),
                    error = 'Call failed or was not answered.'
                WHERE id = ?
                """,
                (elapsed, call_id)
            )
    except subprocess.TimeoutExpired:
        if not _startup_ack_signalled:
            signal_startup_ack(call_id, accepted=False, error_code="startup_timeout")
        if proc is not None:
            proc.kill()
        elapsed = int(time.monotonic() - start_time)
        status = 'no_answer'
        talk_seconds = 0
        db.execute(
            """
            UPDATE calls SET
                status = 'no_answer',
                total_seconds = ?,
                ended_at = now(),
                error = 'Call timed out (no answer).'
            WHERE id = ?
            """,
            (elapsed, call_id)
        )
    except Exception as exc:
        if not _startup_ack_signalled:
            signal_startup_ack(call_id, accepted=False, error_code="startup_internal_error")
        elapsed = int(time.monotonic() - start_time)
        status = 'failed'
        talk_seconds = 0
        db.execute(
            """
            UPDATE calls SET
                status = 'failed',
                total_seconds = ?,
                ended_at = now(),
                error = ?
            WHERE id = ?
            """,
            (elapsed, str(exc), call_id)
        )
    finally:
        ACTIVE_CALLS.dec()
        _active_direct_procs.pop(call_id, None)
        _remove_call_delayed(call_id, status)

    if hangup_finalized:
        return  # already recorded metrics + billed when the hangup was first detected

    # Instrument prometheus metrics on call end
    CALLS_TOTAL.labels(result=status, tenant_id=str(tenant_id or "")).inc()
    CALL_DURATION.observe(talk_seconds)

    # Direct/manual calls have no order_id, so they never went through
    # order_service.update_status_from_call() — billing must happen here.
    # Canceled calls bill 0 unless answered+billable per calculate_charge_and_duration's
    # own charge_failed_calls rule (finalize_call_billing handles this — "canceled"
    # is in its TERMINAL_STATUSES set, billed same as any non-completed result).
    try:
        from app.services.billing_service import finalize_call_billing
        finalize_call_billing(call_id)
    except Exception as exc:
        logger.error("[call_runner] billing failed for call %s: %s", call_id, exc)


# --- Dispatch methods ---

def start_call(number: str, flow_id: int, *, name: str = "", tenant_id: int | None = None, order_id: int | None = None, caller_id: str | None = None, sip_override: dict[str, str] | None = None, sip_gateway_id: int | None = None, rate_snapshot: dict | None = None) -> int:
    """Create the database records and dispatch IVR call to background thread pool.

    rate_snapshot: Pre-resolved BillingPolicy.as_dict() to persist alongside the
    call row before SIP dispatch.  Passed by order_service / quick-call routes after
    resolving the tenant billing policy.  Must be provided for all new billable calls.

    sip_override: REGISTER/INVITE credentials (SIP_DOMAIN/SIP_PORT/SIP_USER/
    SIP_PASSWORD) for the trunk that owns caller_id, e.g. from
    number_service.get_sip_trunk_for_caller_id(). The number pool (admin
    SIP Gateways page, sip_providers table) and sip_config()'s fallback to
    app_settings/tenant.settings are two separate stores — without this,
    a caller_id selected from the pool never actually carries its trunk's
    auth credentials into the call, and sip_ready() falls back to checking
    unrelated global/tenant settings that were never configured."""
    if sip_override:
        if not all(sip_override.get(k) for k in ("SIP_DOMAIN", "SIP_USER", "SIP_PASSWORD")):
            raise RuntimeError("SIP credentials not configured. Open Settings first.")
        sip = dict(sip_override)
    else:
        if not sip_ready(tenant_id):
            raise RuntimeError("SIP credentials not configured. Open Settings first.")
        sip = sip_config(tenant_id)
    normalized = normalize_phone_number(number)
    flow = load_flow(flow_id)
    if caller_id and not sip_override:
        # Only stamp caller_id over SIP_USER for the legacy single-trunk
        # fallback path. When sip_override is set, SIP_USER is already the
        # real trunk auth username (e.g. "09602111380") — caller_id is the
        # same number but E.164-formatted ("+8809602111380"), which Asterisk
        # rejects on REGISTER with 403 Forbidden if substituted in.
        sip["SIP_USER"] = caller_id
    dial_target = format_dial_number(normalized, db.get_setting("ASTERISK_DIAL_FORMAT", "local_bd"))

    import json as _json
    _snap_json: str | None = _json.dumps(rate_snapshot) if rate_snapshot is not None else None

    with _lock:
        contact_id = upsert_contact(normalized, name)
        call_id = db.insert_returning_id(
            """
            INSERT INTO calls (contact_id, flow_id, number, status, attempt_no, call_type, tenant_id, order_id, caller_id, sip_gateway_id, startup_state, rate_snapshot)
            VALUES (?, ?, ?, 'initiated', ?, 'ivr', ?, ?, ?, ?, 'pending', ?)
            """,
            (contact_id, flow_id, normalized, _attempt_no(contact_id), tenant_id, order_id, caller_id, sip_gateway_id, _snap_json),
        )

    # Register startup ack event before submitting to executor so the event is
    # always available when the thread runs (even if it runs immediately).
    register_startup_ack(call_id)

    # Fault point: fires after calls row committed + ACK event registered, but
    # before executor.submit() dispatches the SIP thread.  Simulates a worker
    # crash after the DB row exists but before any telephony starts.
    _maybe_fault("after_calls_row")

    _live.register_active_call(
        call_id, tenant_id=tenant_id, order_id=order_id, call_type="order_confirmation" if order_id else "ivr",
        phone=normalized, caller_id=caller_id, sip_gateway_id=sip_gateway_id, status="queued",
    )
    _live.set_cancel_handler(call_id, lambda cid=call_id: cancel_call(cid))

    # [SAAS STEP 9] SIP/provider dispatch started
    import logging
    logger = logging.getLogger("ccall")
    masked_sip_user = sip.get("SIP_USER", "")[:4] + "..." if sip.get("SIP_USER") else "None"
    logger.info(f"[SAAS STEP 9] SIP/provider dispatch started for call {call_id}. Dial target: {dial_target}, SIP Trunk Auth User: {masked_sip_user}")

    _executor.submit(_run_ivr_thread, call_id, dial_target, flow, sip, tenant_id, order_id)
    return call_id


def start_bulk(numbers: list[str], flow_id: int) -> list[int]:
    """Launch multiple IVR calls in parallel."""
    return [start_call(number, flow_id) for number in numbers]


def start_direct_call(number: str, audio_name: str, repeat_count: int, message_text: str = "", audio_mode: str = "tts", *, name: str = "", tenant_id: int | None = None, order_id: int | None = None, caller_id: str | None = None, sip_override: dict[str, str] | None = None, sip_gateway_id: int | None = None, rate_snapshot: dict | None = None) -> int:
    """Create the database records and dispatch Direct call to background thread pool.
    rate_snapshot: Pre-resolved BillingPolicy.as_dict() — see start_call() for details.
    See start_call() for what sip_override is and why it's needed."""
    if sip_override:
        if not all(sip_override.get(k) for k in ("SIP_DOMAIN", "SIP_USER", "SIP_PASSWORD")):
            raise RuntimeError("SIP credentials not configured. Open Settings first.")
        sip = dict(sip_override)
    else:
        if not sip_ready(tenant_id):
            raise RuntimeError("SIP credentials not configured. Open Settings first.")
        sip = sip_config(tenant_id)

    normalized = normalize_phone_number(number)
    audio_path = INSTANCE_DIR / "sounds" / f"{audio_name}.ulaw"
    if not audio_path.exists():
        raise RuntimeError("Call audio file is missing.")

    if caller_id and not sip_override:
        # See start_call() — don't clobber the real trunk auth username
        # (sip_override's SIP_USER) with the E.164-formatted caller_id.
        sip["SIP_USER"] = caller_id
    dial_target = format_dial_number(normalized, db.get_setting("ASTERISK_DIAL_FORMAT", "local_bd"))
    
    # Calculate duration
    audio_secs = audio_duration_seconds(audio_name)
    repeat_val = 2 if int(repeat_count) > 1 else 1
    max_call_secs = audio_secs * repeat_val + max(0, repeat_val - 1)

    import json as _json2
    _snap_json2: str | None = _json2.dumps(rate_snapshot) if rate_snapshot is not None else None

    with _lock:
        contact_id = upsert_contact(normalized, name)
        call_id = db.insert_returning_id(
            """
            INSERT INTO calls
                (contact_id, number, status, attempt_no, call_type, audio_mode, repeat_count, audio_name, message, tenant_id, order_id, caller_id, sip_gateway_id, startup_state, rate_snapshot)
            VALUES (?, ?, 'initiated', ?, 'direct', ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (contact_id, normalized, _attempt_no(contact_id), audio_mode, repeat_count, audio_name, message_text, tenant_id, order_id, caller_id, sip_gateway_id, _snap_json2),
        )

    # Register startup ACK event before submitting so the event exists before
    # the thread runs.  direct_sip_call.py prints "Calling <number>" to stdout
    # once SIP initialization completes; _run_direct_subprocess watches for that
    # line and signals accepted=True.  Popen alone is not sufficient.
    register_startup_ack(call_id)

    _live.register_active_call(
        call_id, tenant_id=tenant_id, order_id=order_id, call_type="direct",
        phone=normalized, caller_id=caller_id, sip_gateway_id=sip_gateway_id, status="queued",
    )

    _executor.submit(_run_direct_subprocess, call_id, dial_target, str(audio_path), repeat_val, max_call_secs, sip, tenant_id)
    return call_id


def start_direct_bulk(numbers: list[str], audio_name: str, repeat_count: int, message_text: str = "", audio_mode: str = "tts") -> list[int]:
    """Launch multiple direct calls in parallel."""
    return [start_direct_call(number, audio_name, repeat_count, message_text, audio_mode) for number in numbers]
