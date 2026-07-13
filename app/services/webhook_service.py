from __future__ import annotations

import hmac
import hashlib
import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
import datetime
from typing import Any

from app.db import db
from app.models.order import get_order_by_id_system
from app.models.tenant import get_tenant_by_id


logger = logging.getLogger("ccall")
_FLASK_ENV = os.environ.get("FLASK_ENV", "production")
# Explicit opt-in for local dev only. Production startup rejects true (see config.py).
_ALLOW_PRIVATE_WEBHOOKS = (
    os.environ.get("ALLOW_PRIVATE_WEBHOOKS_FOR_DEV", "false").lower() == "true"
    and _FLASK_ENV in ("development", "testing")
)


def mask_phone(phone: str) -> str:
    """Mask phone numbers keeping the first 3 and last 4 digits, replacing middle with ****."""
    if not phone:
        return ""
    clean = phone.replace("+88", "").replace("88", "")
    if len(clean) == 11:
        return clean[:3] + "****" + clean[7:]
    elif len(clean) > 7:
        return clean[:3] + "****" + clean[-4:]
    return clean


def build_payload(order: dict[str, Any]) -> dict[str, Any]:
    """Construct standard webhook payload for the given order."""
    order_id = order["id"]
    
    # Query latest call attempt details
    dtmf_row = db.query_one(
        """
        SELECT dtmf_digit, talk_seconds 
        FROM order_call_attempts 
        WHERE order_id = %s 
        ORDER BY attempt_no DESC LIMIT 1
        """,
        (order_id,)
    )
    dtmf_digit = dtmf_row["dtmf_digit"] if dtmf_row else None
    talk_seconds = int(dtmf_row["talk_seconds"] or 0) if dtmf_row else 0
    
    call_row = db.query_one(
        "SELECT call_id FROM order_call_attempts "
        "WHERE order_id = %s ORDER BY attempt_no DESC LIMIT 1",
        (order_id,)
    )
    actual_call_id = call_row["call_id"] if call_row else None
    
    order_status = order["order_status"]
    event_map = {
        "CONFIRMED": "order.confirmed",
        "CANCELLED": "order.cancelled",
        "NO_ANSWER": "order.no_answer",
        "UNREACHABLE": "order.unreachable",
        "FAILED": "order.failed"
    }
    event = event_map.get(order_status, "order.failed")
    
    confirmed_at = None
    if order.get("confirmed_at"):
        c_at = order["confirmed_at"]
        if hasattr(c_at, "strftime"):
            confirmed_at = c_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            confirmed_at = str(c_at)
            
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    masked_phone = mask_phone(order.get("phone", ""))
    
    amount_str = "0.00"
    if order.get("amount") is not None:
        amount_str = f"{float(order['amount']):.2f}"
        
    return {
        "event": event,
        "order_id": order.get("external_order_id"),
        # internal_id = orders.id (order primary key)
        "internal_id": order_id,
        "order_internal_id": order_id,
        # call_id = calls.id (the actual SIP call record)
        "call_id": actual_call_id,
        "order_status": order_status,
        "dtmf_digit": dtmf_digit,
        "customer_name": order.get("customer_name"),
        "phone": masked_phone,
        "amount": amount_str,
        "talk_seconds": talk_seconds,
        "attempt_no": order.get("webhook_attempt_no", 1),
        "confirmed_at": confirmed_at,
        "timestamp": timestamp
    }


