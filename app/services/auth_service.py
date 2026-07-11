# app/services/auth_service.py
"""Authentication and Tenant registration service layer."""
from __future__ import annotations

import datetime
import hashlib
import logging
import secrets
from typing import Any

from flask import current_app, url_for
from flask_mail import Message

from app.db import db
from app.extensions import mail
from app.models.tenant import create_tenant, get_tenant_by_id, invalidate_tenant_caches, update_tenant_settings
from app.models.user import (
    create_user,
    get_user_by_email,
    get_user_by_id,
    verify_user_password,
    create_verification_token,
    get_verification_token
)

logger = logging.getLogger("ccall")


class AuthError(Exception):
    """Exception raised for user authentication failures."""
    pass


def register_tenant(form_data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Register a new merchant tenant and create their admin user."""
    email = form_data["email"]
    business_name = form_data["business_name"]
    password = form_data["password"]

    # Check if email is already registered
    existing_user = get_user_by_email(email)
    if existing_user:
        raise ValueError("এই ইমেইল এড্রেসটি ইতিমধ্যেই নিবন্ধিত।")

    # 1. Create Tenant (free plan, trial status)
    # create_tenant in models/tenant.py initializes:
    #   - tenants table entry
    #   - wallets table entry (balance=0)
    #   - retry_rules (3 rows)
    tenant_id = create_tenant(
        business_name=business_name,
        email=email,
        plan_name="free",
        status="trial"
    )

    # Generate and save webhook secret
    webhook_secret = f"whsec_{secrets.token_hex(32)}"
    update_tenant_settings(tenant_id, {"webhook_secret": webhook_secret})

    # Create default customer rates for the tenant
    db.execute(
        """
        INSERT INTO customer_rates (tenant_id, rate_per_min, rate_per_call, min_charge)
        VALUES (%s, 0.80, 0.00, 0.80)
        """,
        (tenant_id,)
    )

    # 2. Create Admin User (email_verified=false)
    user_id = create_user(
        tenant_id=tenant_id,
        email=email,
        password_plaintext=password,
        role="admin",
        email_verified=False
    )

    tenant = get_tenant_by_id(tenant_id)
    user = get_user_by_id(user_id)

    if not tenant or not user:
        raise ValueError("নিবন্ধন সম্পন্ন করার সময় ত্রুটি ঘটেছে।")

    # 3. Send verification email
    try:
        send_verification_email(user)
    except Exception as mail_err:
        logger.error(f"Failed to send registration verification email to {email}: {mail_err}")

    return tenant, user


def verify_email(token: str) -> dict[str, Any]:
    """Validate verification token and activate tenant."""
    token_row = get_verification_token(token)
    if not token_row or token_row.get("used_at"):
        raise ValueError("ভেরিফিকেশন টোকেনটি সঠিক নয় অথবা ইতিমধ্যেই ব্যবহৃত হয়েছে।")

    expires_at = token_row["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    elif expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)

    now = datetime.datetime.now(datetime.timezone.utc)
    if expires_at < now:
        raise ValueError("ভেরিফিকেশন লিঙ্কটির মেয়াদ শেষ হয়ে গেছে।")

    user_id = token_row["user_id"]
    user = get_user_by_id(user_id)
    if not user:
        raise ValueError("ইউজার পাওয়া যায়নি।")

    # Mark user verified, set tenant status to active, mark token used
    with db.transaction() as conn:
        conn.execute("UPDATE users SET email_verified = true WHERE id = %s", (user_id,))
        if user.get("tenant_id"):
            conn.execute("UPDATE tenants SET status = 'active' WHERE id = %s", (user["tenant_id"],))
        conn.execute("UPDATE email_verifications SET used_at = now() WHERE token = %s", (token,))

    if user.get("tenant_id"):
        invalidate_tenant_caches(user["tenant_id"])

    return get_user_by_id(user_id)



def authenticate_user(email: str, password: str) -> dict[str, Any]:
    """Authenticate a user by email and password."""
    user = get_user_by_email(email)
    if not user:
        raise AuthError("ইমেইল অথবা পাসওয়ার্ডটি সঠিক নয়।")

    if not verify_user_password(user["password_hash"], password):
        raise AuthError("ইমেইল অথবা পাসওয়ার্ডটি সঠিক নয়।")

    return user


def generate_api_key(tenant_id: int, name: str = "Default") -> str:
    """Generate a raw API key, store its SHA-256 hash, and return raw key."""
    # Prefix: cc_live_ (first 8 chars) followed by 32 random hex characters
    raw_key = "cc_live_" + secrets.token_hex(16)
    key_hash = hash_api_key(raw_key)
    key_prefix = raw_key[:12]  # cc_live_ + first 4 characters

    db.execute(
        """
        INSERT INTO api_keys (tenant_id, name, key_hash, key_prefix)
        VALUES (%s, %s, %s, %s)
        """,
        (tenant_id, name, key_hash, key_prefix)
    )

    return raw_key


def hash_api_key(raw_key: str) -> str:
    """Returns SHA-256 hash hex string of the raw API key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def send_verification_email(user: dict[str, Any]) -> None:
    """Generate token, save to db, and dispatch verification email."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
    
    # Save token
    create_verification_token(user["id"], token, expires_at)
    
    verify_url = url_for("auth.verify_email_route", token=token, _external=True)
    logger.info(f"EMAIL VERIFICATION LINK for {user['email']}: {verify_url}")
    print(f"\n--- EMAIL VERIFICATION LINK FOR {user['email']} ---\n{verify_url}\n------------------------------------------\n")

    msg = Message(
        subject="C-Call Account Verification",
        recipients=[user["email"]],
        body=f"C-Call IVR-এ নিবন্ধনের জন্য ধন্যবাদ। আপনার অ্যাকাউন্ট ভেরিফাই করতে নিচের লিঙ্কে ক্লিক করুন:\n{verify_url}"
    )
    mail.send(msg)


def send_reset_email(user: dict[str, Any]) -> None:
    """Generate token, save to db, and dispatch password reset email."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    
    # Save token
    create_verification_token(user["id"], token, expires_at)
    
    reset_url = url_for("auth.reset_password", token=token, _external=True)
    logger.info(f"PASSWORD RESET LINK for {user['email']}: {reset_url}")
    print(f"\n--- PASSWORD RESET LINK FOR {user['email']} ---\n{reset_url}\n------------------------------------------\n")

    msg = Message(
        subject="C-Call Password Reset Request",
        recipients=[user["email"]],
        body=f"আপনার পাসওয়ার্ড রিসেট করার জন্য অনুরোধ পাওয়া গেছে। নিচের লিঙ্কে ক্লিক করে পাসওয়ার্ড রিসেট করুন:\n{reset_url}\n(এই লিঙ্কটির মেয়াদ ১ ঘণ্টা।)"
    )
    mail.send(msg)
