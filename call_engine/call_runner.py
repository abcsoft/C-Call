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
        if t["node_id"] in nodes and t["next_node_id"]:
            nodes[t["node_id"]].transitions[t["digit"]] = t["next_node_id"]

    root = flow_row["root_node_id"] or (node_rows[0]["id"] if node_rows else None)
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
            elif ev.type == "dtmf" and ev.digit:
                # [SAAS STEP 11] DTMF callback received
                import logging
                logging.getLogger("ccall").info(f"[SAAS STEP 11] DTMF callback received: call_id={call_id}, digit={ev.digit}")
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
    call = IvrCall(target_dial, flow, sip, on_event, record=record, tenant_id=tenant_id)

    # Register call globally to enable UI cancellations
    _active_ivr_calls[call_id] = call
    _live.set_cancel_handler(call_id, lambda cid=call_id: cancel_call(cid))
    ACTIVE_CALLS.inc()

    try:
        result = call.run()
        # [SAAS STEP 10] Provider response
        import logging
        logging.getLogger("ccall").info(f"[SAAS STEP 10] Provider response: call_id={call_id}, status={result.status}, SIP Final Status={result.sip_final_status}, Error={result.error}")
    finally:
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
    # direct_sip_call.py is a read-only legacy subprocess (no DB access,
    # no IPC back to us) — but it does print "Call answered; sending
    # audio" to stdout the instant the far end picks up (see its own
    # line ~421), and stdout is redirected into log_file_path. Record the
    # byte offset right before launch, then tail only the bytes this call
    # appended, watching for that line — that's the real answer signal.
    # Without it we previously stamped "answered" the instant the
    # subprocess launched, which lied about ring time and talk_seconds.
    log_offset = log_file_path.stat().st_size if log_file_path.exists() else 0
    answered_at_monotonic: float | None = None
    hangup_finalized = False

    def _read_new_log() -> bytes:
        try:
            with log_file_path.open("rb") as f:
                f.seek(log_offset)
                return f.read()
        except OSError:
            return b""

    def _check_answered(chunk: bytes) -> bool:
        nonlocal answered_at_monotonic
        if answered_at_monotonic is not None:
            return True
        if b"Call answered; sending audio" in chunk:
            answered_at_monotonic = time.monotonic()
            _live.update_active_call(
                call_id, status="answered",
                answered_at=datetime.datetime.now(datetime.timezone.utc),
            )
            return True
        return False

    ACTIVE_CALLS.inc()
    proc: subprocess.Popen | None = None
    try:
        with log_file_path.open("ab", buffering=0) as log_f:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(BASE_DIR / "direct_sip_call.py"),
                    dial_target,
                    str(audio_path),
                    str(repeat_count),
                    str(max_seconds)
                ],
                env=env,
                stdout=log_f,
                stderr=log_f,
            )
            _active_direct_procs[call_id] = (proc, cancel_event)
            _live.set_cancel_handler(call_id, lambda cid=call_id: cancel_call(cid))

            # Poll instead of one blocking wait() — lets us tail the log
            # for the answer signal and tick talk_seconds only after it.
            deadline = start_time + max_seconds + int(os.environ.get("SIP_PROCESS_TIMEOUT_SECONDS", "90")) + 30
            returncode = None
            while True:
                try:
                    returncode = proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    chunk = _read_new_log()
                    if _check_answered(chunk):
                        _live.update_active_call(call_id, talk_seconds=int(now - answered_at_monotonic))

                    # The remote hanging up is detected by direct_sip_call.py
                    # right away (poll_sip_bye()), but the subprocess itself
                    # then spends up to ~4.5s (3 retries x 1.5s) confirming
                    # its own BYE before actually exiting — read-only file,
                    # can't shorten that. So end the call live the instant
                    # the log says the remote hung up, instead of waiting
                    # for proc.wait() to finally return.
                    if not hangup_finalized and (
                        b"Remote hung up" in chunk or b"Call completed (remote ended)" in chunk
                    ):
                        hangup_finalized = True
                        talk_seconds = int(now - answered_at_monotonic) if answered_at_monotonic else 0
                        elapsed = int(now - start_time)
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
                        _remove_call_delayed(call_id, "completed")
                        CALLS_TOTAL.labels(result="completed", tenant_id=str(tenant_id or "")).inc()
                        CALL_DURATION.observe(talk_seconds)
                        try:
                            from app.services.billing_service import finalize_call_billing
                            finalize_call_billing(call_id)
                        except Exception as exc:
                            logger.error("[call_runner] billing failed for call %s: %s", call_id, exc)

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

def start_call(number: str, flow_id: int, *, name: str = "", tenant_id: int | None = None, order_id: int | None = None, caller_id: str | None = None, sip_override: dict[str, str] | None = None, sip_gateway_id: int | None = None) -> int:
    """Create the database records and dispatch IVR call to background thread pool.

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

    with _lock:
        contact_id = upsert_contact(normalized, name)
        call_id = db.insert_returning_id(
            """
            INSERT INTO calls (contact_id, flow_id, number, status, attempt_no, call_type, tenant_id, order_id, caller_id, sip_gateway_id)
            VALUES (?, ?, ?, 'initiated', ?, 'ivr', ?, ?, ?, ?)
            """,
            (contact_id, flow_id, normalized, _attempt_no(contact_id), tenant_id, order_id, caller_id, sip_gateway_id),
        )

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


def start_direct_call(number: str, audio_name: str, repeat_count: int, message_text: str = "", audio_mode: str = "tts", *, name: str = "", tenant_id: int | None = None, order_id: int | None = None, caller_id: str | None = None, sip_override: dict[str, str] | None = None, sip_gateway_id: int | None = None) -> int:
    """Create the database records and dispatch Direct call to background thread pool.
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

    with _lock:
        contact_id = upsert_contact(normalized, name)
        call_id = db.insert_returning_id(
            """
            INSERT INTO calls
                (contact_id, number, status, attempt_no, call_type, audio_mode, repeat_count, audio_name, message, tenant_id, order_id, caller_id, sip_gateway_id)
            VALUES (?, ?, 'initiated', ?, 'direct', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (contact_id, normalized, _attempt_no(contact_id), audio_mode, repeat_count, audio_name, message_text, tenant_id, order_id, caller_id, sip_gateway_id),
        )

    _live.register_active_call(
        call_id, tenant_id=tenant_id, order_id=order_id, call_type="direct",
        phone=normalized, caller_id=caller_id, sip_gateway_id=sip_gateway_id, status="queued",
    )

    _executor.submit(_run_direct_subprocess, call_id, dial_target, str(audio_path), repeat_val, max_call_secs, sip, tenant_id)
    return call_id


def start_direct_bulk(numbers: list[str], audio_name: str, repeat_count: int, message_text: str = "", audio_mode: str = "tts") -> list[int]:
    """Launch multiple direct calls in parallel."""
    return [start_direct_call(number, audio_name, repeat_count, message_text, audio_mode) for number in numbers]
