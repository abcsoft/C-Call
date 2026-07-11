# app/blueprints/auth/routes.py
"""Authentication blueprints and views."""
from __future__ import annotations

import datetime
import logging
import secrets
from flask import abort, current_app, flash, redirect, render_template, request, url_for, session as flask_session
from flask_login import current_user, login_required, login_user, logout_user

from app.blueprints.auth import auth_bp
from app.blueprints.auth.forms import ForgotPasswordForm, LoginForm, RegistrationForm, ResetPasswordForm
from app.db import db
from app.extensions import limiter, login_manager, redis_ext
from app.models.tenant import get_tenant_by_id
from app.models.user import (
    User,
    create_verification_token,
    get_user_by_email,
    get_user_by_id,
    get_verification_token,
    verify_user_password,
)
from app.services.auth_service import (
    register_tenant,
    verify_email,
    send_reset_email,
)
import bcrypt

logger = logging.getLogger("ccall")


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    """Load user by ID for Flask-Login."""
    user_dict = get_user_by_id(int(user_id))
    return User(user_dict) if user_dict else None


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("5/minute")
def register():
    """Register a new merchant business tenant."""
    if current_user.is_authenticated:
        if current_user.role == "super_admin":
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("customer.dashboard"))

    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            # Register via service layer
            tenant, user = register_tenant({
                "business_name": form.business_name.data,
                "email": form.email.data,
                "password": form.password.data,
                "phone": form.phone.data
            })
            flash("Registration successful! Please check your email (or terminal logs in dev) to verify your account.", "success")
            return redirect(url_for("auth.login"))
        except Exception as exc:
            logger.error(f"Error during registration: {exc}")
            flash(f"নিবন্ধনের সময় একটি ত্রুটি ঘটেছে: {exc}", "error")

    return render_template("auth/register.html", form=form)


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute")
def login():
    """Sign in an existing user."""
    if current_user.is_authenticated:
        if current_user.role == "super_admin":
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("customer.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data
        password = form.password.data
        redis_client = redis_ext.client
        redis_key = f"login_fail:{email}"

        # 1. Brute Force Protection check
        fail_count = redis_client.get(redis_key)
        if fail_count and int(fail_count) >= 5:
            logger.warning(f"Login attempt blocked (brute force lock) for {email}")
            abort(403, description="Account locked 15 minutes")

        # 2. Get user by email
        user_dict = get_user_by_email(email)
        
        # 3. Verify Password
        if user_dict and verify_user_password(user_dict["password_hash"], password):
            user = User(user_dict)

            # Check email verification status
            if not user.email_verified:
                flash("লগইন করতে আপনার অ্যাকাউন্টটি প্রথমে ভেরিফাই করুন।", "error")
                return render_template("auth/login.html", form=form)

            # Check tenant status (exempt super_admin)
            if user.role != "super_admin":
                tenant = get_tenant_by_id(user.tenant_id)
                if not tenant or tenant["status"] not in ("active", "trial"):
                    flash("আপনার অ্যাকাউন্টটি স্থগিত বা নিষ্ক্রিয় করা হয়েছে।", "error")
                    return render_template("auth/login.html", form=form)

            # Clear brute force counter on success
            redis_client.delete(redis_key)

            # Login user
            login_user(user, remember=form.remember.data)

            # Record session in DB
            sid = getattr(flask_session, "sid", None) or secrets.token_hex(16)
            db.execute(
                """
                INSERT INTO sessions (id, user_id, tenant_id, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET expires_at = EXCLUDED.expires_at
                """,
                (
                    sid,
                    user.id,
                    user.tenant_id,
                    datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
                )
            )

            # Update last login time
            db.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user.id,))

            flash("Welcome back!", "success")
            next_page = request.args.get("next")
            if next_page:
                from urllib.parse import urlparse
                parsed = urlparse(next_page)
                # Only allow relative internal URLs (no scheme/netloc = internal)
                if not parsed.scheme and not parsed.netloc:
                    return redirect(next_page)

            if user.role == "super_admin":
                return redirect(url_for("admin.dashboard"))
            return redirect(url_for("customer.dashboard"))
        else:
            # Increment failure counter and set TTL to 15 mins (900 seconds)
            redis_client.incr(redis_key)
            redis_client.expire(redis_key, 900)
            flash("ইমেইল অথবা পাসওয়ার্ডটি সঠিক নয়।", "error")

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    """Logout the current user and destroy session logs."""
    sid = getattr(flask_session, "sid", None)
    if sid:
        db.execute("DELETE FROM sessions WHERE id = %s", (sid,))

    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/verify/<token>", methods=["GET"])
