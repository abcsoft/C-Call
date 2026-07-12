"""Plan TTS policy model.

Each plan row in plan_tts_policies defines:
- Which credential modes are allowed (byok_only / platform_managed / hybrid)
- Which providers tenants may access via BYOK
- Which providers the platform covers
- Monthly quota and daily preview limits

Missing policy: treated as byok_only with free providers only — NEVER as platform access.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.db import db

logger = logging.getLogger("ccall")

# Canonical set of valid provider IDs the application supports.
VALID_PROVIDERS: frozenset[str] = frozenset({"edge", "gtts", "gemini", "elevenlabs", "openvoice"})

# Providers that require an API key (BYOK or platform).
KEY_REQUIRED_PROVIDERS: frozenset[str] = frozenset({"gemini", "elevenlabs"})

# Default safe policy returned when no DB row exists.
_SAFE_DEFAULT: dict[str, Any] = {
    "id": None,
    "plan_id": None,
    "credential_policy": "byok_only",
    "allowed_providers": ["edge", "gtts"],
    "byok_providers": ["edge", "gtts"],
    "platform_managed_providers": [],
    "monthly_character_quota": None,
    "monthly_audio_seconds_quota": None,
    "daily_preview_limit": 10,
    "extra_usage_policy": "block",
    "max_concurrent_tts": 1,
    "premium_voices_enabled": False,
}


def _parse_jsonb_list(value: Any, field_name: str) -> list[str]:
    """Parse a JSONB field that should be a list of provider strings."""
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        try:
            raw = json.loads(value)
        except (ValueError, TypeError):
            logger.warning("plan_tts_policies.%s is not valid JSON: %r", field_name, value)
            return []
    else:
        return []

    result = []
    for item in raw:
        if isinstance(item, str) and item in VALID_PROVIDERS:
            result.append(item)
        else:
            logger.warning("plan_tts_policies.%s contains unknown provider: %r", field_name, item)
    return result


def _row_to_policy(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw DB row to a validated policy dict."""
    policy: dict[str, Any] = {
        "id": row.get("id"),
        "plan_id": row.get("plan_id"),
        "credential_policy": row.get("credential_policy", "byok_only"),
        "allowed_providers": _parse_jsonb_list(row.get("allowed_providers"), "allowed_providers"),
        "byok_providers": _parse_jsonb_list(row.get("byok_providers"), "byok_providers"),
        "platform_managed_providers": _parse_jsonb_list(
            row.get("platform_managed_providers"), "platform_managed_providers"
        ),
        "monthly_character_quota": row.get("monthly_character_quota"),
        "monthly_audio_seconds_quota": row.get("monthly_audio_seconds_quota"),
        "daily_preview_limit": int(row.get("daily_preview_limit") or 10),
        "extra_usage_policy": row.get("extra_usage_policy", "block"),
        "max_concurrent_tts": int(row.get("max_concurrent_tts") or 1),
        "premium_voices_enabled": bool(row.get("premium_voices_enabled", False)),
    }

    # Hard safety: policy MUST NOT grant platform access unless credential_policy allows it.
    if policy["credential_policy"] == "byok_only" and policy["platform_managed_providers"]:
        logger.error(
            "plan_tts_policies id=%s has byok_only credential_policy "
            "but non-empty platform_managed_providers — forcing empty.",
            policy["id"],
        )
        policy["platform_managed_providers"] = []

    return policy


def get_plan_tts_policy(plan_id: int | None) -> dict[str, Any]:
    """Return TTS policy for a plan. Returns safe BYOK-only default if none exists.

    Never treats a missing row as granting platform access.
    """
    if plan_id is None:
        logger.debug("get_plan_tts_policy: plan_id is None — returning safe default")
        return dict(_SAFE_DEFAULT)

    row = db.query_one(
        "SELECT * FROM plan_tts_policies WHERE plan_id = %s",
        (plan_id,),
    )
    if not row:
        logger.warning(
            "No plan_tts_policies row for plan_id=%s — returning conservative default", plan_id
        )
        default = dict(_SAFE_DEFAULT)
        default["plan_id"] = plan_id
        return default

    return _row_to_policy(row)


def provider_allowed_for_plan(plan_id: int | None, provider: str) -> bool:
    """Quick check: is provider in allowed_providers for the plan?"""
    policy = get_plan_tts_policy(plan_id)
    return provider in policy["allowed_providers"]


def mode_allowed_for_plan(plan_id: int | None, mode: str) -> bool:
    """Check that credential mode is allowed by plan policy."""
    policy = get_plan_tts_policy(plan_id)
    cp = policy["credential_policy"]
    if mode == "byok":
        return cp in ("byok_only", "hybrid")
    if mode == "platform_managed":
        return cp in ("platform_managed", "hybrid")
    return False
