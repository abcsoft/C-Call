from __future__ import annotations

import logging
import json
from flask import render_template, current_app
from flask_mail import Message
from app.extensions import mail

logger = logging.getLogger("ccall")

# Plain text versions in Python strings
VERIFICATION_TEXT = """Dear User,

Thank you for registering on C-Call IVR. Please verify your email by clicking the link below:
{verification_url}

If you did not request this registration, please ignore this email.

Best regards,
C-Call IVR Team"""

RESET_TEXT = """Dear User,

We received a request to reset your password on C-Call IVR. Please click the link below to set a new password:
{reset_url}

If you did not request a password reset, please ignore this email.

Best regards,
C-Call IVR Team"""

LOW_BALANCE_TEXT = """Dear {business_name},

Your prepay wallet balance on C-Call IVR is low.
Current Balance: {balance} BDT
Alert Threshold: {threshold} BDT

Please recharge your wallet from the panel to ensure uninterrupted IVR services.

Best regards,
C-Call IVR Team"""

WEBHOOK_FAILURE_TEXT = """Dear {business_name},

Our system has detected 3 consecutive webhook delivery failures for order {order_id} to URL:
{url}

Please check your webhook endpoint and ensure it is functioning correctly.

Best regards,
C-Call IVR Team"""

ORDER_UNREACHABLE_TEXT = """Dear {business_name},

Order {order_id} for customer {customer_name} (phone: {phone}) has failed to be confirmed after reaching the maximum number of call attempts.

Best regards,
C-Call IVR Team"""