def verify_email_route(token: str):
    """Verify user's email address and activate their tenant."""
    try:
        verify_email(token)
        flash("Your account has been successfully verified! You can now log in.", "success")
    except ValueError as val_err:
        flash(str(val_err), "error")
    
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Forgot password request view."""
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user_dict = get_user_by_email(form.email.data)
        if user_dict:
            try:
                send_reset_email(user_dict)
            except Exception as mail_err:
                logger.error(f"Failed to send password reset email to {form.email.data}: {mail_err}")
        
        # Always flash security-safe response to prevent account harvesting
        flash("If that email is registered, we have sent a password reset link to it.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", form=form)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    """Reset password view using verification token."""
    token_row = get_verification_token(token)
    if not token_row or token_row.get("used_at"):
        flash("পাসওয়ার্ড রিসেট লিঙ্কটি সঠিক নয় অথবা ইতিমধ্যেই ব্যবহৃত হয়েছে।", "error")
        return redirect(url_for("auth.login"))

    # Expiry validation
    expires_at = token_row["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    elif expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)

    now = datetime.datetime.now(datetime.timezone.utc)
    if expires_at < now:
        flash("পাসওয়ার্ড রিসেট লিঙ্কটির মেয়াদ শেষ হয়ে গেছে।", "error")
        return redirect(url_for("auth.login"))

    form = ResetPasswordForm()
    if form.validate_on_submit():
        user_id = token_row["user_id"]

        # Hash new password using bcrypt cost 12
        salt = bcrypt.gensalt(rounds=12)
        password_hash = bcrypt.hashpw(form.password.data.encode("utf-8"), salt).decode("utf-8")

        try:
            with db.transaction() as conn:
                # Update password
                conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, user_id))
                # Mark token used
                conn.execute("UPDATE email_verifications SET used_at = now() WHERE token = %s", (token,))
                # Invalidate all active sessions for the user
                conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))

            flash("Your password has been reset successfully! You can now log in.", "success")
            return redirect(url_for("auth.login"))
        except Exception as exc:
            logger.error(f"Error resetting password for user {user_id}: {exc}")
            flash("পাসওয়ার্ড রিসেট করার সময় ত্রুটি ঘটেছে।", "error")

    return render_template("auth/reset_password.html", form=form)


@auth_bp.route("/check-email", methods=["POST"])
def check_email():
    """Real-time HTMX endpoint to check email availability."""
    email = request.form.get("email", "").strip()
    if not email:
        return ""
    
    # Basic validation
    if "@" not in email or "." not in email:
        return '<p class="text-rose-400 text-xs mt-1.5 font-medium">সঠিক ইমেইল এড্রেস লিখুন।</p>'
        
    from app.models.user import get_user_by_email
    user_dict = get_user_by_email(email)
    if user_dict:
        return '<p class="text-rose-400 text-xs mt-1.5 font-medium">এই ইমেইল এড্রেসটি ইতিমধ্যেই নিবন্ধিত।</p>'
    
    return '<p class="text-emerald-400 text-xs mt-1.5 font-medium">ইমেইল এড্রেসটি খালি আছে।</p>'


@auth_bp.route("/accept-invite/<token>", methods=["GET", "POST"])
def accept_invite(token: str):
    """Accept team invitation and set password if new user."""
    invitation = db.query_one(
        "SELECT * FROM team_invitations WHERE token = %s AND used_at IS NULL AND expires_at > now()",
        (token,)
    )
    if not invitation:
        flash("আমন্ত্রণ লিঙ্কটি সঠিক নয় অথবা এটির মেয়াদ শেষ হয়ে গেছে।", "error")
        return redirect(url_for("auth.login"))

    tenant = db.query_one("SELECT business_name FROM tenants WHERE id = %s", (invitation["tenant_id"],))
    tenant_name = tenant["business_name"] if tenant else "Merchant"
    email = invitation["email"]

    # Check if user already exists
    user_exists = get_user_by_email(email) is not None

    if request.method == "POST":
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        if not user_exists:
            if not password or len(password) < 8:
                flash("পাসওয়ার্ড কমপক্ষে ৮ অক্ষরের হতে হবে।", "error")
                return render_template("auth/accept_invite.html", token=token, email=email, tenant_name=tenant_name, user_exists=user_exists)
            if password != confirm_password:
                flash("পাসওয়ার্ড দুটি মিলছে না।", "error")
                return render_template("auth/accept_invite.html", token=token, email=email, tenant_name=tenant_name, user_exists=user_exists)
        else:
            # For existing user, password can be optional if they don't want to change it
            if password:
                if len(password) < 8:
                    flash("পাসওয়ার্ড কমপক্ষে ৮ অক্ষরের হতে হবে।", "error")
                    return render_template("auth/accept_invite.html", token=token, email=email, tenant_name=tenant_name, user_exists=user_exists)
                if password != confirm_password:
                    flash("পাসওয়ার্ড দুটি মিলছে না।", "error")
                    return render_template("auth/accept_invite.html", token=token, email=email, tenant_name=tenant_name, user_exists=user_exists)

        try:
            from app.services.team_service import accept_invitation
            user = accept_invitation(token, password if password else None)
            
            # Log the user in
            login_user(user)
            
            # Update last login time
            db.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user.id,))
            
            # Record session in DB
            sid = getattr(flask_session, "sid", None) or secrets.token_hex(16)
            db.execute(
                """
                INSERT INTO sessions (id, user_id, tenant_id, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET expires_at = EXCLUDED.expires_at
                """,
                (
                    sid,
                    user.id,
                    user.tenant_id,
                    datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
                )
            )
            
            flash("Welcome to the team!", "success")
            return redirect(url_for("customer.dashboard"))
        except Exception as exc:
            logger.error(f"Error accepting invitation: {exc}", exc_info=True)
            flash("আমন্ত্রণ গ্রহণ করার সময় ত্রুটি ঘটেছে।", "error")

    return render_template("auth/accept_invite.html", token=token, email=email, tenant_name=tenant_name, user_exists=user_exists)


