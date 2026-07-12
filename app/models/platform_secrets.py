"""Platform TTS secret storage.

Platform secrets are encrypted with PLATFORM_SECRET_ENCRYPTION_KEY (Fernet).
This key MUST be distinct from SIP_PASSWORD_ENCRYPTION_KEY.

Rules:
- encrypted_value is NEVER returned to any route, template, log, or API response.
- Full plaintext is only available through get_platform_secret_for_generation(),
  which is called exclusively inside tts_service / tts_entitlement_service.
- All public-facing functions return only metadata (last_four, status, updated_at).
- repr() / str() of this module's objects never includes the secret value.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from app.db import db

logger = logging.getLogger("ccall")

ALLOWED_PROVIDERS: frozenset[str] = frozenset({"gemini", "elevenlabs"})


# ── Encryption helpers ────────────────────────────────────────────────────────

def _get_fernet():
    """Return a Fernet instance keyed by PLATFORM_SECRET_ENCRYPTION_KEY.

    Raises RuntimeError if the key is absent or malformed — never silently
    degrades to plaintext storage.
    """
    key = os.environ.get("PLATFORM_SECRET_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "PLATFORM_SECRET_ENCRYPTION_KEY is not set. "
            "Platform secret operations are disabled."
        )
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:
        raise RuntimeError(
            f"PLATFORM_SECRET_ENCRYPTION_KEY is invalid: {exc}"
        ) from exc


def _encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def _decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


# ── Write operations ─────────────────────────────────────────────────────────

def save_platform_secret(
    provider: str,
    plaintext: str,
    updated_by_user_id: int | None = None,
) -> dict[str, Any]:
    """Encrypt and upsert a platform secret. Returns safe metadata only.

    Raises ValueError for unknown providers.
    Raises RuntimeError if encryption key is absent.
    """
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(f"Unknown platform secret provider: {provider!r}")
    if not plaintext or not plaintext.strip():
        raise ValueError("Platform secret value must not be empty.")

    plaintext = plaintext.strip()
    encrypted = _encrypt(plaintext)
    last_four = plaintext[-4:] if len(plaintext) >= 4 else "****"

    db.execute(
        """
        INSERT INTO platform_secrets
            (provider, encrypted_value, last_four, status, updated_by, updated_at)
        VALUES (%s, %s, %s, 'active', %s, now())
        ON CONFLICT (provider) DO UPDATE SET
            encrypted_value  = EXCLUDED.encrypted_value,
            last_four        = EXCLUDED.last_four,
            status           = 'active',
            updated_by       = EXCLUDED.updated_by,
            updated_at       = now(),
            last_test_status = NULL,
            last_tested_at   = NULL
        """,
        (provider, encrypted, last_four, updated_by_user_id),
    )
    logger.info(
        "Platform secret saved: provider=%s last_four=****%s updated_by=%s",
        provider, last_four, updated_by_user_id,
    )
    return get_platform_secret_meta(provider)


def disable_platform_secret(provider: str, updated_by_user_id: int | None = None) -> None:
    """Mark a platform secret as disabled so it cannot be used for generation."""
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(f"Unknown platform secret provider: {provider!r}")
    db.execute(
        """
        UPDATE platform_secrets
        SET status = 'disabled', updated_by = %s, updated_at = now()
        WHERE provider = %s
        """,
        (updated_by_user_id, provider),
    )
    logger.info("Platform secret disabled: provider=%s by user=%s", provider, updated_by_user_id)


def update_test_status(provider: str, success: bool) -> None:
    """Record the result of a liveness test for the platform secret."""
    db.execute(
        """
        UPDATE platform_secrets
        SET last_test_status = %s, last_tested_at = now()
        WHERE provider = %s
        """,
        ("ok" if success else "failed", provider),
    )
    if not success:
        db.execute(
            "UPDATE platform_secrets SET status = 'invalid' WHERE provider = %s AND last_test_status = 'failed'",
            (provider,),
        )


# ── Read operations ───────────────────────────────────────────────────────────

def get_platform_secret_meta(provider: str) -> dict[str, Any] | None:
    """Return display-safe metadata. Never includes encrypted_value or plaintext."""
    row = db.query_one(
        """
        SELECT id, provider, last_four, status,
               last_tested_at, last_test_status,
               created_at, updated_at
        FROM platform_secrets WHERE provider = %s
        """,
        (provider,),
    )
    return dict(row) if row else None


def list_platform_secret_meta() -> list[dict[str, Any]]:
    """Return display-safe metadata for all platform secrets."""
    rows = db.query(
        """
        SELECT id, provider, last_four, status,
               last_tested_at, last_test_status,
               created_at, updated_at
        FROM platform_secrets ORDER BY provider
        """
    )
    return [dict(r) for r in rows]


def get_platform_secret_for_generation(provider: str) -> str | None:
    """Return decrypted platform secret for TTS generation ONLY.

    SECURITY CONTRACT:
    - Caller MUST NOT log, serialize, or return this value.
    - Caller MUST NOT include this value in any exception message.
    - This function is ONLY called from tts_entitlement_service.resolve_internal_tts_credential().

    Returns None if secret is absent, disabled, or invalid.
    """
    if provider not in ALLOWED_PROVIDERS:
        return None

    row = db.query_one(
        "SELECT encrypted_value, status FROM platform_secrets WHERE provider = %s",
        (provider,),
    )
    if not row:
        logger.debug("Platform secret not found for provider=%s", provider)
        return None
    if row["status"] != "active":
        logger.warning(
            "Platform secret for provider=%s has status=%s — refusing generation",
            provider, row["status"],
        )
        return None

    try:
        return _decrypt(row["encrypted_value"])
    except Exception as exc:
        logger.error(
            "Failed to decrypt platform secret for provider=%s: %s",
            provider, type(exc).__name__,  # type only — no value in log
        )
        return None


def platform_secret_is_active(provider: str) -> bool:
    """Return True only if an active platform secret exists for the provider."""
    row = db.query_one(
        "SELECT status FROM platform_secrets WHERE provider = %s",
        (provider,),
    )
    return bool(row and row["status"] == "active")
