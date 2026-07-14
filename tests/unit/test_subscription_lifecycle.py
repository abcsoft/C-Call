# tests/unit/test_subscription_lifecycle.py
"""Unit tests for subscription lifecycle service and access enforcement.

Tests cover:
- State machine transition validation
- Enforcement matrix (check_feature_access) for each status × feature
- Banner content per status
- Period-end calculation (monthly/yearly)
- Idempotency key uniqueness
"""
from __future__ import annotations

import datetime
import pytest


# ── State machine ─────────────────────────────────────────────────────────

class TestTransitionValidation:
    def _assert_allowed(self, from_s, to_s):
        from app.services.subscription_lifecycle_service import _assert_transition
        _assert_transition(from_s, to_s)  # should not raise

    def _assert_blocked(self, from_s, to_s):
        from app.services.subscription_lifecycle_service import _assert_transition
        with pytest.raises(ValueError):
            _assert_transition(from_s, to_s)

    def test_trial_to_active_allowed(self):
        self._assert_allowed("trial", "active")

    def test_trial_to_expired_allowed(self):
        self._assert_allowed("trial", "expired")

    def test_trial_to_grace_period_blocked(self):
        self._assert_blocked("trial", "grace_period")

    def test_active_to_grace_period_allowed(self):
        self._assert_allowed("active", "grace_period")

    def test_active_to_cancelled_allowed(self):
        self._assert_allowed("active", "cancelled")

    def test_active_to_suspended_allowed(self):
        # Phase 1 fix: active → suspended is valid (admin security/compliance action)
        self._assert_allowed("active", "suspended")

    def test_grace_period_to_active_allowed(self):
        self._assert_allowed("grace_period", "active")

    def test_grace_period_to_past_due_allowed(self):
        self._assert_allowed("grace_period", "past_due")

    def test_past_due_to_active_allowed(self):
        self._assert_allowed("past_due", "active")

    def test_past_due_to_suspended_allowed(self):
        self._assert_allowed("past_due", "suspended")

    def test_suspended_to_active_allowed(self):
        self._assert_allowed("suspended", "active")

    def test_suspended_to_trial_blocked(self):
        self._assert_blocked("suspended", "trial")

    def test_cancelled_to_active_allowed(self):
        self._assert_allowed("cancelled", "active")

    def test_expired_to_active_allowed(self):
        self._assert_allowed("expired", "active")

    def test_expired_to_trial_blocked(self):
        self._assert_blocked("expired", "trial")

    def test_grace_period_to_suspended_allowed(self):
        # Phase 1 fix: grace_period → suspended is valid (admin action)
        self._assert_allowed("grace_period", "suspended")

    def test_past_due_to_expired_allowed(self):
        # Phase 1 fix: past_due → expired when past_due_ends_at passes
        self._assert_allowed("past_due", "expired")

    def test_suspended_to_cancelled_allowed(self):
        self._assert_allowed("suspended", "cancelled")

    def test_idempotent_same_status(self):
        # Same-status transition must not raise (idempotent)
        from app.services.subscription_lifecycle_service import _assert_transition
        for s in ("trial", "active", "grace_period", "past_due", "suspended", "cancelled", "expired"):
            _assert_transition(s, s)  # must not raise


# ── Period end calculation ─────────────────────────────────────────────────

