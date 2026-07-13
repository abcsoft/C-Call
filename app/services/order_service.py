from __future__ import annotations

import logging
import datetime
import threading
import time
import json
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any
from flask import current_app

# Bounded executor for async call dispatch — max 10 in-flight dispatches.
# Unlike bare daemon threads, this pool drains cleanly on interpreter shutdown.
_dispatch_pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="ccall-dispatch")

from app.db import db
from app.services.webhook_service import mask_phone
from app.models.order import (
    create_order as db_create_order,
    get_order_by_id,
    get_order_by_id_system,
    get_order_by_external_id,
    update_order_status_system,
    set_order_caller_id,
    set_order_confirmed,
    create_call_attempt,
    update_call_attempt_by_call_id_system,
    increment_order_attempts,
    get_call_attempts_for_order,
    get_orders,
    get_orders_count
)
from app.models.tenant import get_tenant_by_id
from app.services.rate_service import estimate_charge
from app.services.wallet_service import check_balance
from app.exceptions import InsufficientBalanceError
from app.services.number_service import select_caller_id
from app.services.tts_service import generate_order_audio
from app.services.webhook_service import deliver_async
from call_engine import call_runner

logger = logging.getLogger("ccall")

# Background thread to monitor calls and process outcomes
_monitor_thread = None
_monitor_active = False

_MONITOR_LOCK_KEY = "ccall:monitor_leader"
_MONITOR_LOCK_TTL = 10  # seconds — must be > poll interval (1.5s) with headroom


def start_call_monitor(app) -> None:
    """Start the background call monitor thread.

    In a multi-worker deployment (Gunicorn 4 workers) each worker calls this
    function and would otherwise run 4 polling loops that duplicate work and
    emit duplicate billing events.  We use a Redis SETNX lock with a short TTL
    so only one worker (the lock holder) actually runs the loop.  Workers that
    fail to acquire the lock skip startup entirely; they will pick it up on the
    next restart if the holder dies without releasing."""
    global _monitor_thread, _monitor_active
    if _monitor_active:
        return
    _monitor_active = True
    _monitor_thread = threading.Thread(target=_monitor_loop, args=(app,), daemon=True)
    _monitor_thread.start()
    logger.info("Background Call Monitor Thread started.")

def stop_call_monitor() -> None:
    global _monitor_active
    _monitor_active = False

def _monitor_loop(app) -> None:
    """Polls database for completed calls to trigger order updates and billing.

    Uses Redis SETNX as a distributed leader lock so only one of the N Gunicorn
    workers runs this loop.  The lock is refreshed every poll cycle (TTL 10s,
    cycle 1.5s) — if this worker dies the lock expires and another worker's
    thread can acquire it on its next renewal attempt.
    """
    import os
    worker_id = f"{os.getpid()}:{threading.get_ident()}"

    with app.app_context():
        from app.extensions import redis_ext

        while _monitor_active:
            try:
                # Acquire/renew the leader lock.  set(nx=True) succeeds only if
                # key doesn't exist; set(xx=True) renews only if we already own it.
                redis_client = redis_ext.client
                is_leader = redis_client.set(
                    _MONITOR_LOCK_KEY, worker_id,
                    ex=_MONITOR_LOCK_TTL, nx=True,
                ) or redis_client.get(_MONITOR_LOCK_KEY) == worker_id.encode()

                if not is_leader:
                    time.sleep(1.5)
                    continue

                # Renew TTL since we confirmed we hold the lock.
                redis_client.expire(_MONITOR_LOCK_KEY, _MONITOR_LOCK_TTL)

            except Exception as redis_exc:
                logger.warning("Monitor loop Redis lock error (continuing as leader): %s", redis_exc)

            try:
                # Find all order call attempts where the result is still pending/NULL
                pending_attempts = db.query(
                    "SELECT id, order_id, tenant_id, call_id, attempt_no FROM order_call_attempts WHERE result IS NULL"
                )
                
                for attempt in pending_attempts:
                    call_id = attempt["call_id"]
                    if not call_id:
                        continue
                        
                    # Query the calls table for the status of this call
                    call_row = db.query_one("SELECT status, talk_seconds FROM calls WHERE id = %s", (call_id,))
                    if not call_row:
                        continue
                        
                    status = call_row["status"]
                    # If call has reached a terminal status
                    if status in ("completed", "failed", "no_answer", "busy", "canceled"):
                        # Extract DTMF digit pressed if any
                        dtmf_row = db.query_one(
                            "SELECT digit FROM call_events WHERE call_id = %s AND event_type = 'dtmf' ORDER BY seq DESC LIMIT 1",
                            (call_id,)
                        )
                        dtmf_digit = dtmf_row["digit"] if dtmf_row else None
                        talk_seconds = int(call_row["talk_seconds"] or 0)
                        
                        logger.info(f"Detected completed call {call_id} for order {attempt['order_id']} (status: {status}, dtmf: {dtmf_digit})")
                        
                        # Process outcome
                        update_status_from_call(
                            order_id=attempt["order_id"],
                            tenant_id=attempt["tenant_id"],
                            call_result=status,
                            dtmf_digit=dtmf_digit,
                            talk_seconds=talk_seconds,
                            call_id=call_id
                        )
            except Exception as exc:
                logger.error(f"Error in background call monitor loop: {exc}")
                
            time.sleep(1.5)  # Poll every 1.5 seconds