def sign_payload(payload_json_str: str, secret: str) -> str:
    """Generate HMAC-SHA256 signature for payload verification."""
    sig = hmac.new(
        secret.encode("utf-8"),
        payload_json_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"sha256={sig}"


def deliver_async(order_id: int) -> None:
    """Queue webhook delivery in a background thread."""
    threading.Thread(target=_deliver_with_retry, args=(order_id,), daemon=True).start()


def deliver_webhook(order_id: int, attempt_no: int = 1) -> bool:
    """Synchronously attempts a single webhook delivery and returns success status.
    Uses HMAC-SHA256 signature in both X-CCAll-Signature header and payload body for backwards compatibility.
    """
    order = get_order_by_id_system(order_id)
    if not order or not order.get("webhook_url"):
        return False
        
    tenant_id = order["tenant_id"]
    webhook_url = order["webhook_url"]
    
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        return False
        
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
            
    secret = settings.get("webhook_secret") or str(tenant["uuid"])
    
    order_copy = dict(order)
    order_copy["webhook_attempt_no"] = attempt_no
    payload = build_payload(order_copy)
    
    # 1. Sign raw payload (excluding signature key) for headers (legacy)
    payload_json_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    signature = sign_payload(payload_json_str, secret)

    # 2. Generate new timestamp-based signature
    timestamp = str(int(time.time()))
    sig_base = f"{timestamp}.{payload_json_str}"
    timestamp_signature = f"sha256={hmac.new(secret.encode('utf-8'), sig_base.encode('utf-8'), hashlib.sha256).hexdigest()}"

    # [SAAS STEP 14] Webhook signature payload string
    masked_secret = secret[:4] + "..." + secret[-4:] if secret else "None"
    logger.info(f"[SAAS STEP 14] Webhook signature payload string: secret={masked_secret}, base_string={sig_base}, signature={timestamp_signature}")
    
    # 3. Append signature key inside payload body for backward compatibility with integration tests
    payload_with_sig = dict(payload)
    payload_with_sig["signature"] = signature
    payload_json_str_with_sig = json.dumps(payload_with_sig, sort_keys=True, ensure_ascii=False)
    
    # Log attempt in DB
    from app.models.webhook_log import create_log
    log_data = {
        "tenant_id": tenant_id,
        "order_id": order_id,
        "url": webhook_url,
        "payload": payload_with_sig,
        "status_code": None,
        "response_body": None,
        "attempt_no": attempt_no,
        "success": False
    }
    log_id = create_log(log_data)
    
    req_body = payload_json_str_with_sig.encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=req_body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "C-Call-Webhook/1.0",
            "X-CCAll-Signature": signature,
            "X-C-Call-Signature": timestamp_signature,
            "X-C-Call-Timestamp": timestamp
        },
        method="POST"
    )
    
    # Enforce maximum 3 redirects limit
    class MaxRedirectHandler(urllib.request.HTTPRedirectHandler):
        def __init__(self, max_redirects=3):
            self.max_redirects = max_redirects
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            redirects = getattr(req, "redirect_count", 0)
            if redirects >= self.max_redirects:
                raise urllib.error.HTTPError(req.full_url, 310, "Too many redirects", headers, fp)
            new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
            new_req.redirect_count = redirects + 1
            return new_req
            
    opener = urllib.request.build_opener(MaxRedirectHandler(3))
    urllib.request.install_opener(opener)
    
    status_code = None
    response_body = ""
    success = False
    
    # [SAAS STEP 13] WooCommerce webhook URL called
    logger.info(f"[SAAS STEP 13] WooCommerce webhook URL called. Target URL: {webhook_url}, Order ID: {order_id}, Attempt: {attempt_no}, Payload: {payload_json_str_with_sig}")

    try:
        # Re-validate URL immediately before delivery — DNS rebinding: a hostname that
        # resolved to a public IP at order creation can resolve to a private IP by now.
        import socket, ipaddress, urllib.parse as _up
        _parsed = _up.urlparse(webhook_url)
        _hostname = _parsed.hostname or ""
        if not _ALLOW_PRIVATE_WEBHOOKS:
            try:
                _resolved = socket.gethostbyname(_hostname)
                _ip = ipaddress.ip_address(_resolved)
                if _ip.is_private or _ip.is_loopback or _ip.is_link_local:
                    logger.warning(
                        "Webhook delivery blocked — %s resolved to private/loopback/link-local IP %s (SSRF guard)",
                        _hostname, _resolved,
                    )
                    return False
            except Exception:
                pass  # DNS failure or literal IP — pass through to HTTP attempt

        # Enforce 10s connection timeout
        import ssl
        context = None
        if webhook_url.startswith("https://localhost") or webhook_url.startswith("https://127.0.0.1"):
            context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=10, context=context) as resp:
            status_code = resp.status
            # Read up to 1000 bytes and truncate response body at 500 characters
            response_body = resp.read(1000).decode("utf-8", errors="ignore")[:500]
            if 200 <= status_code < 300:
                success = True
    except urllib.error.HTTPError as err:
        status_code = err.code
        try:
            response_body = err.read(1000).decode("utf-8", errors="ignore")[:500]
        except Exception:
            response_body = str(err)[:500]
    except Exception as exc:
        status_code = 0
        response_body = str(exc)[:500]

    # [SAAS STEP 15] Webhook HTTP code/body
    logger.info(f"[SAAS STEP 15] Webhook HTTP code: {status_code}, Body: {response_body}")
        
    from app.models.webhook_log import update_log
    update_log(log_id, status_code, response_body, success)
    
    if success:
        db.execute(
            "UPDATE orders SET webhook_status = 'sent', updated_at = now() WHERE id = %s",
            (order_id,)
        )
    else:
        db.execute(
            "UPDATE orders SET webhook_status = 'failed', updated_at = now() WHERE id = %s",
            (order_id,)
        )
        
    # [SAAS STEP 16] Final delivery result saved
    logger.info(f"[SAAS STEP 16] Final delivery result saved. Success: {success}, Order ID: {order_id}, Log ID: {log_id}")

    return success