class TestPeriodEndCalculation:
    def _compute(self, billing_cycle, start):
        from app.services.subscription_lifecycle_service import _compute_period_end
        return _compute_period_end(start, billing_cycle)

    def test_monthly_add_one_month(self):
        start = datetime.datetime(2026, 1, 15, tzinfo=datetime.timezone.utc)
        end = self._compute("monthly", start)
        assert end.year == 2026 and end.month == 2 and end.day == 15

    def test_monthly_january_end(self):
        start = datetime.datetime(2026, 1, 31, tzinfo=datetime.timezone.utc)
        end = self._compute("monthly", start)
        # Feb has 28 days in 2026; clamp to 28
        assert end.month == 2 and end.day == 28

    def test_monthly_december_wraps_to_next_year(self):
        start = datetime.datetime(2026, 12, 1, tzinfo=datetime.timezone.utc)
        end = self._compute("monthly", start)
        assert end.year == 2027 and end.month == 1

    def test_yearly_adds_one_year(self):
        start = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        end = self._compute("yearly", start)
        assert end.year == 2027 and end.month == 6 and end.day == 1

    def test_yearly_leap_day_handling(self):
        start = datetime.datetime(2024, 2, 29, tzinfo=datetime.timezone.utc)
        end = self._compute("yearly", start)
        assert end.year == 2025 and end.month == 2 and end.day == 28


# ── Enforcement matrix (no DB needed — monkeypatch db.query_one) ───────────

FEATURES = ["order_intake", "queue_call", "process_retry", "tts_generate",
            "api_access", "wallet_deduct", "dashboard"]

STATUSES = ["trial", "active", "grace_period", "past_due", "suspended", "cancelled", "expired"]

# Expected allow/deny per (feature, status)
_EXPECTED_ALLOW = {
    ("order_intake",  "trial"):        True,
    ("order_intake",  "active"):       True,
    ("order_intake",  "grace_period"): True,
    ("order_intake",  "past_due"):     False,
    ("order_intake",  "suspended"):    False,
    ("order_intake",  "cancelled"):    False,
    ("order_intake",  "expired"):      False,
    ("queue_call",    "trial"):        True,
    ("queue_call",    "active"):       True,
    ("queue_call",    "grace_period"): True,
    ("queue_call",    "past_due"):     False,
    ("queue_call",    "suspended"):    False,
    ("queue_call",    "cancelled"):    False,
    ("queue_call",    "expired"):      False,
    ("api_access",    "trial"):        True,
    ("api_access",    "active"):       True,
    ("api_access",    "grace_period"): True,
    ("api_access",    "past_due"):     False,
    ("api_access",    "suspended"):    False,
    ("api_access",    "cancelled"):    False,
    ("api_access",    "expired"):      False,
    ("dashboard",     "trial"):        True,
    ("dashboard",     "active"):       True,
    ("dashboard",     "grace_period"): True,
    ("dashboard",     "past_due"):     True,
    ("dashboard",     "suspended"):    False,
    ("dashboard",     "cancelled"):    False,
    ("dashboard",     "expired"):      True,
    ("wallet_deduct", "trial"):        True,
    ("wallet_deduct", "active"):       True,
    ("wallet_deduct", "grace_period"): True,
    ("wallet_deduct", "past_due"):     True,
    ("wallet_deduct", "suspended"):    False,
    ("wallet_deduct", "cancelled"):    False,
    ("wallet_deduct", "expired"):      False,
    ("tts_generate",  "trial"):        True,
    ("tts_generate",  "active"):       True,
    ("tts_generate",  "grace_period"): True,
    ("tts_generate",  "past_due"):     False,
    ("tts_generate",  "suspended"):    False,
    ("tts_generate",  "cancelled"):    False,
    ("tts_generate",  "expired"):      False,
}


@pytest.mark.parametrize("feature,status,expected_allow", [
    (f, s, _EXPECTED_ALLOW[(f, s)])
    for f in ["order_intake", "queue_call", "api_access", "dashboard", "wallet_deduct", "tts_generate"]
    for s in STATUSES
])
def test_enforcement_matrix(feature, status, expected_allow, monkeypatch):
    from app.services import subscription_access_service as sas
    monkeypatch.setattr(sas, "db", type("FakeDB", (), {
        "query_one": staticmethod(lambda sql, params: {"status": status})
    })())
    result = sas.check_feature_access(1, feature)
    assert result.allowed == expected_allow, (
        f"check_feature_access(1, {feature!r}) with status={status!r}: "
        f"expected allowed={expected_allow}, got {result.allowed} (outcome={result.outcome})"
    )


