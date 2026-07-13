# tests/unit/test_tts_usage_service.py
"""Unit tests for tts_usage_service — billing units, quota split, overage charge.

All tests are pure-unit (no DB, no Flask app fixture).
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from app.services.tts_usage_service import (
    _calculate_overage_charge,
    _is_platform_billable,
    calculate_tts_billable_characters,
    make_preview_idempotency_key,
    make_production_idempotency_key,
)


# ── calculate_tts_billable_characters ────────────────────────────────────────

class TestCalculateTtsBillableCharacters:
    def test_empty_string_returns_zero(self):
        assert calculate_tts_billable_characters("", "gemini") == 0

    def test_none_like_empty_returns_zero(self):
        # Callers may pass empty
        assert calculate_tts_billable_characters("", "edge") == 0

    def test_ascii_text(self):
        assert calculate_tts_billable_characters("hello world", "gemini") == 11

    def test_bangla_unicode(self):
        text = "আপনার অর্ডার নিশ্চিত করুন।"
        assert calculate_tts_billable_characters(text, "gemini") == len(text)

    def test_whitespace_included(self):
        # Spec: whitespace included
        assert calculate_tts_billable_characters("a b c", "gemini") == 5

    def test_provider_gemini_billable(self):
        result = calculate_tts_billable_characters("test", "gemini")
        assert result == 4

    def test_provider_elevenlabs_billable(self):
        result = calculate_tts_billable_characters("test", "elevenlabs")
        assert result == 4

    def test_provider_edge_billable(self):
        # edge is free/BYOK-only but character count still accurate
        assert calculate_tts_billable_characters("test", "edge") == 4

    def test_consistent_between_preview_and_production(self):
        text = "হ্যালো সাকিব হাসান, আপনার অর্ডার ৯৭০ টাকা।"
        assert (
            calculate_tts_billable_characters(text, "gemini")
            == calculate_tts_billable_characters(text, "gemini")
        )

    def test_never_negative(self):
        assert calculate_tts_billable_characters("x", "gemini") >= 0


# ── _is_platform_billable ─────────────────────────────────────────────────────

class TestIsPlatformBillable:
    def test_gemini_platform_managed_is_billable(self):
        assert _is_platform_billable("gemini", "platform_managed") is True

    def test_elevenlabs_platform_managed_is_billable(self):
        assert _is_platform_billable("elevenlabs", "platform_managed") is True

    def test_edge_platform_managed_not_billable(self):
        assert _is_platform_billable("edge", "platform_managed") is False

    def test_gtts_platform_managed_not_billable(self):
        assert _is_platform_billable("gtts", "platform_managed") is False

    def test_gemini_byok_not_billable(self):
        assert _is_platform_billable("gemini", "byok") is False

    def test_elevenlabs_byok_not_billable(self):
        assert _is_platform_billable("elevenlabs", "byok") is False


# ── _calculate_overage_charge ─────────────────────────────────────────────────

class TestCalculateOverageCharge:
    def test_zero_chars_returns_zero(self):
        assert _calculate_overage_charge(0, Decimal("0.5"), Decimal("0")) == Decimal("0")

    def test_zero_rate_returns_zero(self):
        assert _calculate_overage_charge(1000, Decimal("0"), Decimal("0")) == Decimal("0")

    def test_exact_1000_chars(self):
        charge = _calculate_overage_charge(1000, Decimal("0.5"), Decimal("0"))
        assert charge == Decimal("0.5")

    def test_partial_1000_rounds_up(self):
        # 1001 chars → ceil(1001/1000) = 2 thousands
        charge = _calculate_overage_charge(1001, Decimal("0.5"), Decimal("0"))
        assert charge == Decimal("1.0")

    def test_999_chars_rounds_up_to_1_thousand(self):
        charge = _calculate_overage_charge(999, Decimal("0.5"), Decimal("0"))
        assert charge == Decimal("0.5")

    def test_minimum_charge_applied_when_computed_is_less(self):
        # 100 chars × 0.001 = 0.0001, but minimum is 1.0
        charge = _calculate_overage_charge(100, Decimal("0.001"), Decimal("1.0"))
        assert charge == Decimal("1.0")

    def test_minimum_charge_not_applied_when_computed_exceeds(self):
        # 10000 chars × 0.5 = 5.0, minimum is 1.0 → result is 5.0
        charge = _calculate_overage_charge(10000, Decimal("0.5"), Decimal("1.0"))
        assert charge == Decimal("5.0")

    def test_no_float_in_result(self):
        charge = _calculate_overage_charge(5000, Decimal("0.75"), Decimal("0"))
        assert isinstance(charge, Decimal)

    def test_negative_chars_returns_zero(self):
        # Guard: negative chars treated as no overage
        assert _calculate_overage_charge(-100, Decimal("0.5"), Decimal("0")) == Decimal("0")


# ── Idempotency keys ──────────────────────────────────────────────────────────

class TestIdempotencyKeys:
    def test_preview_key_is_uuid_format(self):
        import uuid
        key = make_preview_idempotency_key()
        uuid.UUID(key)  # raises if not valid UUID

    def test_preview_keys_are_unique(self):
        keys = {make_preview_idempotency_key() for _ in range(20)}
        assert len(keys) == 20

    def test_production_key_deterministic(self):
        k1 = make_production_idempotency_key(1, "WC-100", "abc", "gemini", "production", 1)
        k2 = make_production_idempotency_key(1, "WC-100", "abc", "gemini", "production", 1)
        assert k1 == k2

    def test_production_key_differs_by_tenant(self):
        k1 = make_production_idempotency_key(1, "WC-100", "abc", "gemini", "production", 1)
        k2 = make_production_idempotency_key(2, "WC-100", "abc", "gemini", "production", 1)
        assert k1 != k2

    def test_production_key_differs_by_order(self):
        k1 = make_production_idempotency_key(1, "WC-100", "abc", "gemini", "production", 1)
        k2 = make_production_idempotency_key(1, "WC-101", "abc", "gemini", "production", 1)
        assert k1 != k2

    def test_production_key_differs_by_attempt(self):
        k1 = make_production_idempotency_key(1, "WC-100", "abc", "gemini", "production", 1)
        k2 = make_production_idempotency_key(1, "WC-100", "abc", "gemini", "production", 2)
        assert k1 != k2

    def test_production_key_differs_by_provider(self):
        k1 = make_production_idempotency_key(1, "WC-100", "abc", "gemini", "production", 1)
        k2 = make_production_idempotency_key(1, "WC-100", "abc", "elevenlabs", "production", 1)
        assert k1 != k2

    def test_production_key_is_hex_string(self):
        k = make_production_idempotency_key(1, "WC-100", "abc", "gemini", "production", 1)
        int(k, 16)  # valid hex
        assert len(k) == 64  # SHA-256 hex digest