def parse_template(template_str: str, order: dict, tenant: dict) -> str:
    """Parse dynamic script variables in voice templates.
    Delegates to render_order_voice_script() for full e-commerce variable support.
    Kept for backward compatibility.
    """
    from app.services.tts_service import render_order_voice_script
    return render_order_voice_script(template_str, order, tenant)

import os
import ipaddress
import socket
from urllib.parse import urlparse

_FLASK_ENV = os.environ.get("FLASK_ENV", "production")
# Explicit opt-in for private/loopback webhook URLs. Same flag as webhook_service.
# Production startup rejects this flag being true (see config.py).
_ALLOW_PRIVATE_WEBHOOKS = (
    os.environ.get("ALLOW_PRIVATE_WEBHOOKS_FOR_DEV", "false").lower() == "true"
    and _FLASK_ENV in ("development", "testing")
)

def _is_private_host(hostname: str) -> bool:
    try:
        ip_str = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except Exception:
        try:
            ip = ipaddress.ip_address(hostname)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except Exception:
            return False

def _validate_webhook_url(url: str) -> bool:
    if not url:
        return True
    parsed = urlparse(url)
    if _ALLOW_PRIVATE_WEBHOOKS:
        # Dev-only: allow http/https with any host (including localhost/private IPs)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    hostname = parsed.hostname or ""
    return not _is_private_host(hostname)