# ── HTTP status codes for denials ─────────────────────────────────────────

def test_past_due_api_access_returns_402(monkeypatch):
    from app.services import subscription_access_service as sas
    monkeypatch.setattr(sas, "db", type("FakeDB", (), {
        "query_one": staticmethod(lambda sql, params: {"status": "past_due"})
    })())
    result = sas.check_feature_access(1, "api_access")
    assert result.http_status == 402

def test_suspended_api_access_returns_403(monkeypatch):
    from app.services import subscription_access_service as sas
    monkeypatch.setattr(sas, "db", type("FakeDB", (), {
        "query_one": staticmethod(lambda sql, params: {"status": "suspended"})
    })())
    result = sas.check_feature_access(1, "api_access")
    assert result.http_status == 403

def test_active_order_intake_returns_200(monkeypatch):
    from app.services import subscription_access_service as sas
    monkeypatch.setattr(sas, "db", type("FakeDB", (), {
        "query_one": staticmethod(lambda sql, params: {"status": "active"})
    })())
    result = sas.check_feature_access(1, "order_intake")
    assert result.http_status == 200


# ── Banner content ─────────────────────────────────────────────────────────

def test_banner_trial_ending_soon(monkeypatch):
    from app.services import subscription_access_service as sas
    import datetime
    soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)
    monkeypatch.setattr(sas, "db", type("FakeDB", (), {
        "query_one": staticmethod(lambda sql, params: {
            "status": "trial", "trial_ends_at": soon,
            "current_period_end": None, "grace_ends_at": None
        })
    })())
    banner = sas.get_tenant_status_banner(1)
    assert banner is not None
    assert banner["type"] == "warning"
    assert "trial" in banner["message"].lower()

def test_banner_grace_period(monkeypatch):
    from app.services import subscription_access_service as sas
    import datetime
    grace = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=5)
    monkeypatch.setattr(sas, "db", type("FakeDB", (), {
        "query_one": staticmethod(lambda sql, params: {
            "status": "grace_period", "trial_ends_at": None,
            "current_period_end": None, "grace_ends_at": grace
        })
    })())
    banner = sas.get_tenant_status_banner(1)
    assert banner is not None
    assert banner["type"] == "warning"

def test_banner_past_due(monkeypatch):
    from app.services import subscription_access_service as sas
    monkeypatch.setattr(sas, "db", type("FakeDB", (), {
        "query_one": staticmethod(lambda sql, params: {
            "status": "past_due", "trial_ends_at": None,
            "current_period_end": None, "grace_ends_at": None
        })
    })())
    banner = sas.get_tenant_status_banner(1)
    assert banner is not None
    assert banner["type"] == "danger"

def test_banner_active_is_none(monkeypatch):
    from app.services import subscription_access_service as sas
    monkeypatch.setattr(sas, "db", type("FakeDB", (), {
        "query_one": staticmethod(lambda sql, params: {
            "status": "active", "trial_ends_at": None,
            "current_period_end": None, "grace_ends_at": None
        })
    })())
    assert sas.get_tenant_status_banner(1) is None


# ── Idempotency key uniqueness ─────────────────────────────────────────────

def test_payment_idempotency_key_unique():
    from app.services.subscription_lifecycle_service import _payment_idempotency_key
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    k1 = _payment_idempotency_key(1, 10, now)
    k2 = _payment_idempotency_key(2, 10, now)
    k3 = _payment_idempotency_key(1, 11, now)
    assert k1 != k2
    assert k1 != k3
    assert k2 != k3

def test_payment_idempotency_key_same_inputs_deterministic():
    from app.services.subscription_lifecycle_service import _payment_idempotency_key
    import datetime
    t = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    assert _payment_idempotency_key(5, 20, t) == _payment_idempotency_key(5, 20, t)
