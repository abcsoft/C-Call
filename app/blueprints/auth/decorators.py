# app/blueprints/auth/decorators.py
"""Decorators for session authentication and REST API key validations."""
from __future__ import annotations

from functools import wraps
from flask import g, abort, redirect, url_for, request, jsonify
from flask_login import current_user


def require_login(f):
    """Ensure the user is authenticated via Flask-Login, else redirects to login page."""
    @wraps(f)
    def decorated_view(*args, **kwargs):
        if not current_user or not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.url))
        return f(*args, **kwargs)
    return decorated_view


def require_tenant_admin(f):
    """Ensure the user is an admin of their tenant business account."""
    @wraps(f)
    def decorated_view(*args, **kwargs):
        if not current_user or not current_user.is_authenticated or current_user.role not in ("admin",):
            abort(403)
        return f(*args, **kwargs)
    return decorated_view


def require_super_admin(f):
    """Ensure the user is a platform super-admin (no tenant_id associated)."""
    @wraps(f)
    def decorated_view(*args, **kwargs):
        if (
            not current_user 
            or not current_user.is_authenticated 
            or current_user.role != "super_admin" 
            or current_user.tenant_id is not None
        ):
            abort(403)
        return f(*args, **kwargs)
    return decorated_view


def require_role(roles: list[str]):
    """Ensure the user possesses one of the specified roles."""
    def decorator(f):
        @wraps(f)
        def decorated_view(*args, **kwargs):
            if not current_user or not current_user.is_authenticated or current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_view
    return decorator


def require_api_key(f):
    """Authenticate incoming REST API request using X-API-Key header.

    The SHA-256 hash lookup + tenant-status check happen exactly once per
    request, in app/middleware/tenant_context.py's before_request hook
    (runs before any view function, including this decorator). This just
    reads the result — no second database query per request.
    """
    import logging
    logger = logging.getLogger("ccall")

    @wraps(f)
    def decorated_view(*args, **kwargs):
        api_key = request.headers.get("X-API-Key")
        masked_key = api_key[:4] + "..." + api_key[-4:] if api_key else "None"
        if not api_key:
            logger.warning(f"[SAAS STEP 2] API key check failed: API key is missing. Key: {masked_key}")
            return jsonify({
                "success": False,
                "error": "API key is missing in X-API-Key header",
                "code": "API_KEY_MISSING"
            }), 401

        if g.tenant_id is None:
            err_reason = getattr(g, "api_key_error", "Invalid Key")
            logger.warning(f"[SAAS STEP 2] API key check failed: Tenant not matched or inactive. Key: {masked_key}, Error: {err_reason}")
            if err_reason == "TENANT_INACTIVE":
                return jsonify({
                    "success": False,
                    "error": "Tenant account is suspended or inactive",
                    "code": "TENANT_INACTIVE"
                }), 403
            return jsonify({
                "success": False,
                "error": "Invalid or inactive API key",
                "code": "UNAUTHORIZED"
            }), 401

        logger.info(f"[SAAS STEP 2] API key check passed: Tenant matched to ID {g.tenant_id}. Key: {masked_key}")
        return f(*args, **kwargs)
    return decorated_view
