"""Phase 9: TTS subscription security tests.

All imports inside resolve_tts_entitlement are deferred (inside the function body),
so patches must target the SOURCE module, not the caller module.

Patch targets:
  app.models.tenant.get_tenant_by_id
  app.models.plan_tts_policy.get_plan_tts_policy
  app.models.plan_tts_policy.mode_allowed_for_plan
  app.models.platform_secrets.platform_secret_is_active
  app.models.tenant_tts_usage.get_total_monthly_characters
  app.models.tenant_secrets.get_secret_plaintext
  app.models.platform_secrets.get_platform_secret_for_generation
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock, call


_BYOK_POLICY = {
    "allowed_providers": ["edge", "gtts", "gemini", "elevenlabs", "openvoice"],
    "credential_policy": "byok_only",
    "platform_managed_providers": [],
    "monthly_character_quota": None,
    "daily_preview_limit": 10,
    "extra_usage_policy": "block",
    "max_concurrent_tts": 1,
    "premium_voices_enabled": False,
}

_PLATFORM_POLICY = {
    "allowed_providers": ["edge", "gtts", "gemini", "elevenlabs"],
    "credential_policy": "platform_managed",
    "platform_managed_providers": ["gemini", "elevenlabs"],
    "monthly_character_quota": 10000,
    "daily_preview_limit": 10,
    "extra_usage_policy": "block",
    "max_concurrent_tts": 2,
    "premium_voices_enabled": True,
}

_ACTIVE_TENANT = {
    "id": 1, "plan_id": 1, "status": "active", "tts_credential_mode": "byok"
}


# ─────────────────────────────────────────────────────────────────────────────
# Entitlement denial codes
# ─────────────────────────────────────────────────────────────────────────────

class TestTtsEntitlementDenialCodes:

    def test_unknown_provider_returns_provider_invalid(self):
        from app.services.tts_entitlement_service import resolve_tts_entitlement, PROVIDER_INVALID
        # Provider check happens BEFORE tenant lookup
        result = resolve_tts_entitlement(1, "evil_provider_xyz", "production")
        assert not result.allowed
        assert result.denial_code == PROVIDER_INVALID

    def test_suspended_tenant_returns_tenant_suspended(self):
        from app.services.tts_entitlement_service import resolve_tts_entitlement, TENANT_SUSPENDED
        suspended = {**_ACTIVE_TENANT, "status": "suspended"}
        with patch("app.models.tenant.get_tenant_by_id", return_value=suspended):
            result = resolve_tts_entitlement(99, "edge", "production")
        assert not result.allowed
        assert result.denial_code == TENANT_SUSPENDED

    def test_missing_tenant_returns_tenant_not_found(self):
        from app.services.tts_entitlement_service import resolve_tts_entitlement, TENANT_NOT_FOUND
        with patch("app.models.tenant.get_tenant_by_id", return_value=None):
            result = resolve_tts_entitlement(9999, "edge", "production")
        assert not result.allowed
        assert result.denial_code == TENANT_NOT_FOUND

    def test_provider_not_in_plan_returns_provider_not_allowed(self):
        from app.services.tts_entitlement_service import resolve_tts_entitlement, PROVIDER_NOT_ALLOWED
        restricted_policy = {**_BYOK_POLICY, "allowed_providers": ["edge", "gtts"]}
        with patch("app.models.tenant.get_tenant_by_id", return_value=_ACTIVE_TENANT), \
             patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=restricted_policy):
            result = resolve_tts_entitlement(1, "elevenlabs", "production")
        assert not result.allowed
        assert result.denial_code == PROVIDER_NOT_ALLOWED

    def test_platform_managed_mode_blocked_on_byok_only_plan(self):
        from app.services.tts_entitlement_service import resolve_tts_entitlement, CREDENTIAL_MODE_NOT_ALLOWED
        platform_tenant = {**_ACTIVE_TENANT, "tts_credential_mode": "platform_managed"}
        with patch("app.models.tenant.get_tenant_by_id", return_value=platform_tenant), \
             patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=_BYOK_POLICY), \
             patch("app.models.plan_tts_policy.mode_allowed_for_plan", return_value=False):
            result = resolve_tts_entitlement(1, "gemini", "production")
        assert not result.allowed
        assert result.denial_code == CREDENTIAL_MODE_NOT_ALLOWED

    def test_byok_missing_key_returns_tenant_credential_missing(self):
        from app.services.tts_entitlement_service import resolve_tts_entitlement, TENANT_CREDENTIAL_MISSING
        with patch("app.models.tenant.get_tenant_by_id", return_value=_ACTIVE_TENANT), \
             patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=_BYOK_POLICY), \
             patch("app.models.plan_tts_policy.mode_allowed_for_plan", return_value=True), \
             patch("app.models.tenant_secrets.get_secret_plaintext", return_value=None):
            result = resolve_tts_entitlement(1, "gemini", "production")
        assert not result.allowed
        assert result.denial_code == TENANT_CREDENTIAL_MISSING

    def test_quota_exceeded_returns_monthly_tts_quota_exceeded(self):
        from app.services.tts_entitlement_service import resolve_tts_entitlement, MONTHLY_TTS_QUOTA_EXCEEDED
        platform_tenant = {**_ACTIVE_TENANT, "plan_id": 2, "tts_credential_mode": "platform_managed"}
        with patch("app.models.tenant.get_tenant_by_id", return_value=platform_tenant), \
             patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=_PLATFORM_POLICY), \
             patch("app.models.plan_tts_policy.mode_allowed_for_plan", return_value=True), \
             patch("app.models.platform_secrets.platform_secret_is_active", return_value=True), \
             patch("app.models.tenant_tts_usage.get_total_monthly_characters", return_value=10000):
            result = resolve_tts_entitlement(1, "gemini", "production")
        assert not result.allowed
        assert result.denial_code == MONTHLY_TTS_QUOTA_EXCEEDED

    def test_platform_credential_unavailable_when_secret_inactive(self):
        from app.services.tts_entitlement_service import resolve_tts_entitlement, PLATFORM_CREDENTIAL_UNAVAILABLE
        platform_tenant = {**_ACTIVE_TENANT, "plan_id": 2, "tts_credential_mode": "platform_managed"}
        with patch("app.models.tenant.get_tenant_by_id", return_value=platform_tenant), \
             patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=_PLATFORM_POLICY), \
             patch("app.models.plan_tts_policy.mode_allowed_for_plan", return_value=True), \
             patch("app.models.platform_secrets.platform_secret_is_active", return_value=False):
            result = resolve_tts_entitlement(1, "gemini", "production")
        assert not result.allowed
        assert result.denial_code == PLATFORM_CREDENTIAL_UNAVAILABLE

    def test_byok_free_provider_allowed_without_key(self):
        """edge/gtts need no API key — should be allowed even with no tenant_secrets row."""
        from app.services.tts_entitlement_service import resolve_tts_entitlement
        with patch("app.models.tenant.get_tenant_by_id", return_value=_ACTIVE_TENANT), \
             patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=_BYOK_POLICY), \
             patch("app.models.plan_tts_policy.mode_allowed_for_plan", return_value=True):
            result = resolve_tts_entitlement(1, "edge", "production")
        assert result.allowed
        assert result.denial_code is None

    def test_byok_with_key_present_is_allowed(self):
        from app.services.tts_entitlement_service import resolve_tts_entitlement
        with patch("app.models.tenant.get_tenant_by_id", return_value=_ACTIVE_TENANT), \
             patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=_BYOK_POLICY), \
             patch("app.models.plan_tts_policy.mode_allowed_for_plan", return_value=True), \
             patch("app.models.tenant_secrets.get_secret_plaintext", return_value="sk_test"):
            result = resolve_tts_entitlement(1, "gemini", "production")
        assert result.allowed
        assert result.credential_mode == "byok"

    def test_platform_managed_under_quota_is_allowed(self):
        from app.services.tts_entitlement_service import resolve_tts_entitlement
        platform_tenant = {**_ACTIVE_TENANT, "plan_id": 2, "tts_credential_mode": "platform_managed"}
        with patch("app.models.tenant.get_tenant_by_id", return_value=platform_tenant), \
             patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=_PLATFORM_POLICY), \
             patch("app.models.plan_tts_policy.mode_allowed_for_plan", return_value=True), \
             patch("app.models.platform_secrets.platform_secret_is_active", return_value=True), \
             patch("app.models.tenant_tts_usage.get_total_monthly_characters", return_value=5000):
            result = resolve_tts_entitlement(1, "gemini", "production")
        assert result.allowed
        assert result.monthly_remaining == 5000


# ─────────────────────────────────────────────────────────────────────────────
# TtsEntitlement repr — no secrets
# ─────────────────────────────────────────────────────────────────────────────

class TestTtsEntitlementReprNoSecrets:

    def test_repr_contains_no_key_value(self):
        from app.services.tts_entitlement_service import TtsEntitlement
        e = TtsEntitlement(
            tenant_id=1, plan_id=1, provider="gemini",
            credential_mode="byok", allowed=True
        )
        r = repr(e)
        assert "AIza" not in r
        assert "sk_" not in r

    def test_repr_short_no_key_field(self):
        from app.services.tts_entitlement_service import TtsEntitlement
        e = TtsEntitlement(
            tenant_id=5, plan_id=2, provider="elevenlabs",
            credential_mode="platform_managed", allowed=True,
            secret_source="platform_secrets"
        )
        r = repr(e)
        assert len(r) < 300


# ─────────────────────────────────────────────────────────────────────────────
# resolve_internal_tts_credential
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveInternalCredentialSecurity:

    def test_denied_entitlement_returns_none(self):
        from app.services.tts_entitlement_service import TtsEntitlement, resolve_internal_tts_credential
        denied = TtsEntitlement(
            tenant_id=1, plan_id=1, provider="gemini",
            credential_mode="byok", allowed=False,
            denial_code="PROVIDER_NOT_ALLOWED"
        )
        assert resolve_internal_tts_credential(denied) is None

    def test_free_provider_returns_none_no_db_call(self):
        from app.services.tts_entitlement_service import TtsEntitlement, resolve_internal_tts_credential
        e = TtsEntitlement(
            tenant_id=1, plan_id=1, provider="edge",
            credential_mode="byok", allowed=True
        )
        with patch("app.models.tenant_secrets.get_secret_plaintext") as m:
            result = resolve_internal_tts_credential(e)
        assert result is None
        m.assert_not_called()

    def test_byok_gemini_calls_tenant_secrets_with_correct_tenant(self):
        from app.services.tts_entitlement_service import TtsEntitlement, resolve_internal_tts_credential
        e = TtsEntitlement(
            tenant_id=7, plan_id=1, provider="gemini",
            credential_mode="byok", allowed=True,
            secret_source="tenant_secrets"
        )
        with patch("app.models.tenant_secrets.get_secret_plaintext", return_value="key_t7") as mt, \
             patch("app.models.platform_secrets.get_platform_secret_for_generation") as mp:
            result = resolve_internal_tts_credential(e)
        assert result == "key_t7"
        mt.assert_called_once_with(7, "gemini_api_key")
        mp.assert_not_called()

    def test_platform_managed_calls_platform_not_tenant_secrets(self):
        from app.services.tts_entitlement_service import TtsEntitlement, resolve_internal_tts_credential
        e = TtsEntitlement(
            tenant_id=7, plan_id=2, provider="gemini",
            credential_mode="platform_managed", allowed=True,
            secret_source="platform_secrets"
        )
        with patch("app.models.tenant_secrets.get_secret_plaintext") as mt, \
             patch("app.models.platform_secrets.get_platform_secret_for_generation", return_value="plat_key") as mp:
            result = resolve_internal_tts_credential(e)
        assert result == "plat_key"
        mp.assert_called_once_with("gemini")
        mt.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# platform_secrets model
# ─────────────────────────────────────────────────────────────────────────────

class TestPlatformSecretsEncryption:

    def test_get_meta_contains_no_encrypted_value_field(self):
        from app.models.platform_secrets import get_platform_secret_meta
        fake_row = {
            "provider": "gemini", "status": "active",
            "last_test_status": "ok", "last_test_at": None,
            "updated_at": None, "updated_by_user_id": 1,
        }
        with patch("app.models.platform_secrets.db") as m:
            m.query_one.return_value = fake_row
            result = get_platform_secret_meta("gemini")
        assert "encrypted_value" not in result
        assert "plaintext" not in str(result).lower()

    def test_fernet_key_missing_raises_exception(self):
        import os
        import app.models.platform_secrets as ps_mod
        env_without = {k: v for k, v in os.environ.items()
                       if k != "PLATFORM_SECRET_ENCRYPTION_KEY"}
        with patch.dict(os.environ, env_without, clear=True):
            with pytest.raises((RuntimeError, Exception)):
                ps_mod._get_fernet()


# ─────────────────────────────────────────────────────────────────────────────
# plan_tts_policy model
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanTtsPolicyModel:

    def test_byok_only_plan_rejects_platform_managed_mode(self):
        from app.models.plan_tts_policy import mode_allowed_for_plan
        with patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=_BYOK_POLICY):
            assert mode_allowed_for_plan(1, "platform_managed") is False

    def test_byok_only_plan_allows_byok_mode(self):
        from app.models.plan_tts_policy import mode_allowed_for_plan
        with patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=_BYOK_POLICY):
            assert mode_allowed_for_plan(1, "byok") is True

    def test_unknown_provider_not_in_allowed(self):
        from app.models.plan_tts_policy import provider_allowed_for_plan
        with patch("app.models.plan_tts_policy.get_plan_tts_policy", return_value=_BYOK_POLICY):
            assert provider_allowed_for_plan(1, "evil_provider") is False

    def test_none_plan_id_returns_safe_byok_default(self):
        from app.models.plan_tts_policy import get_plan_tts_policy
        with patch("app.models.plan_tts_policy.db") as m:
            m.query_one.return_value = None
            result = get_plan_tts_policy(None)
        assert result["credential_policy"] == "byok_only"
        assert result["platform_managed_providers"] == []

    def test_byok_only_with_nonempty_platform_providers_sanitized(self):
        """DB inconsistency: byok_only + non-empty platform_managed_providers → model forces empty."""
        from app.models.plan_tts_policy import get_plan_tts_policy
        bad_row = {
            "plan_id": 1,
            "allowed_providers": '["edge","gemini"]',
            "credential_policy": "byok_only",
            "platform_managed_providers": '["gemini"]',
            "monthly_character_quota": None,
            "daily_preview_limit": 10,
            "extra_usage_policy": "block",
            "max_concurrent_tts": 1,
            "premium_voices_enabled": False,
        }
        with patch("app.models.plan_tts_policy.db") as m:
            m.query_one.return_value = bad_row
            result = get_plan_tts_policy(1)
        assert result["platform_managed_providers"] == []


# ─────────────────────────────────────────────────────────────────────────────
# Cross-tenant isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossTenantIsolation:

    def test_byok_credential_lookup_uses_own_tenant_id(self):
        """Two tenants must never share credentials."""
        from app.services.tts_entitlement_service import TtsEntitlement, resolve_internal_tts_credential
        calls = []
        def fake_get_secret(tid, key_name):
            calls.append((tid, key_name))
            return f"key_for_{tid}"

        e7 = TtsEntitlement(tenant_id=7, plan_id=1, provider="gemini",
                             credential_mode="byok", allowed=True, secret_source="tenant_secrets")
        e8 = TtsEntitlement(tenant_id=8, plan_id=1, provider="gemini",
                             credential_mode="byok", allowed=True, secret_source="tenant_secrets")

        with patch("app.models.tenant_secrets.get_secret_plaintext", side_effect=fake_get_secret):
            r7 = resolve_internal_tts_credential(e7)
            r8 = resolve_internal_tts_credential(e8)

        assert r7 == "key_for_7"
        assert r8 == "key_for_8"
        assert r7 != r8
        assert calls[0][0] == 7
        assert calls[1][0] == 8

    def test_platform_managed_never_reads_tenant_secrets(self):
        from app.services.tts_entitlement_service import TtsEntitlement, resolve_internal_tts_credential
        e = TtsEntitlement(tenant_id=5, plan_id=2, provider="elevenlabs",
                           credential_mode="platform_managed", allowed=True,
                           secret_source="platform_secrets")
        with patch("app.models.tenant_secrets.get_secret_plaintext") as tenant_mock, \
             patch("app.models.platform_secrets.get_platform_secret_for_generation", return_value="xi_key"):
            result = resolve_internal_tts_credential(e)
        tenant_mock.assert_not_called()
        assert result == "xi_key"

    def test_byok_never_reads_platform_secrets(self):
        from app.services.tts_entitlement_service import TtsEntitlement, resolve_internal_tts_credential
        e = TtsEntitlement(tenant_id=3, plan_id=1, provider="gemini",
                           credential_mode="byok", allowed=True,
                           secret_source="tenant_secrets")
        with patch("app.models.tenant_secrets.get_secret_plaintext", return_value="my_key"), \
             patch("app.models.platform_secrets.get_platform_secret_for_generation") as plat_mock:
            result = resolve_internal_tts_credential(e)
        plat_mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Exception repr safety
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptionReprNoSecrets:

    def test_tts_credential_missing_no_key_value(self):
        from app.exceptions import TTSCredentialMissingError
        e = TTSCredentialMissingError(provider="gemini")
        assert "AIza" not in str(e)
        assert "sk_" not in str(e)

    def test_tts_provider_not_allowed_no_key_value(self):
        from app.exceptions import TTSProviderNotAllowedError
        e = TTSProviderNotAllowedError(provider="elevenlabs", tenant_id=5)
        assert "sk_" not in str(e)

    def test_tts_quota_exceeded_message_is_user_friendly(self):
        from app.exceptions import TTSQuotaExceededError
        e = TTSQuotaExceededError(tenant_id=3, provider="gemini")
        msg = str(e).lower()
        assert "quota" in msg or "monthly" in msg

    def test_tts_credential_mode_error_no_key_value(self):
        from app.exceptions import TTSCredentialModeError
        e = TTSCredentialModeError(mode="platform_managed", tenant_id=1)
        assert "sk_" not in str(e)
        assert "AIza" not in str(e)
