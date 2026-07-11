# call_engine/active_calls.py
"""Live-call registry backed by Redis.

Metadata (status, dtmf, timings) is stored in a Redis hash per call so all
Gunicorn workers can read and update it.  Cancel handlers (IvrCall.request_cancel
or Popen.terminate wrappers) are process-local callables that cannot be
serialised — they stay in a local dict.  Cancel requests are bridged via a
Redis pub/sub channel: any worker can publish a cancel signal; the worker that
owns the call subscribes to that channel in a background thread and fires its
local handler when it sees the message.

Key schema
  Hash:   ccall:active:{call_id}   — call metadata, TTL = CALL_MAX_DURATION_SECONDS
  PubSub: ccall:cancel:{call_id}   — cancel signal (message body = "cancel")

Falls back to the local in-process dict if Redis is unavailable so dev/test
environments without Redis still work.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from typing import Any, Callable

logger = logging.getLogger("ccall")

_CALL_TTL = int(os.environ.get("CALL_MAX_DURATION_SECONDS", 180)) + 30  # extra buffer

# Process-local cancel handlers — keyed by call_id.
_cancel_handlers: dict[int, Callable[[], None]] = {}
_cancel_subscriptions: dict[int, threading.Thread] = {}
_local_lock = threading.Lock()

# Fallback: in-process registry used when Redis is unreachable.
_local_registry: dict[int, dict[str, Any]] = {}


def _redis():
    """Return the Redis client, or None if unavailable."""
    try:
        from app.extensions import redis_ext
        return redis_ext.client
    except Exception:
        return None


def _hash_key(call_id: int) -> str:
    return f"ccall:active:{call_id}"


def _cancel_channel(call_id: int) -> str:
    return f"ccall:cancel:{call_id}"


def _serialize(value: Any) -> str:
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if value is None:
        return ""
    return str(value)


def _deserialize_entry(raw: dict[bytes, bytes]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in raw.items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        # Convert numeric fields back
        if key in ("call_id", "tenant_id", "order_id", "sip_gateway_id", "talk_seconds", "ring_seconds"):
            try:
                result[key] = int(val) if val else None
            except ValueError:
                result[key] = None
        elif key in ("cancel_supported",):
            result[key] = val == "True"
        elif val == "":
            result[key] = None
        else:
            result[key] = val
    return result


def register_active_call(
    call_id: int,
    *,
    tenant_id: int | None,
    order_id: int | None,
    call_type: str,
    phone: str,
    caller_id: str | None,
    sip_gateway_id: int | None = None,
    status: str = "queued",
    cancel_supported: bool = True,
) -> None:
    entry = {
        "call_id": call_id,
        "tenant_id": tenant_id,
        "order_id": order_id,
        "call_type": call_type,
        "phone": phone,
        "caller_id": caller_id,
        "sip_gateway_id": sip_gateway_id,
        "status": status,
        "started_at": _serialize(datetime.datetime.now(datetime.timezone.utc)),
        "answered_at": "",
        "talk_seconds": 0,
        "ring_seconds": 0,
        "dtmf_digit": "",
        "last_event": status,
        "cancel_supported": cancel_supported,
    }

    r = _redis()
    if r is not None:
        try:
            r.hset(_hash_key(call_id), mapping={k: _serialize(v) for k, v in entry.items()})
            r.expire(_hash_key(call_id), _CALL_TTL)
            return
        except Exception as exc:
            logger.warning("active_calls Redis write failed for call %s: %s — using local fallback", call_id, exc)

    with _local_lock:
        _local_registry[call_id] = {**entry, "started_at": datetime.datetime.now(datetime.timezone.utc)}


def update_active_call(call_id: int, **fields: Any) -> None:
    if "status" in fields:
        fields["last_event"] = fields["status"]

    r = _redis()
    if r is not None:
        try:
            key = _hash_key(call_id)
            if r.exists(key):
                r.hset(key, mapping={k: _serialize(v) for k, v in fields.items()})
                r.expire(key, _CALL_TTL)
                return
        except Exception as exc:
            logger.warning("active_calls Redis update failed for call %s: %s", call_id, exc)

    with _local_lock:
        entry = _local_registry.get(call_id)
        if entry is not None:
            entry.update(fields)


def set_cancel_handler(call_id: int, handler: Callable[[], None]) -> None:
    """Register a process-local cancel handler and subscribe to the Redis
    cancel channel for this call.  When any worker publishes a cancel signal,
    the subscriber thread fires the local handler."""
    with _local_lock:
        _cancel_handlers[call_id] = handler

    r = _redis()
    if r is None:
        return

    def _listen() -> None:
        try:
            pub = r.pubsub()
            pub.subscribe(_cancel_channel(call_id))
            for message in pub.listen():
                if message["type"] == "message":
                    with _local_lock:
                        fn = _cancel_handlers.get(call_id)
                    if fn:
                        try:
                            fn()
                        except Exception as exc:
                            logger.error("Cancel handler for call %s raised: %s", call_id, exc)
                    pub.unsubscribe()
                    break
        except Exception as exc:
            logger.warning("Cancel subscription thread for call %s died: %s", call_id, exc)

    t = threading.Thread(target=_listen, daemon=True, name=f"cancel-sub-{call_id}")
    t.start()
    with _local_lock:
        _cancel_subscriptions[call_id] = t


def remove_active_call(call_id: int) -> None:
    r = _redis()
    if r is not None:
        try:
            r.delete(_hash_key(call_id))
        except Exception as exc:
            logger.warning("active_calls Redis delete failed for call %s: %s", call_id, exc)

    with _local_lock:
        _local_registry.pop(call_id, None)
        _cancel_handlers.pop(call_id, None)
        _cancel_subscriptions.pop(call_id, None)


def get_active_call(call_id: int) -> dict[str, Any] | None:
    r = _redis()
    if r is not None:
        try:
            raw = r.hgetall(_hash_key(call_id))
            if raw:
                return _deserialize_entry(raw)
        except Exception as exc:
            logger.warning("active_calls Redis read failed for call %s: %s", call_id, exc)

    with _local_lock:
        entry = _local_registry.get(call_id)
        return dict(entry) if entry else None


def list_active_calls(tenant_id: int | None = None) -> list[dict[str, Any]]:
    """tenant_id=None → all tenants (super admin). Pass tenant_id to filter."""
    r = _redis()
    if r is not None:
        try:
            keys = r.keys("ccall:active:*")
            entries = []
            pipeline = r.pipeline()
            for k in keys:
                pipeline.hgetall(k)
            for raw in pipeline.execute():
                if raw:
                    entries.append(_deserialize_entry(raw))
            if tenant_id is not None:
                entries = [e for e in entries if e.get("tenant_id") == tenant_id]
            return entries
        except Exception as exc:
            logger.warning("active_calls Redis list failed: %s — using local fallback", exc)

    with _local_lock:
        entries = list(_local_registry.values())
    if tenant_id is not None:
        entries = [e for e in entries if e.get("tenant_id") == tenant_id]
    return [dict(e) for e in entries]


def request_cancel(call_id: int, tenant_id: int | None = None) -> bool:
    """Publish a cancel signal via Redis pub/sub.  The worker that owns the
    call will receive the message and fire its local handler.

    Returns False immediately if the call isn't found in the registry (it may
    already be done), or if it belongs to a different tenant, or if it doesn't
    support cancellation.  Returns True if the signal was published (not a
    guarantee the call was actually terminated — the handler runs asynchronously).
    """
    entry = get_active_call(call_id)
    if entry is None:
        return False
    if tenant_id is not None and entry.get("tenant_id") != tenant_id:
        return False
    if not entry.get("cancel_supported"):
        return False

    r = _redis()
    if r is not None:
        try:
            r.publish(_cancel_channel(call_id), "cancel")
            return True
        except Exception as exc:
            logger.warning("active_calls Redis publish failed for call %s: %s — trying local handler", call_id, exc)

    # Fallback: fire local handler directly (works in single-worker / no-Redis mode).
    with _local_lock:
        handler = _cancel_handlers.get(call_id)
    if handler:
        try:
            handler()
        except Exception as exc:
            logger.error("Local cancel handler for call %s raised: %s", call_id, exc)
        return True

    return False