def create_order(tenant_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """Validate details, check wallet balance, insert order in DB, and queue call asynchronously.
    Supports idempotency via external_order_id.
    Accepts rich e-commerce metadata: product_name, price, delivery_charge, grand_total, etc.
    """
    logger.info(f"[SAAS STEP 1] /api/v1/orders request received. Tenant ID: {tenant_id}, Data: {data}")

    external_order_id = str(data.get("order_id") or "").strip()
    phone = str(data.get("phone") or "").strip()
    customer_name = re.sub(r"<[^>]*>", "", str(data.get("customer_name") or "")).strip() or None
    amount_str = data.get("amount", "0")

    # 1. Validation
    if not external_order_id:
        logger.warning(f"[SAAS STEP 3] Payload validation failed: order_id is missing. Data: {data}")
        raise ValueError("order_id is required.")
    if not phone:
        logger.warning("[SAAS STEP 3] Payload validation failed: phone number is missing.")
        raise ValueError("phone number is required.")

    # Validate phone (BD E.164 format: Optional +8801 or 8801 or 01 followed by 9 digits)
    cleaned_phone = re.sub(r"[^\d+]", "", str(phone))
    phone_match = re.match(r"^(?:\+88|88)?(01[3-9]\d{8})$", cleaned_phone)
    if not phone_match:
        logger.warning(f"[SAAS STEP 3] Payload validation failed: Invalid phone format. Phone: {mask_phone(str(phone))}")
        raise ValueError("Invalid Bangladesh mobile number format.")
    normalized_phone = f"+88{phone_match.group(1)}"

    try:
        amount = Decimal(str(amount_str))
    except Exception:
        logger.warning(f"[SAAS STEP 3] Payload validation failed: Invalid amount. Amount: {amount_str}")
        raise ValueError("Amount must be a valid numeric value.")
    if amount < 0:
        logger.warning(f"[SAAS STEP 3] Payload validation failed: Negative amount. Amount: {amount}")
        raise ValueError("Amount cannot be negative.")

    logger.info(f"[SAAS STEP 3] Payload validation passed. Phone: {mask_phone(normalized_phone)}, External ID: {external_order_id}")

    # 2. Check tenant active status
    tenant = get_tenant_by_id(tenant_id)
    if not tenant or tenant.get("status") not in ("active", "trial"):
        raise ValueError("Tenant is inactive or suspended.")

    # 3. Idempotency Check
    existing_order = get_order_by_external_id(tenant_id, external_order_id)
    if existing_order:
        logger.info(f"[SAAS STEP 4] Existing order found. ID: {existing_order['id']}, External ID: {existing_order['external_order_id']}")
        logger.info(f"Order {external_order_id} already exists for tenant {tenant_id}. Returning existing record.")
        return existing_order

    # 4. Wallet balance check (min_charge)
    min_charge = estimate_charge(tenant_id)
    if not check_balance(tenant_id, min_charge):
        from app.services.wallet_service import get_balance
        current_bal = get_balance(tenant_id)
        from app.exceptions import InsufficientBalanceError
        raise InsufficientBalanceError(current_bal, min_charge)

    # 5. Snapshot webhook URL from settings
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
    webhook_url = str(data.get("webhook_url") or settings.get("webhook_url") or "").strip() or None
    if webhook_url:
        if not _validate_webhook_url(webhook_url):
            raise ValueError("webhook_url must be a valid HTTPS URL.")

    # 6. Extract and sanitize rich e-commerce metadata
    def _clean_str(val, max_len=500):
        """Sanitize a text value: strip HTML tags, trim whitespace, limit length."""
        if val is None:
            return None
        s = re.sub(r"<[^>]*>", "", str(val)).strip()
        return s[:max_len] if s else None

    def _clean_decimal(val):
        """Parse a numeric value to string, return None if invalid."""
        if val is None:
            return None
        try:
            return str(Decimal(str(val).strip()))
        except Exception:
            return None

    metadata = {}

    # Store name / site name
    store_name = _clean_str(data.get("site_name") or data.get("store_name"))
    if store_name:
        metadata["store_name"] = store_name

    # Order number
    order_number = _clean_str(data.get("order_number"))
    if order_number:
        metadata["order_number"] = order_number

    # Qty
    try:
        qty_val = data.get("qty") or data.get("quantity")
        if qty_val is not None:
            metadata["qty"] = int(qty_val)
    except Exception:
        pass

    # Item price
    item_price_val = _clean_decimal(data.get("item_price"))
    if item_price_val:
        metadata["item_price"] = item_price_val

    # Discount
    discount_val = _clean_decimal(data.get("discount"))
    if discount_val:
        metadata["discount"] = discount_val

    # Tax
    tax_val = _clean_decimal(data.get("tax"))
    if tax_val:
        metadata["tax"] = tax_val

    # Site URL
    site_url = _clean_str(data.get("site_url"))
    if site_url:
        metadata["site_url"] = site_url

    # Product information
    product_name = _clean_str(data.get("product_name"))
    if product_name:
        metadata["product_name"] = product_name

    items_val = data.get("items")
    if items_val:
        if isinstance(items_val, str):
            metadata["items"] = _clean_str(items_val)
        else:
            metadata["items"] = str(items_val)[:500]

    products_val = data.get("products")
    if products_val and isinstance(products_val, list):
        # Sanitize each product entry
        clean_products = []
        for p in products_val[:50]:  # Limit to 50 products
            if isinstance(p, dict):
                clean_products.append({
                    "name": _clean_str(p.get("name"), 200) or "",
                    "qty": int(p.get("qty") or p.get("quantity") or 1),
                    "total": _clean_decimal(p.get("total") or p.get("price")) or "0"
                })
            elif isinstance(p, str):
                clean_products.append({"name": _clean_str(p, 200) or "", "qty": 1, "total": "0"})
        if clean_products:
            metadata["products"] = clean_products

    # Pricing breakdown
    price_val = _clean_decimal(data.get("product_price") or data.get("price"))
    if price_val:
        metadata["price"] = price_val

    subtotal_val = _clean_decimal(data.get("subtotal"))
    if subtotal_val:
        metadata["subtotal"] = subtotal_val

    delivery_charge = _clean_decimal(data.get("delivery_charge") or data.get("shipping_total"))
    if delivery_charge:
        metadata["delivery_charge"] = delivery_charge

    grand_total = _clean_decimal(data.get("grand_total"))
    if grand_total:
        metadata["grand_total"] = grand_total

    # Payment method
    payment_method = _clean_str(data.get("payment_method"), 100)
    if payment_method:
        metadata["payment_method"] = payment_method

    # Callback number
    callback_number = _clean_str(data.get("callback_number"), 20)
    if callback_number:
        metadata["callback_number"] = callback_number

    # Voice override texts for confirm/cancel messages
    confirm_text = _clean_str(data.get("confirm_text"), 500)
    if confirm_text:
        metadata["confirm_text"] = confirm_text

    cancel_text = _clean_str(data.get("cancel_text"), 500)
    if cancel_text:
        metadata["cancel_text"] = cancel_text

    # Agent extension for transfer
    agent_extension = _clean_str(data.get("agent_extension"), 20)
    if agent_extension:
        metadata["agent_extension"] = agent_extension

    # Key actions (custom DTMF mappings)
    key_actions = {}
    for digit in range(1, 10):
        action_key = f"key_{digit}_action"
        value_key = f"key_{digit}_value"
        action = _clean_str(data.get(action_key), 50)
        value = _clean_str(data.get(value_key), 200)
        if action:
            key_actions[str(digit)] = {"action": action, "value": value or ""}
    if key_actions:
        metadata["key_actions"] = key_actions

    # Custom audio URLs per key (key_3_audio_url through key_9_audio_url)
    for digit in range(3, 10):
        audio_key = f"key_{digit}_audio_url"
        audio_url = _clean_str(data.get(audio_key), 500)
        if audio_url:
            metadata.setdefault("key_audio_urls", {})[str(digit)] = audio_url

    # Per-order voice overrides (stored as columns, not in metadata)
    custom_text = _clean_str(data.get("custom_text"), 1000)
    custom_audio_url = _clean_str(data.get("custom_audio_url"), 500)

    # Validate custom_audio_url if present
    if custom_audio_url:
        from urllib.parse import urlparse
        parsed_audio = urlparse(custom_audio_url)
        if parsed_audio.scheme != "https" or not parsed_audio.netloc:
            custom_audio_url = None  # Silently discard invalid URLs

    # Validate confirm/cancel audio URLs in metadata
    for url_key in ("confirm_audio_url", "cancel_audio_url"):
        url_val = _clean_str(data.get(url_key), 500)
        if url_val:
            from urllib.parse import urlparse as _urlparse
            p = _urlparse(url_val)
            if p.scheme == "https" and p.netloc:
                metadata[url_key] = url_val

    # 7. Save order to DB
    order_id = db_create_order(
        tenant_id=tenant_id,
        external_order_id=external_order_id,
        phone=normalized_phone,
        customer_name=customer_name,
        amount=amount,
        webhook_url=webhook_url,
        metadata=metadata if metadata else None,
        custom_text=custom_text,
        custom_audio_url=custom_audio_url
    )
    logger.info(f"[SAAS STEP 4] New order saved to DB. ID: {order_id}, External ID: {external_order_id}")
    
    # Mark CALL_QUEUED — attempt counter incremented only after successful dispatch
    # so a dispatch failure doesn't consume an attempt or leave attempt_count ahead of reality.
    update_order_status_system(order_id, "CALL_QUEUED")

    # 8. Dispatch call asynchronously
    app = current_app._get_current_object()
    def run_async():
        with app.app_context():
            try:
                queue_confirmation_call(order_id, tenant_id)
                increment_order_attempts(tenant_id, order_id)
            except Exception as exc:
                logger.error("Async queue_confirmation_call failed for order %s: %s", order_id, exc)
                # Revert to NEW so the order isn't stuck in CALL_QUEUED with no call dispatched.
                try:
                    update_order_status_system(order_id, "NEW")
                except Exception as revert_exc:
                    logger.error("Failed to revert order %s to NEW after dispatch failure: %s", order_id, revert_exc)

    _dispatch_pool.submit(run_async)

    return get_order_by_id_system(order_id)

def create_order_and_queue(tenant_id: int, data: dict) -> dict:
    """Backward-compatible wrapper for create_order."""
    return create_order(tenant_id, data)

def get_order_by_external_id_service(tenant_id: int, external_order_id: str) -> dict | None:
    return get_order_by_external_id(tenant_id, external_order_id)

def queue_confirmation_call(order_id: int, tenant_id: int) -> int:
    """Select phone number CLI, generate script audio, and launch the SIP IVR call."""
    order = get_order_by_id_system(order_id)
    tenant = get_tenant_by_id(tenant_id)
    if not order or not tenant:
        raise ValueError("Invalid order_id or tenant_id")

    # Select caller ID CLI
    caller_id = select_caller_id(tenant_id)

    # Retrieve SIP provider status
    from app.services.number_service import get_sip_trunk_for_caller_id
    sip_override = get_sip_trunk_for_caller_id(caller_id) if caller_id else None
    from app.services.wallet_service import get_balance
    balance = get_balance(tenant_id)

    # [SAAS STEP 5] Tenant active/wallet/caller ID/SIP config check
    logger.info(
        f"[SAAS STEP 5] Tenant check: ID={tenant_id}, "
        f"Status={tenant.get('status')}, Balance={balance}, "
        f"Caller ID={caller_id}, SIP Config={'Custom' if sip_override else 'Default'}"
    )

    if not caller_id:
        from psycopg.types.json import Jsonb
        metadata = order.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        metadata["failure_reason"] = "No active caller ID assigned to this account. Go to Settings → contact admin."
        db.execute(
            "UPDATE orders SET order_status = 'FAILED', metadata = %s, updated_at = now() WHERE id = %s",
            (Jsonb(metadata), order_id)
        )
        logger.warning(f"Failed to queue call for order {order_id} (tenant {tenant_id}): no active phone CLI available.")
        return 0

    set_order_caller_id(tenant_id, order_id, caller_id)

    # Generate or retrieve TTS audio (ulaw file)
    audio_filename = generate_order_audio(tenant_id, order)

    # Resolve flow (check if custom flow is selected in tenant settings)
    flow_id = None
    tenant = get_tenant_by_id(tenant_id)
    if tenant:
        settings = tenant.get("settings") or {}
        if isinstance(settings, str):
            import json
            try:
                settings = json.loads(settings)
            except Exception:
                settings = {}
        order_call_flow_id = settings.get("order_call_flow_id")
        if order_call_flow_id:
            try:
                flow_row = db.query_one("SELECT id FROM flows WHERE id = %s AND tenant_id = %s", (int(order_call_flow_id), tenant_id))
                if flow_row:
                    flow_id = flow_row["id"]
            except Exception:
                pass

    if not flow_id:
        flow_id = _get_or_create_confirmation_flow(tenant_id, audio_filename)

    # Launch call
    try:
        from app.services.number_service import get_sip_trunk_for_caller_id, get_sip_provider_id_for_caller_id
        sip_override = get_sip_trunk_for_caller_id(caller_id)
        sip_gateway_id = get_sip_provider_id_for_caller_id(caller_id)
        call_id = call_runner.start_call(
            order["phone"],
            flow_id,
            name=order.get("customer_name") or "",
            tenant_id=tenant_id,
            order_id=order_id,
            caller_id=caller_id,
            sip_gateway_id=sip_gateway_id,
            sip_override=sip_override
        )
        
        # Create attempt
        create_call_attempt(
            order_id=order_id,
            tenant_id=tenant_id,
            attempt_no=order["attempt_count"],
            call_id=call_id
        )
        
        # [SAAS STEP 8] Call queued
        logger.info(f"[SAAS STEP 8] Call queued in DB attempts table. Call ID: {call_id}, Order ID: {order_id}, Attempt: {order['attempt_count']}")
        
        update_order_status_system(order_id, "CALLING")
        logger.info(f"Dispatched call {call_id} for order {order_id} (Attempt {order['attempt_count']})")
        return call_id
    except Exception as exc:
        update_order_status_system(order_id, "FAILED")
        logger.error(f"Error starting call for order {order_id}: {exc}")
        return 0

CONFIRMED_MSG_TEXT = "ধন্যবাদ, আপনার অর্ডারটি নিশ্চিত করা হয়েছে।"
CANCELLED_MSG_TEXT = "ধন্যবাদ, আপনার অর্ডারটি বাতিল করা হয়েছে।"


def _refresh_message_audio(tenant_id: int, flow_id: int) -> None:
    """Regenerate TTS for ALL message-type nodes in this flow using the current
    tenant TTS provider/voice. Called every time queue_confirmation_call runs so
    that a provider change (edge→elevenlabs etc.) takes effect on the next call."""
    nodes = db.query(
        "SELECT id, prompt_text FROM flow_nodes WHERE flow_id = %s AND node_type = 'message' "
        "AND prompt_text IS NOT NULL AND prompt_text != ''",
        (flow_id,)
    )
    for node in nodes:
        try:
            audio = generate_order_audio(tenant_id, node["prompt_text"])
            db.execute("UPDATE flow_nodes SET prompt_audio = %s WHERE id = %s", (audio, node["id"]))
        except Exception as exc:
            logger.error(f"Failed to refresh TTS audio for message node {node['id']}: {exc}")


def _backfill_missing_message_audio(tenant_id: int, flow_id: int) -> None:
    """Self-heal flows created before this fix: 'message' type nodes
    (Confirmed Msg / Cancelled Msg) used to be inserted with prompt_audio=''
    and no TTS was ever generated for them. ivr_engine.py::_play_prompt()
    silently plays nothing when prompt_audio is empty (no fallback to
    on-the-fly TTS from prompt_text) — so the customer heard silence after
    pressing 1/2 even though the order status updated correctly in the DB.
    Runs a cheap no-op SELECT once the audio has been generated, so this
    only does real work exactly once per tenant."""
    broken_nodes = db.query(
        "SELECT id, prompt_text FROM flow_nodes WHERE flow_id = %s AND node_type = 'message' "
        "AND (prompt_audio IS NULL OR prompt_audio = '') AND prompt_text IS NOT NULL AND prompt_text != ''",
        (flow_id,)
    )
    for node in broken_nodes:
        try:
            audio = generate_order_audio(tenant_id, node["prompt_text"])
            db.execute("UPDATE flow_nodes SET prompt_audio = %s WHERE id = %s", (audio, node["id"]))
            logger.info(
                f"Backfilled missing TTS audio for message node {node['id']} "
                f"(flow {flow_id}, tenant {tenant_id})"
            )
        except Exception as exc:
            logger.error(f"Failed to backfill TTS audio for message node {node['id']}: {exc}")


def _get_or_create_confirmation_flow(tenant_id: int, audio_filename: str) -> int:
    """Retrieve the confirmation flow for a tenant, or create it if not exists.
    Updates the root node's prompt audio to the latest generated script.
    """
    # Check if flow already exists
    flow_row = db.query_one(
        "SELECT id FROM flows WHERE tenant_id = %s AND name = 'Order Confirmation Flow'",
        (tenant_id,)
    )
    
    if flow_row:
        flow_id = flow_row["id"]
        # Find root node ID
        root_row = db.query_one("SELECT root_node_id FROM flows WHERE id = %s", (flow_id,))
        root_node_id = root_row["root_node_id"] if root_row else None
        
        if root_node_id:
            # Update root node audio
            db.execute(
                "UPDATE flow_nodes SET prompt_audio = %s WHERE id = %s",
                (audio_filename, root_node_id)
            )
            # Regenerate ALL message nodes (Confirmed/Cancelled) with current
            # tenant TTS provider — not just empty ones. Provider change would
            # otherwise leave these nodes playing audio from the old provider.
            _refresh_message_audio(tenant_id, flow_id)
            return flow_id

    # Pre-generate real TTS audio for the two static outcome messages —
    # previously these were inserted with prompt_audio='', which
    # ivr_engine.py::_play_prompt() silently treats as "play nothing".
    confirmed_audio = generate_order_audio(tenant_id, CONFIRMED_MSG_TEXT)
    cancelled_audio = generate_order_audio(tenant_id, CANCELLED_MSG_TEXT)

    # Create new confirmation flow
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO flows (tenant_id, name, description, is_active) VALUES (%s, 'Order Confirmation Flow', 'System flow for order confirmations.', true) RETURNING id",
            (tenant_id,)
        )
        flow_id = cursor.fetchone()["id"]
        
        # Node 1: Main Confirmation Menu
        cursor = conn.execute(
            """
            INSERT INTO flow_nodes (flow_id, name, node_type, prompt_text, prompt_audio, prompt_lang, gather_timeout_seconds, max_retries)
            VALUES (%s, 'Confirm Menu', 'menu', 'Confirm menu prompt', %s, 'bn', 8, 2)
            RETURNING id
            """,
            (flow_id, audio_filename)
        )
        menu_node_id = cursor.fetchone()["id"]
        
        # Update flow root_node_id
        conn.execute("UPDATE flows SET root_node_id = %s WHERE id = %s", (menu_node_id, flow_id))
        
        # Node 2: Confirmed Message
        cursor = conn.execute(
            """
            INSERT INTO flow_nodes (flow_id, name, node_type, prompt_text, prompt_audio, prompt_lang, gather_timeout_seconds, max_retries)
            VALUES (%s, 'Confirmed Msg', 'message', %s, %s, 'bn', 0, 0)
            RETURNING id
            """,
            (flow_id, CONFIRMED_MSG_TEXT, confirmed_audio)
        )
        confirm_node_id = cursor.fetchone()["id"]
        
        # Node 3: Cancelled Message
        cursor = conn.execute(
            """
            INSERT INTO flow_nodes (flow_id, name, node_type, prompt_text, prompt_audio, prompt_lang, gather_timeout_seconds, max_retries)
            VALUES (%s, 'Cancelled Msg', 'message', %s, %s, 'bn', 0, 0)
            RETURNING id
            """,
            (flow_id, CANCELLED_MSG_TEXT, cancelled_audio)
        )
        cancel_node_id = cursor.fetchone()["id"]
        
        # Map Transitions
        # DTMF 1 -> Node 2 (Confirmed)
        conn.execute(
            "INSERT INTO flow_transitions (node_id, digit, next_node_id, label) VALUES (%s, '1', %s, 'Confirm')",
            (menu_node_id, confirm_node_id)
        )
        # DTMF 2 -> Node 3 (Cancelled)
        conn.execute(
            "INSERT INTO flow_transitions (node_id, digit, next_node_id, label) VALUES (%s, '2', %s, 'Cancel')",
            (menu_node_id, cancel_node_id)
        )
        
    return flow_id

