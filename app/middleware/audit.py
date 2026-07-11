# E:\c-call-ivr\app\middleware\audit.py
"""Generic audit trail — call this for every sensitive admin action:
impersonation start/end, wallet adjustment, tenant suspend, API key
generate/revoke.

Writes into the same `audit_log` table app/services/admin_service.py's
impersonation logging uses (extended by migration 005 with
actor_user_id/target_type/target_id/ip_address/details so both call
sites query consistently).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from flask import has_request_context, request

from app.db import db

logger = logging.getLogger("ccall")


def audit_log(
    action: str,
    actor_id: int,
    target_type: str,
    target_id: int | None,
    details: dict[str, Any] | None = None,
) -> None:
    """Insert one audit_log row. Never raises — an audit-write failure
    must not block the action it's auditing; it's logged instead."""
    ip_address = request.remote_addr if has_request_context() else None
    target_tenant_id = target_id if target_type == "tenant" else None

    try:
        db.execute(
            """
            INSERT INTO audit_log
                (actor_user_id, admin_user_id, action, target_type, target_id,
                 target_tenant_id, ip_address, details, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                actor_id, actor_id, action, target_type, target_id,
                target_tenant_id, ip_address,
                json.dumps(details or {}), json.dumps(details or {}),
            ),
        )
    except Exception:
        logger.warning(
            "Failed to write audit_log row (action=%s actor=%s target=%s:%s)",
            action, actor_id, target_type, target_id, exc_info=True,
        )
