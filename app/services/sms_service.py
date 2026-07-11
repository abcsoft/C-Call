# app/services/sms_service.py
from __future__ import annotations

import os
import requests
import json
import logging
from decimal import Decimal
from app.db import db
from app.models.tenant import get_tenant_by_id

logger = logging.getLogger("ccall")


def send_sms(phone: str, message: str, tenant_id: int, order_id: int | None = None) -> dict:
    """Send an SMS via SmsLayer API. Uses tenant's API key if set, or platform key as fallback.
    Logs transaction to sms_logs table.
    """
    # 1. Load tenant SMS settings
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        logger.error("send_sms: Tenant %s not found", tenant_id)
        return {"success": False, "message": "Tenant not found"}
        
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
            
    api_key = settings.get("smslayer_api_key")
    
    # 2. Check if platform key should be used
    if not api_key:
        api_key = db.get_setting("PLATFORM_SMSLAYER_API_KEY") or os.environ.get("PLATFORM_SMSLAYER_API_KEY")
        gateway = "smslayer_platform"
    else:
        gateway = "smslayer"

    if not api_key:
        logger.error("send_sms: SMSLayer API key not configured for tenant %s or platform", tenant_id)
        return {"success": False, "message": "SMSLayer API key not configured"}
        
    # 3. Send HTTP request to smslayer.fun
    url = "https://smslayer.fun/api/sms/send"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "to": phone,
        "message": message,
        "from": "C-Call"
    }
    
    success = False
    message_id = None
    cost = 0.0
    status = "failed"
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        status_code = response.status_code
        
        # Parse JSON response safely
        try:
            resp_data = response.json()
        except Exception:
            resp_data = {}
            
        if 200 <= status_code < 300:
            success = True
            status = "sent"
            message_id = resp_data.get("message_id") or resp_data.get("id") or "success_placeholder"
            cost = float(resp_data.get("cost") or 0.0)
            
            # If using platform gateway, we charge the tenant according to platform rate
            if gateway == "smslayer_platform":
                rate = float(db.get_setting("PLATFORM_SMS_RATE") or 0.0)
                cost = rate
                
                # Deduct cost from tenant wallet if rate > 0
                if cost > 0:
                    from app.services.wallet_service import deduct
                    try:
                        deduct(
                            tenant_id=tenant_id,
                            amount=Decimal(str(cost)),
                            description=f"SMS Fallback Charge for phone {phone}",
                            ref_id=message_id
                        )
                    except Exception as exc:
                        logger.error("send_sms: Failed to deduct SMS charge of %s for tenant %s: %s", cost, tenant_id, exc)
                        # We still proceed since SMS was already sent
        else:
            status = f"failed_http_{status_code}"
            logger.error("send_sms failed with HTTP %d: %s", status_code, response.text)
    except Exception as exc:
        status = f"error_{type(exc).__name__}"
        logger.error("send_sms: HTTP request failed: %s", exc, exc_info=True)
        
    # 4. Log to sms_logs table
    db.execute(
        """
        INSERT INTO sms_logs (tenant_id, order_id, phone, message, gateway, message_id, status, cost, sent_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        """,
        (tenant_id, order_id, phone, message, gateway, message_id, status, cost)
    )
    
    return {"success": success, "message_id": message_id, "cost": cost}


def send_order_fallback_sms(order_id: int, tenant_id: int) -> bool:
    """Load order data, compile SMS message from template, send it, and update order status."""
    # 1. Load order
    order = db.query_one("SELECT * FROM orders WHERE tenant_id = %s AND id = %s", (tenant_id, order_id))
    if not order:
        logger.error("send_order_fallback_sms: Order %s not found for tenant %s", order_id, tenant_id)
        return False
        
    # 2. Get SMS Template from tenant settings
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        return False
        
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
            
    template = settings.get("sms_template") or "আপনার অর্ডার #{order_id} ({amount} টাকা) নিশ্চিত করতে কল করুন: {callback_number}"
    
    # Determine callback number (dedicated CLI or global default)
    from app.services.number_service import select_caller_id
    callback_number = select_caller_id(tenant_id) or db.get_setting("GLOBAL_CALLBACK_NUMBER") or "C-Call"
    
    # Build message replacing template variables
    amount_str = f"{float(order['amount']):.2f}" if order.get("amount") is not None else "0.00"
    message = template.replace("{order_id}", str(order.get("external_order_id") or order_id)) \
                      .replace("{amount}", amount_str) \
                      .replace("{callback_number}", callback_number)
                      
    # 3. Call send_sms
    res = send_sms(order["phone"], message, tenant_id, order_id=order_id)
    
    if res.get("success"):
        # Update order: sms_sent=true, sms_sent_at=now()
        db.execute(
            "UPDATE orders SET sms_sent = true, sms_sent_at = now() WHERE tenant_id = %s AND id = %s",
            (tenant_id, order_id)
        )
        logger.info("SMS Fallback sent successfully for order %s", order_id)
        return True
    return False