def update_status_from_call(
    order_id: int,
    tenant_id: int,
    call_result: str,
    dtmf_digit: str | None,
    talk_seconds: int,
    call_id: int | None = None
) -> None:
    """Updates order status, computes charges, deducts wallet, and triggers retry or webhook."""
    order = get_order_by_id_system(order_id)
    if not order:
        return

    # Guard: never overwrite a terminal status with a late/duplicate call result.
    # CONFIRMED, CANCELLED, UNREACHABLE are final — a retry or duplicate delivery
    # must not reopen them.
    _TERMINAL = {"CONFIRMED", "CANCELLED", "UNREACHABLE"}
    if order.get("order_status") in _TERMINAL:
        logger.info(
            "update_status_from_call: order %s already terminal (%s) — skipping late/duplicate result %s",
            order_id, order["order_status"], call_result,
        )
        return

    # Inferred call_id if not provided
    if not call_id:
        row = db.query_one(
            "SELECT call_id FROM order_call_attempts WHERE order_id = %s ORDER BY attempt_no DESC LIMIT 1",
            (order_id,)
        )
        if row:
            call_id = row["call_id"]

    # Billing is centralized in billing_service.finalize_call_billing() —
    # it's idempotent (no-ops once calls.wallet_transaction_id is set), so
    # whether call_runner.py already billed this call or not, this is safe
    # to call again. Direct/manual calls bill the same way via call_runner.
    #
    # finalize_call_billing reads calls.status (not the call_result param)
    # to decide what to charge, so make sure it's in sync first — normally
    # call_runner.py already wrote the terminal status before calling here,
    # but any other caller (or a test) might not have, and billing on a
    # stale non-terminal status would silently charge nothing.
    charge = Decimal("0.0000")
    billable_seconds = 0
    txn_id = None
    if call_id:
        db.execute("UPDATE calls SET status = %s WHERE id = %s", (call_result, call_id))
        from app.services.billing_service import finalize_call_billing
        result = finalize_call_billing(call_id)
        charge = result["charge"]
        billable_seconds = result["billable_seconds"]
        txn_id = result["wallet_transaction_id"]

        update_call_attempt_by_call_id_system(
            call_id=call_id,
            result=call_result,
            dtmf_digit=dtmf_digit,
            talk_seconds=talk_seconds,
            charge=charge,
            billable_seconds=billable_seconds,
            wallet_transaction_id=txn_id
        )

    # Determine status mapping
    next_status = "FAILED"
    if call_result == "completed":
        if dtmf_digit == "1":
            next_status = "CONFIRMED"
        elif dtmf_digit == "2":
            next_status = "CANCELLED"
        else:
            # no input/timeout or invalid DTMF
            next_status = "FAILED"
    elif call_result in ("no_answer", "busy"):
        next_status = "NO_ANSWER"
    elif call_result == "failed":
        next_status = "FAILED"

    # [SAAS STEP 12] DTMF mapped to CONFIRMED/CANCELLED/NO_ANSWER/FAILED
    logger.info(f"[SAAS STEP 12] DTMF outcome status mapped: call_id={call_id}, Result={call_result}, DTMF={dtmf_digit} -> Mapped Order Status={next_status}")

    # Update order state
    if next_status == "CONFIRMED":
        set_order_confirmed(tenant_id, order_id)
    else:
        update_order_status_system(order_id, next_status)

    # Fire webhook asynchronously
    deliver_async(order_id)

    # If NO_ANSWER: trigger retry. Pass the raw call_result through (not
    # next_status) so schedule_retry can filter against retry_rules.retry_on
    # — "no_answer" and "busy" are both collapsed into NO_ANSWER above for
    # the order_status enum, but retry eligibility is configured per the
    # original result, not the collapsed status.
    if next_status == "NO_ANSWER":
        from app.services.retry_service import schedule_retry
        schedule_retry(order_id, tenant_id, call_result=call_result)