def _get_val(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def send_verification_email(user, token) -> None:
    """Send account email verification link."""
    email = _get_val(user, "email")
    app_url = current_app.config.get("APP_URL", "http://localhost:5000")
    verification_url = f"{app_url}/auth/verify?token={token}"
    
    msg = Message(
        subject="[C-Call] Verify Your Email",
        recipients=[email],
        body=VERIFICATION_TEXT.format(verification_url=verification_url)
    )
    try:
        msg.html = render_template("emails/verification.html", verification_url=verification_url, user=user)
    except Exception as exc:
        logger.warning(f"Failed to render HTML verification template, sending text only: {exc}")
        
    try:
        mail.send(msg)
        logger.info(f"Sent verification email to {email}")
    except Exception as exc:
        logger.error(f"Failed to send verification email to {email}: {exc}")


def send_reset_email(user, token) -> None:
    """Send password reset link."""
    email = _get_val(user, "email")
    app_url = current_app.config.get("APP_URL", "http://localhost:5000")
    reset_url = f"{app_url}/auth/reset-password?token={token}"
    
    msg = Message(
        subject="[C-Call] Reset Your Password",
        recipients=[email],
        body=RESET_TEXT.format(reset_url=reset_url)
    )
    try:
        msg.html = render_template("emails/reset_password.html", reset_url=reset_url, user=user)
    except Exception as exc:
        logger.warning(f"Failed to render HTML reset template, sending text only: {exc}")
        
    try:
        mail.send(msg)
        logger.info(f"Sent password reset email to {email}")
    except Exception as exc:
        logger.error(f"Failed to send password reset email to {email}: {exc}")


def send_low_balance_alert(tenant, current_balance) -> None:
    """Send low balance alert email when a merchant's wallet goes below the threshold."""
    email = _get_val(tenant, "email")
    business_name = _get_val(tenant, "business_name", "Merchant")
    
    # Extract threshold from settings
    settings = _get_val(tenant, "settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
    threshold = settings.get("low_balance_threshold", 50.00)
    
    msg = Message(
        subject="[C-Call IVR] Low Wallet Balance Alert",
        recipients=[email],
        body=LOW_BALANCE_TEXT.format(business_name=business_name, balance=current_balance, threshold=threshold)
    )
    try:
        msg.html = render_template("emails/low_balance_alert.html", tenant=tenant, balance=current_balance, threshold=threshold)
    except Exception as exc:
        logger.warning(f"Failed to render HTML low balance alert template, sending text only: {exc}")
        
    try:
        mail.send(msg)
        logger.info(f"Sent low balance alert email to {email}")
    except Exception as exc:
        logger.error(f"Failed to send low balance alert email to {email}: {exc}")


def send_webhook_failure_alert(tenant, order_id, url) -> None:
    """Send alert email when consecutive webhook delivery failures occur."""
    email = _get_val(tenant, "email")
    business_name = _get_val(tenant, "business_name", "Merchant")
    
    msg = Message(
        subject="[C-Call IVR] Webhook Delivery Failure Alert",
        recipients=[email],
        body=WEBHOOK_FAILURE_TEXT.format(business_name=business_name, order_id=order_id, url=url)
    )
    try:
        msg.html = render_template("emails/webhook_failure_alert.html", tenant=tenant, order_id=order_id, url=url)
    except Exception as exc:
        logger.warning(f"Failed to render HTML webhook failure alert template, sending text only: {exc}")
        
    try:
        mail.send(msg)
        logger.info(f"Sent webhook failure alert email to {email}")
    except Exception as exc:
        logger.error(f"Failed to send webhook failure alert email to {email}: {exc}")


def send_order_unreachable_alert(tenant, order) -> None:
    """Send alert email when an order fails to confirm after maximum retry attempts."""
    email = _get_val(tenant, "email")
    business_name = _get_val(tenant, "business_name", "Merchant")
    order_id = _get_val(order, "external_order_id") or _get_val(order, "id")
    customer_name = _get_val(order, "customer_name", "Customer")
    phone = _get_val(order, "phone", "")
    
    msg = Message(
        subject="[C-Call IVR] Order Unreachable Alert",
        recipients=[email],
        body=ORDER_UNREACHABLE_TEXT.format(business_name=business_name, order_id=order_id, customer_name=customer_name, phone=phone)
    )
    try:
        msg.html = render_template("emails/order_unreachable_alert.html", tenant=tenant, order=order)
    except Exception as exc:
        logger.warning(f"Failed to render HTML order unreachable alert template, sending text only: {exc}")
        
    try:
        mail.send(msg)
        logger.info(f"Sent order unreachable alert email to {email}")
    except Exception as exc:
        logger.error(f"Failed to send order unreachable alert email to {email}: {exc}")


def send_team_invite_email(email: str, tenant_name: str, token: str) -> None:
    """Send team invitation link."""
    app_url = current_app.config.get("APP_URL", "http://localhost:5000")
    invite_url = f"{app_url}/auth/accept-invite/{token}"

    body = f"""Dear User,

You have been invited to join the team of "{tenant_name}" on C-Call IVR.

Please click the link below to accept the invitation and set up your account:
{invite_url}

This invitation link will expire in 48 hours.

Best regards,
C-Call IVR Team"""

    msg = Message(
        subject=f"[C-Call] Join {tenant_name} Team",
        recipients=[email],
        body=body
    )

    try:
        mail.send(msg)
        logger.info(f"Sent team invitation email to {email}")
    except Exception as exc:
        logger.error(f"Failed to send team invitation email to {email}: {exc}")


# ── Subscription lifecycle email notifications ─────────────────────────────

def _get_tenant_email(tenant_id: int):
    from app.db import db
    return db.query_one("SELECT email, business_name FROM tenants WHERE id = %s", (tenant_id,))


def send_trial_ending_soon_email(tenant_id: int, trial_ends_at) -> None:
    t = _get_tenant_email(tenant_id)
    if not t:
        return
    import datetime
    days_left = max(0, (trial_ends_at - datetime.datetime.now(datetime.timezone.utc)).days) if hasattr(trial_ends_at, "tzinfo") else 3
    body = (
        f"Dear {t['business_name']},\n\n"
        f"Your C-Call IVR free trial ends in {days_left} day(s). "
        f"Please subscribe from your dashboard to continue uninterrupted service.\n\n"
        f"C-Call IVR Team"
    )
    try:
        mail.send(Message(subject="[C-Call] Your trial is ending soon", recipients=[t["email"]], body=body))
        logger.info("Sent trial_ending_soon email to tenant %s", tenant_id)
    except Exception as exc:
        logger.warning("trial_ending_soon email failed for tenant %s: %s", tenant_id, exc)


def send_grace_period_email(tenant_id: int, grace_ends_at) -> None:
    t = _get_tenant_email(tenant_id)
    if not t:
        return
    body = (
        f"Dear {t['business_name']},\n\n"
        f"Your C-Call IVR subscription payment is overdue. "
        f"You have a grace period until {grace_ends_at.strftime('%Y-%m-%d') if hasattr(grace_ends_at, 'strftime') else grace_ends_at} to renew "
        f"before new orders and calls are suspended.\n\n"
        f"Please log in and renew your subscription.\n\nC-Call IVR Team"
    )
    try:
        mail.send(Message(subject="[C-Call] Action required: payment overdue", recipients=[t["email"]], body=body))
        logger.info("Sent grace_period email to tenant %s", tenant_id)
    except Exception as exc:
        logger.warning("grace_period email failed for tenant %s: %s", tenant_id, exc)


def send_past_due_email(tenant_id: int) -> None:
    t = _get_tenant_email(tenant_id)
    if not t:
        return
    body = (
        f"Dear {t['business_name']},\n\n"
        f"Your C-Call IVR account is now past due. New orders, calls, and retries are paused.\n\n"
        f"Please renew your subscription to restore full service.\n\nC-Call IVR Team"
    )
    try:
        mail.send(Message(subject="[C-Call] Service suspended: past due", recipients=[t["email"]], body=body))
        logger.info("Sent past_due email to tenant %s", tenant_id)
    except Exception as exc:
        logger.warning("past_due email failed for tenant %s: %s", tenant_id, exc)


def send_subscription_expired_email(tenant_id: int) -> None:
    t = _get_tenant_email(tenant_id)
    if not t:
        return
    body = (
        f"Dear {t['business_name']},\n\n"
        f"Your C-Call IVR subscription has expired. Service has been paused.\n\n"
        f"Please subscribe to re-activate your account.\n\nC-Call IVR Team"
    )
    try:
        mail.send(Message(subject="[C-Call] Subscription expired", recipients=[t["email"]], body=body))
        logger.info("Sent subscription_expired email to tenant %s", tenant_id)
    except Exception as exc:
        logger.warning("subscription_expired email failed for tenant %s: %s", tenant_id, exc)


def send_renewal_confirmation_email(tenant_id: int) -> None:
    t = _get_tenant_email(tenant_id)
    if not t:
        return
    body = (
        f"Dear {t['business_name']},\n\n"
        f"Your C-Call IVR subscription has been renewed successfully. "
        f"Your service is now fully active.\n\nThank you!\n\nC-Call IVR Team"
    )
    try:
        mail.send(Message(subject="[C-Call] Subscription renewed", recipients=[t["email"]], body=body))
        logger.info("Sent renewal_confirmation email to tenant %s", tenant_id)
    except Exception as exc:
        logger.warning("renewal_confirmation email failed for tenant %s: %s", tenant_id, exc)

