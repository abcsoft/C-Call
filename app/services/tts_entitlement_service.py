"""Central TTS entitlement resolver.

Single source of truth for "is this tenant allowed to use this TTS provider
in this credential mode right now?"

Rules:
- No silent provider fallback.
- No silent credential mode fallback.
- Plan policy is always consulted — never bypassed.
- Full API key/secret is NEVER included in the returned dataclass.
- resolve_internal_tts_credential() is the only function that returns a
  decrypted key, and only to TTS generation code.

Usage:
    entitlement = resolve_tts_entitlement(tenant_id, "gemini", "production")
    if not entitlement.allowed:
        raise SomeError(entitlement.denial_message)
    key = resolve_internal_tts_credential(entitlement)
    audio = create_tts_audio(..., gemini_api_key=key)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("ccall")

# ── Stable denial codes ───────────────────────────────────────────────────────
PLAN_TTS_NOT_CONFIGURED         = "PLAN_TTS_NOT_CONFIGURED"
PROVIDER_NOT_ALLOWED            = "PROVIDER_NOT_ALLOWED"
CREDENTIAL_MODE_NOT_ALLOWED     = "CREDENTIAL_MODE_NOT_ALLOWED"
TENANT_CREDENTIAL_MISSING       = "TENANT_CREDENTIAL_MISSING"
PLATFORM_CREDENTIAL_UNAVAILABLE = "PLATFORM_CREDENTIAL_UNAVAILABLE"
MONTHLY_TTS_QUOTA_EXCEEDED      = "MONTHLY_TTS_QUOTA_EXCEEDED"
DAILY_PREVIEW_LIMIT_EXCEEDED    = "DAILY_PREVIEW_LIMIT_EXCEEDED"
TENANT_SUSPENDED                = "TENANT_SUSPENDED"
TENANT_NOT_FOUND                = "TENANT_NOT_FOUND"
PROVIDER_INVALID                = "PROVIDER_INVALID"


@dataclass
class TtsEntitlement:
    """Result of resolve_tts_entitlement().

    SECURITY: Never add a 'credential', 'api_key', or 'secret' field here.
    Use resolve_internal_tts_credential(entitlement) separately.
    """
    tenant_id: int
    plan_id: int | None
    provider: str
    credential_mode: str           # byok | platform_managed
    allowed: bool
    denial_code: str | None = None
    denial_message: str | None = None
    secret_source: str | None = None   # tenant_secrets | platform_secrets | none
    monthly_quota: int | None = None   # None = unlimited (BYOK)
    monthly_used: int = 0
    monthly_remaining: int | None = None
    daily_preview_limit: int = 10
    extra_usage_policy: str = "block"
    max_concurrent_tts: int = 1
    premium_voices_enabled: bool = False

    def __repr__(self) -> str:
        return (
            f"TtsEntitlement(tenant_id={self.tenant_id}, provider={self.provider!r}, "
            f"mode={self.credential_mode!r}, allowed={self.allowed}, "
            f"denial={self.denial_code!r})"
        )


def _denied(
    tenant_id: int,
    plan_id: int | None,
    provider: str,
    mode: str,
    code: str,
    message: str,
) -> TtsEntitlement:
    return TtsEntitlement(
        tenant_id=tenant_id,
        plan_id=plan_id,
        provider=provider,
        credential_mode=mode,
        allowed=False,
        denial_code=code,
        denial_message=message,
    )


def resolve_tts_entitlement(
    tenant_id: int,
    provider: str,
    purpose: str,  # "preview" | "production"
) -> TtsEntitlement:
    """Resolve full TTS entitlement for a request.

    Decision order:
    1. Validate inputs.
    2. Load tenant — reject if missing/suspended.
    3. Load plan TTS policy.
    4. Validate provider is in allowed_providers.
    5. Determine effective credential mode from tenant column.
    6. Validate mode against plan policy.
    7. BYOK: check tenant_secrets has key if provider requires one.
    8. platform_managed: check provider in platform_managed_providers,
       active platform secret exists, and quota remains.
    9. Return TtsEntitlement with safe metadata only.
    """
    from app.models.plan_tts_policy import (
        get_plan_tts_policy,
        KEY_REQUIRED_PROVIDERS,
        VALID_PROVIDERS,
    )
    from app.models.platform_secrets import platform_secret_is_active
    from app.models.tenant_tts_usage import get_total_monthly_characters

    # ── 1. Input validation ──────────────────────────────────────────────────
    if provider not in VALID_PROVIDERS:
        return _denied(tenant_id, None, provider, "byok", PROVIDER_INVALID,
                       f"Unknown TTS provider: {provider!r}")

    if purpose not in ("preview", "production"):
        purpose = "production"

    # ── 2. Load tenant ───────────────────────────────────────────────────────
    from app.models.tenant import get_tenant_by_id
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        return _denied(tenant_id, None, provider, "byok", TENANT_NOT_FOUND,
                       "Tenant not found.")

    tenant_status = tenant.get("status", "")
    if tenant_status in ("suspended", "cancelled"):
        return _denied(tenant_id, tenant.get("plan_id"), provider, "byok",
                       TENANT_SUSPENDED, "Your account is suspended or cancelled.")
    # past_due and expired also block TTS generation
    if tenant_status in ("past_due", "expired"):
        from app.services.subscription_access_service import check_feature_access
        _access = check_feature_access(tenant_id, "tts_generate")
        if not _access.allowed:
            return _denied(tenant_id, tenant.get("plan_id"), provider, "byok",
                           TENANT_SUSPENDED, _access.message)

    plan_id: int | None = tenant.get("plan_id")
    tts_mode: str = tenant.get("tts_credential_mode") or "byok"

    # ── 3. Load plan TTS policy ──────────────────────────────────────────────
    policy = get_plan_tts_policy(plan_id)

    # ── 4. Provider allowlist ────────────────────────────────────────────────
    if provider not in policy["allowed_providers"]:
        return _denied(
            tenant_id, plan_id, provider, tts_mode,
            PROVIDER_NOT_ALLOWED,
            f"Provider '{provider}' is not available on your current plan.",
        )

    # ── 5+6. Credential mode validation ─────────────────────────────────────
    from app.models.plan_tts_policy import mode_allowed_for_plan
    if not mode_allowed_for_plan(plan_id, tts_mode):
        return _denied(
            tenant_id, plan_id, provider, tts_mode,
            CREDENTIAL_MODE_NOT_ALLOWED,
            f"Credential mode '{tts_mode}' is not allowed on your current plan.",
        )

    # ── 7. BYOK path ─────────────────────────────────────────────────────────
    if tts_mode == "byok":
        secret_source = "none"
        if provider in KEY_REQUIRED_PROVIDERS:
            # Verify tenant has a key stored
            from app.models.tenant_secrets import get_secret_plaintext
            has_key = bool(get_secret_plaintext(tenant_id, f"{provider}_api_key"))
            if not has_key:
                return _denied(
                    tenant_id, plan_id, provider, tts_mode,
                    TENANT_CREDENTIAL_MISSING,
                    f"Your {provider.title()} API key is not configured. "
                    "Please add your API key in the Voice Script settings.",
                )
            secret_source = "tenant_secrets"

        return TtsEntitlement(
            tenant_id=tenant_id,
            plan_id=plan_id,
            provider=provider,
            credential_mode="byok",
            allowed=True,
            secret_source=secret_source,
            monthly_quota=None,       # BYOK = no platform quota
            monthly_used=0,
            monthly_remaining=None,   # unlimited from platform POV
            daily_preview_limit=policy["daily_preview_limit"],
            extra_usage_policy=policy["extra_usage_policy"],
            max_concurrent_tts=policy["max_concurrent_tts"],
            premium_voices_enabled=policy["premium_voices_enabled"],
        )

    # ── 8. Platform-managed path ─────────────────────────────────────────────
    if tts_mode == "platform_managed":
        # Provider must be in platform_managed_providers
        if provider not in policy["platform_managed_providers"]:
            return _denied(
                tenant_id, plan_id, provider, tts_mode,
                PROVIDER_NOT_ALLOWED,
                f"Provider '{provider}' is not available via platform-managed mode on your plan.",
            )

        # Active platform secret must exist
        if not platform_secret_is_active(provider):
            return _denied(
                tenant_id, plan_id, provider, tts_mode,
                PLATFORM_CREDENTIAL_UNAVAILABLE,
                f"Platform TTS credential for '{provider}' is currently unavailable.",
            )

        # Quota check
        quota = policy.get("monthly_character_quota")
        used = 0
        remaining = None
        if quota is not None and quota > 0:
            used = get_total_monthly_characters(tenant_id, provider, "platform_managed")
            remaining = max(0, quota - used)
            if remaining <= 0:
                return _denied(
                    tenant_id, plan_id, provider, tts_mode,
                    MONTHLY_TTS_QUOTA_EXCEEDED,
                    "Your monthly TTS character quota has been reached. "
                    "Quota resets on the 1st of next month.",
                )

        return TtsEntitlement(
            tenant_id=tenant_id,
            plan_id=plan_id,
            provider=provider,
            credential_mode="platform_managed",
            allowed=True,
            secret_source="platform_secrets",
            monthly_quota=quota,
            monthly_used=used,
            monthly_remaining=remaining,
            daily_preview_limit=policy["daily_preview_limit"],
            extra_usage_policy=policy["extra_usage_policy"],
            max_concurrent_tts=policy["max_concurrent_tts"],
            premium_voices_enabled=policy["premium_voices_enabled"],
        )

    # Should never reach here given CHECK constraint
    return _denied(tenant_id, plan_id, provider, tts_mode,
                   CREDENTIAL_MODE_NOT_ALLOWED, f"Unknown credential mode: {tts_mode!r}")


def resolve_internal_tts_credential(entitlement: TtsEntitlement) -> str | None:
    """Return the decrypted API key for generation. INTERNAL USE ONLY.

    SECURITY CONTRACT:
    - Caller MUST NOT log this value.
    - Caller MUST NOT include in exception messages.
    - Caller MUST NOT serialize or return to routes.
    - Called only from generate_order_audio() in tts_service.py.

    Returns None if no key needed (free providers) or entitlement is denied.
    """
    if not entitlement.allowed:
        return None

    from app.models.plan_tts_policy import KEY_REQUIRED_PROVIDERS

    if entitlement.provider not in KEY_REQUIRED_PROVIDERS:
        return None  # edge/gtts need no key

    if entitlement.credential_mode == "byok":
        from app.models.tenant_secrets import get_secret_plaintext
        return get_secret_plaintext(
            entitlement.tenant_id, f"{entitlement.provider}_api_key"
        )

    if entitlement.credential_mode == "platform_managed":
        from app.models.platform_secrets import get_platform_secret_for_generation
        return get_platform_secret_for_generation(entitlement.provider)

    return None
