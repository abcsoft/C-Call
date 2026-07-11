from __future__ import annotations

import logging
import re
import secrets
import time

from flask import Flask, g, request

logger = logging.getLogger("ccall")

_PHONE_RE = re.compile(r"(?:\+?88)?01[3-9]\d{8}")


def mask_phone(phone: str) -> str:
    """Mask sensitive phone numbers in log messages to prevent PII leakage.
    Example: 01711234567 -> 017****4567
    """
    if not phone:
        return ""
    phone = str(phone).strip()
    if len(phone) >= 7:
        return phone[:3] + "****" + phone[-4:]
    return "***"


def mask_sensitive(text: str) -> str:
    """Mask phone numbers in a free-text log line. Defense-in-depth for
    log statements that interpolate request data — never log raw PII."""
    if not text:
        return text
    return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), text)


def register_security_headers(app: Flask) -> None:
    """Security headers + request timing log on every response.

    CSP note: script-src allows the CDN domains the UI actually loads
    (Alpine.js, HTMX, Chart.js — none of this is self-hosted) plus a
    per-request nonce for inline <script> blocks. 'unsafe-eval' is kept
    because Alpine.js's expression evaluation (x-data, x-show, etc.)
    requires it in its standard (non-CSP) build — dropping it would break
    every Alpine-driven page across admin/ and customer/. Switching to
    Alpine's dedicated CSP build is a real follow-up, not done here to
    avoid silently breaking the UI as a side effect of a hardening task.
    """

    @app.before_request
    def _start_timer():
        g._request_start = time.monotonic()
        g._csp_nonce = secrets.token_urlsafe(16)

    @app.context_processor
    def _inject_csp_nonce():
        return {"csp_nonce": getattr(g, "_csp_nonce", "")}

    @app.after_request
    def _add_security_headers(response):
        nonce = getattr(g, "_csp_nonce", "")
        csp_policies = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' 'unsafe-eval' "
            "https://cdn.jsdelivr.net https://unpkg.com https://cdn.tailwindcss.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        response.headers["Content-Security-Policy"] = csp_policies
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "microphone=(), camera=(), geolocation=()"

        _log_request(response)
        return response


def _log_request(response) -> None:
    """method, path, tenant_id, duration_ms, status_code — never the
    request body (may hold phone numbers, passwords, raw API keys)."""
    start = getattr(g, "_request_start", None)
    duration_ms = int((time.monotonic() - start) * 1000) if start else -1
    tenant_id = getattr(g, "tenant_id", None)
    logger.info(
        "%s %s tenant_id=%s status=%s duration_ms=%s",
        request.method, request.path, tenant_id, response.status_code, duration_ms,
    )