def get_order(order_id: int | str, tenant_id: int) -> dict[str, Any] | None:
    """Retrieve details and call history of a specific order by ID or external ID."""
    order = None
    if isinstance(order_id, int) or (isinstance(order_id, str) and order_id.isdigit()):
        order = get_order_by_id(tenant_id, int(order_id))
    if not order:
        order = get_order_by_external_id(tenant_id, str(order_id))
        
    if not order:
        return None
        
    order = dict(order)
    attempts = get_call_attempts_for_order(tenant_id, order["id"])
    order["attempts"] = attempts
    return order


def manual_recall(order_id: int, tenant_id: int) -> int:
    """Manually trigger a re-call for an existing order.

    Validates the order belongs to the tenant, checks balance, resets
    status to CALL_QUEUED, and dispatches synchronously (in the calling
    thread's context, which is already a background thread from the route).
    Returns the new call_id.
    """
    order = get_order_by_id_system(order_id)
    if not order or order.get("tenant_id") != tenant_id:
        raise ValueError("Order not found or access denied.")

    terminal_statuses = {"CONFIRMED", "CANCELLED", "UNREACHABLE", "CALLING", "CALL_QUEUED"}
    if order.get("order_status") in terminal_statuses:
        raise ValueError(f"Cannot recall order with status {order['order_status']}.")

    min_charge = estimate_charge(tenant_id)
    balance_ok = check_balance(tenant_id, min_charge)
    if not balance_ok:
        from app.services.wallet_service import get_balance
        raise InsufficientBalanceError(get_balance(tenant_id), min_charge)

    update_order_status_system(order_id, "CALL_QUEUED")
    call_id = queue_confirmation_call(order_id, tenant_id)
    increment_order_attempts(tenant_id, order_id)
    return call_id


def list_orders(tenant_id: int, filters: dict | None = None, page: int = 1, per_page: int = 20) -> dict:
    """Paginated order list for API. Returns {data, total}."""
    f = filters or {}
    offset = (page - 1) * per_page
    orders = get_orders(
        tenant_id,
        status=f.get("status"),
        search=f.get("search"),
        date_from=f.get("date_from"),
        date_to=f.get("date_to"),
        limit=per_page,
        offset=offset,
    )
    total = get_orders_count(
        tenant_id,
        status=f.get("status"),
        search=f.get("search"),
        date_from=f.get("date_from"),
        date_to=f.get("date_to"),
    )
    return {"data": orders, "total": total}
