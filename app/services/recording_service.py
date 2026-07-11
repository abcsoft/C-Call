# app/services/recording_service.py
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import urllib.parse
from flask import current_app
from app.db import db


def get_secret() -> str:
    """Return the app secret key for signing recording URLs.
    Reads Flask's SECRET_KEY (sourced from FLASK_SECRET_KEY in .env via config.py).
    Falls back to the env var directly in case this is called outside app context.
    Never falls back to a hardcoded placeholder — raises instead so misconfiguration
    is loud rather than silently producing forgeable signatures.
    """
    key = current_app.config.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY", "")
    if not key or key == "change-me-to-a-long-random-secret-key" or len(key) < 32:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not configured or too weak — cannot sign recording URLs safely."
        )
    return key


def get_signed_url(recording_path: str, tenant_id: int, expires_minutes: int = 60) -> str:
    """Verify recording belongs to tenant (check call.tenant_id),
    then generate signed URL: HMAC-SHA256(path + expiry + secret) -> base64.
    """
    # 1. Verify recording belongs to tenant (check call.tenant_id)
    call = db.query_one("SELECT tenant_id FROM calls WHERE recording_path = %s", (recording_path,))
    if not call:
        raise PermissionError("Access Denied: Recording call entry not found.")
    if call["tenant_id"] != tenant_id:
        raise PermissionError("Access Denied: Recording does not belong to this tenant.")

    # 2. Expiry timestamp
    expiry = int(time.time()) + (expires_minutes * 60)
    secret = get_secret()

    # 3. Generate HMAC-SHA256 signature
    message = f"{recording_path}{expiry}".encode("utf-8")
    sig_bytes = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(sig_bytes).decode("utf-8").strip("=")

    encoded_path = urllib.parse.quote(recording_path)
    return f"/recordings/play?path={encoded_path}&sig={sig}&exp={expiry}"


def verify_signed_url(path: str, sig: str, exp: str | int) -> bool:
    """Verify signed URL: Check exp > now() and verify HMAC signature."""
    try:
        exp_time = int(exp)
    except (ValueError, TypeError):
        return False

    if exp_time < time.time():
        return False

    secret = get_secret()
    message = f"{path}{exp_time}".encode("utf-8")
    expected_bytes = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    expected_sig = base64.urlsafe_b64encode(expected_bytes).decode("utf-8").strip("=")

    return hmac.compare_digest(sig, expected_sig)
