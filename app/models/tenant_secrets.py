"""Encrypted per-tenant secrets (API keys etc.).

Storage contract:
- Values are Fernet-encrypted before INSERT/UPDATE.
- Full plaintext is never returned to templates or logs.
- Only last_four is available for display.
- Every read/write operation requires an explicit tenant_id.
"""
from __future__ import annotations

import logging
from typing import Any

from app.db import db
from app.models.sip_provider import _get_fernet  # reuse project Fernet helper

logger = logging.getLogger("ccall")

ALLOWED_SECRET_TYPES = frozenset({"gemini_api_key", "elevenlabs_api_key"})


def _encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def _decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def save_secret(tenant_id: int, secret_type: str, plaintext: str) -> None:
    """Encrypt and upsert a secret for tenant_id. Raises ValueError for unknown types."""
    if secret_type not in ALLOWED_SECRET_TYPES:
        raise ValueError(f"Unknown secret_type: {secret_type}")
    encrypted = _encrypt(plaintext)
    last_four = plaintext[-4:] if len(plaintext) >= 4 else "****"
    db.execute(
        """
        INSERT INTO tenant_secrets (tenant_id, secret_type, encrypted_value, last_four, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (tenant_id, secret_type)
        DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value,
                      last_four       = EXCLUDED.last_four,
                      updated_at      = now()
        """,
        (tenant_id, secret_type, encrypted, last_four),
    )
    logger.info("Saved secret type=%s for tenant %s (last_four=%s)", secret_type, tenant_id, last_four)


def get_secret_plaintext(tenant_id: int, secret_type: str) -> str | None:
    """Return decrypted secret for this tenant only. Returns None if not set."""
    row = db.query_one(
        "SELECT encrypted_value FROM tenant_secrets WHERE tenant_id = %s AND secret_type = %s",
        (tenant_id, secret_type),
    )
    if not row:
        return None
    try:
        return _decrypt(row["encrypted_value"])
    except Exception as exc:
        logger.error("Failed to decrypt secret type=%s tenant %s: %s", secret_type, tenant_id, exc)
        return None


def get_secret_meta(tenant_id: int, secret_type: str) -> dict[str, Any] | None:
    """Return display-safe metadata (last_four, updated_at). Never returns plaintext."""
    return db.query_one(
        "SELECT secret_type, last_four, updated_at FROM tenant_secrets WHERE tenant_id = %s AND secret_type = %s",
        (tenant_id, secret_type),
    )


def delete_secret(tenant_id: int, secret_type: str) -> None:
    """Remove a secret for this tenant only."""
    db.execute(
        "DELETE FROM tenant_secrets WHERE tenant_id = %s AND secret_type = %s",
        (tenant_id, secret_type),
    )
    logger.info("Deleted secret type=%s for tenant %s", secret_type, tenant_id)


def list_secret_meta(tenant_id: int) -> list[dict[str, Any]]:
    """Return display-safe metadata for all secrets of this tenant."""
    return db.query(
        "SELECT secret_type, last_four, updated_at FROM tenant_secrets WHERE tenant_id = %s ORDER BY secret_type",
        (tenant_id,),
    )
