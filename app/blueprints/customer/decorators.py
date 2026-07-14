# app/blueprints/customer/decorators.py
"""Access decorators for customer-facing routes and views."""
from __future__ import annotations

from functools import wraps
from flask import g, abort, redirect, url_for, flash
from flask_login import current_user
from app.models.tenant import get_tenant_by_id


def require_tenant_login(f):
    """Decorator to ensure the user is logged in, their email is verified, and their tenant is active."""
    @wraps(f)
    def decorated_view(*args, **kwargs):
        if not current_user or not current_user.is_authenticated:
            return redirect(url_for("auth.login"))
        
        # Check email verification status
        if not current_user.email_verified:
            flash("Please verify your email address to access this page.", "error")
            return redirect(url_for("auth.login"))

        # Check tenant account status (exempt super admin)
        if current_user.role != "super_admin":
            if current_user.tenant_id:
                tenant = get_tenant_by_id(current_user.tenant_id)
                if not tenant:
                    flash("No business account found.", "error")
                    return redirect(url_for("auth.login"))
                status = tenant["status"]
                # Hard block: suspended and cancelled cannot access the panel
                if status in ("suspended", "cancelled"):
                    flash("Your account has been suspended or cancelled. Please contact support.", "error")
                    return redirect(url_for("auth.login"))
                # grace_period, past_due, expired: allowed in (banner shown by dashboard)
                # active, trial: fully allowed
            else:
                flash("No business account associated with your user profile.", "error")
                return redirect(url_for("auth.login"))

        return f(*args, **kwargs)
    return decorated_view


def require_role(roles: list[str]):
    """Decorator to restrict access to specific user roles (e.g., admin, agent)."""
    def decorator(f):
        @wraps(f)
        def decorated_view(*args, **kwargs):
            user_role = getattr(g, "user_role", None)
            if not user_role and current_user and current_user.is_authenticated:
                user_role = current_user.role
            
            if user_role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_view
    return decorator