def _deliver_with_retry(order_id: int) -> None:
    """Deliver webhook with backoff retry schedule: immediate, 60s, 300s."""
    order = get_order_by_id_system(order_id)
    if not order or not order.get("webhook_url"):
        return
        
    tenant_id = order["tenant_id"]
    webhook_url = order["webhook_url"]
    
    terminal_statuses = ("CONFIRMED", "CANCELLED", "NO_ANSWER", "UNREACHABLE", "FAILED")
    if order["order_status"] not in terminal_statuses:
        return
        
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        return
        
    from app.models.webhook_log import get_logs_for_order
    existing_logs = get_logs_for_order(order_id)
    start_attempt = len(existing_logs) + 1 if existing_logs else 1
    
    for attempt in range(start_attempt, 4):
        # Update orders.webhook_status to pending during retry
        db.execute(
            "UPDATE orders SET webhook_status = 'pending', updated_at = now() WHERE id = %s",
            (order_id,)
        )
        
        success = deliver_webhook(order_id, attempt)
        if success:
            return
            
        if attempt < 3:
            delay = 60 if attempt == 1 else 300
            logger.warning(f"Webhook delivery failed for order {order_id} (Attempt {attempt}). Sleeping {delay}s...")
            time.sleep(delay)
        else:
            # 3 failures reached
            logger.error(f"Webhook delivery failed permanently after 3 attempts for order {order_id}.")
            try:
                from app.services.email_service import send_webhook_failure_alert
                send_webhook_failure_alert(tenant, order.get("external_order_id") or str(order_id), webhook_url)
            except Exception as mail_exc:
                logger.error(f"Failed to send webhook failure alert email: {mail_exc}")


def retry_failed_webhooks() -> int:
    """APScheduler task running to retry failed webhook deliveries with attempts remaining."""
    logger.info("Running scheduled webhook retry job...")
    failed_attempts = db.query(
        """
        SELECT DISTINCT ON (o.id) o.id, o.tenant_id
        FROM orders o
        JOIN webhook_logs wl ON o.id = wl.order_id
        WHERE o.webhook_status = 'failed'
          AND wl.success = false
          AND wl.attempt_no < 3
        """
    )
    
    count = 0
    for row in failed_attempts:
        order_id = row["id"]
        deliver_async(order_id)
        count += 1
        
    return count


def get_webhook_logs(order_id: int, tenant_id: int) -> list[dict[str, Any]]:
    """Retrieve webhook logs for an order under a specific tenant's context."""
    rows = db.query(
        "SELECT * FROM webhook_logs WHERE order_id = %s AND tenant_id = %s ORDER BY sent_at DESC",
        (order_id, tenant_id)
    )
    return [dict(r) for r in rows]
