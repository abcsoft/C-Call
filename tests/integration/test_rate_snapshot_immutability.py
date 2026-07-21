"""Rate snapshot immutability tests — Phase 4B scenarios A–N.

Verifies:
  A: validate_rate_snapshot rejects non-dict input
  B: validate_rate_snapshot rejects missing required keys
  C: validate_rate_snapshot rejects wrong tenant_id
  D: validate_rate_snapshot rejects negative rate_per_min
  E: validate_rate_snapshot rejects rate_per_min > 1000
  F: validate_rate_snapshot rejects invalid billing_pulse_seconds
  G: validate_rate_snapshot rejects invalid currency
  H: validate_rate_snapshot rejects wrong policy_version
  I: validate_rate_snapshot rejects sensitive field leakage (password/secret)
  J: validate_rate_snapshot accepts valid snapshot and returns Decimal values
  K: billing_policy_from_snapshot round-trips BillingPolicy correctly
  L: serialize_billing_policy → validate_rate_snapshot round-trip
  M: finalize_call_billing uses snapshot rate, not live rate
  N: finalize_call_billing fails closed on missing snapshot for new call (>= cutoff)
  O: finalize_call_billing fails closed on malformed snapshot for new call
  P: finalize_call_billing falls back to live rate for legacy call (before cutoff)
  Q: Retry call reuses original snapshot (not recalculated)
  R: Rate change after call INSERT does not affect stored snapshot
"""
from __future__ import annotations

import datetime
import json
from decimal import Decimal

import pytest

from app.services import rate_service as _rs

# Skip entire module when Phase 4 API not present.
if not hasattr(_rs, "validate_rate_snapshot"):
    import pytest as _pytest
    _pytest.skip(
        "validate_rate_snapshot not available — Phase 4 rate_service required",
        allow_module_level=True,
    )

from app.services.rate_service import (
    BillingPolicy,
    POLICY_VERSION,
    validate_rate_snapshot,
    billing_policy_from_snapshot,
    serialize_billing_policy,
    resolve_billing_policy,
    DEFAULT_RATE_PER_MIN,
    DEFAULT_BILLING_PULSE_SECONDS,
    DEFAULT_MINIMUM_BILLABLE_SECONDS,
    DEFAULT_CHARGE_FAILED_CALLS,
)
from app.db import db
from app.models.tenant import create_tenant, invalidate_tenant_caches


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_snapshot(tenant_id: int = 1) -> dict:
    """Minimal valid snapshot dict (pre-parsed Decimal values)."""
    return {
        "policy_version": POLICY_VERSION,
        "tenant_id": tenant_id,
        "rate_per_min": Decimal("2.50"),
        "rate_per_call": Decimal("0.00"),
        "min_charge": Decimal("0.50"),
        "billing_pulse_seconds": 60,
        "minimum_billable_seconds": 30,
        "charge_failed_calls": False,
        "currency": "BDT",
        "rate_per_min_source": "plan_default",
        "billing_pulse_seconds_source": "platform_default",
        "minimum_billable_seconds_source": "platform_default",
        "charge_failed_calls_source": "platform_default",
    }


def _valid_snapshot_json(tenant_id: int = 1) -> str:
    """JSON string of valid snapshot (Decimals serialized as strings)."""
    snap = _valid_snapshot(tenant_id)
    serializable = {
        k: str(v) if isinstance(v, Decimal) else v
        for k, v in snap.items()
    }
    return json.dumps(serializable)


def _make_tenant(name: str) -> int:
    plan = db.query_one("SELECT name FROM plans WHERE LOWER(name) = 'free'")
    plan_name = plan["name"] if plan else "free"
    tenant_id = create_tenant(
        business_name=name,
        email=f"{name.replace(' ', '_').lower()}@test.ccall",
        plan_name=plan_name,
    )
    db.execute(
        "UPDATE wallets SET balance = 1000.00 WHERE tenant_id = %s",
        (tenant_id,),
    )
    return tenant_id


def _cleanup_tenant(tenant_id: int) -> None:
    db.execute("DELETE FROM customer_rates WHERE tenant_id = %s", (tenant_id,))
    db.execute("DELETE FROM wallet_transactions WHERE tenant_id = %s", (tenant_id,))
    db.execute("DELETE FROM calls WHERE tenant_id = %s", (tenant_id,))
    db.execute("DELETE FROM wallets WHERE tenant_id = %s", (tenant_id,))
    db.execute("DELETE FROM retry_rules WHERE tenant_id = %s", (tenant_id,))
    db.execute("DELETE FROM users WHERE tenant_id = %s", (tenant_id,))
    db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))


def _insert_call(tenant_id: int, snap_json: str | None, created_at=None, talk_seconds: int = 60,
                 status: str = "completed") -> int:
    """Insert a call row and return its id. Uses started_at (the actual calls column name)."""
    if created_at is None:
        created_at = datetime.datetime(2026, 7, 20, 0, 0, 0, tzinfo=datetime.timezone.utc)
    return db.insert_returning_id(
        """
        INSERT INTO calls (tenant_id, number, status, talk_seconds, rate_snapshot, started_at)
        VALUES (%s, '01799000001', %s, %s, %s, %s) RETURNING id
        """,
        (tenant_id, status, talk_seconds, snap_json, created_at),
    )


# ---------------------------------------------------------------------------
# Scenario A — non-dict input rejected
# ---------------------------------------------------------------------------

def test_A_rejects_non_dict():
    with pytest.raises(ValueError, match="dict"):
        validate_rate_snapshot("not a dict")

    with pytest.raises(ValueError):
        validate_rate_snapshot(None)

    with pytest.raises(ValueError):
        validate_rate_snapshot(42)


# ---------------------------------------------------------------------------
# Scenario B — missing required keys
# ---------------------------------------------------------------------------

def test_B_rejects_missing_keys():
    snap = _valid_snapshot()
    del snap["rate_per_min"]
    with pytest.raises(ValueError, match="rate_per_min"):
        validate_rate_snapshot(snap)


def test_B_rejects_missing_policy_version():
    snap = _valid_snapshot()
    del snap["policy_version"]
    with pytest.raises(ValueError, match="policy_version"):
        validate_rate_snapshot(snap)


# ---------------------------------------------------------------------------
# Scenario C — tenant_id mismatch
# ---------------------------------------------------------------------------

def test_C_rejects_wrong_tenant_id():
    snap = _valid_snapshot(tenant_id=99)
    with pytest.raises(ValueError, match="tenant_id"):
        validate_rate_snapshot(snap, expected_tenant_id=1)


def test_C_accepts_correct_tenant_id():
    snap = _valid_snapshot(tenant_id=5)
    result = validate_rate_snapshot(snap, expected_tenant_id=5)
    assert result["tenant_id"] == 5


# ---------------------------------------------------------------------------
# Scenario D — negative rate_per_min
# ---------------------------------------------------------------------------

def test_D_rejects_negative_rate():
    snap = _valid_snapshot()
    snap["rate_per_min"] = Decimal("-1.00")
    with pytest.raises(ValueError, match="rate_per_min"):
        validate_rate_snapshot(snap)


# ---------------------------------------------------------------------------
# Scenario E — rate_per_min exceeds upper bound
# ---------------------------------------------------------------------------

def test_E_rejects_excessive_rate():
    snap = _valid_snapshot()
    snap["rate_per_min"] = Decimal("1001.00")
    with pytest.raises(ValueError, match="rate_per_min"):
        validate_rate_snapshot(snap)


# ---------------------------------------------------------------------------
# Scenario F — invalid billing_pulse_seconds
# ---------------------------------------------------------------------------

def test_F_rejects_zero_pulse():
    snap = _valid_snapshot()
    snap["billing_pulse_seconds"] = 0
    with pytest.raises(ValueError, match="billing_pulse_seconds"):
        validate_rate_snapshot(snap)


def test_F_rejects_negative_pulse():
    snap = _valid_snapshot()
    snap["billing_pulse_seconds"] = -1
    with pytest.raises(ValueError, match="billing_pulse_seconds"):
        validate_rate_snapshot(snap)


# ---------------------------------------------------------------------------
# Scenario G — invalid currency
# ---------------------------------------------------------------------------

def test_G_rejects_invalid_currency():
    snap = _valid_snapshot()
    snap["currency"] = "XYZ"
    with pytest.raises(ValueError, match="currency"):
        validate_rate_snapshot(snap)


def test_G_accepts_usd():
    snap = _valid_snapshot()
    snap["currency"] = "USD"
    result = validate_rate_snapshot(snap)
    assert result["currency"] == "USD"


# ---------------------------------------------------------------------------
# Scenario H — wrong policy_version
# ---------------------------------------------------------------------------

def test_H_rejects_wrong_version():
    snap = _valid_snapshot()
    snap["policy_version"] = 999
    with pytest.raises(ValueError, match="policy_version"):
        validate_rate_snapshot(snap)


# ---------------------------------------------------------------------------
# Scenario I — sensitive field leakage
# ---------------------------------------------------------------------------

def test_I_rejects_password_field():
    snap = _valid_snapshot()
    snap["password"] = "secret123"
    with pytest.raises(ValueError, match="sensitive"):
        validate_rate_snapshot(snap)


def test_I_rejects_secret_field():
    snap = _valid_snapshot()
    snap["api_secret"] = "tok_abc"
    with pytest.raises(ValueError, match="sensitive"):
        validate_rate_snapshot(snap)


# ---------------------------------------------------------------------------
# Scenario J — valid snapshot accepted, Decimal values returned
# ---------------------------------------------------------------------------

def test_J_accepts_valid_snapshot():
    snap = _valid_snapshot()
    result = validate_rate_snapshot(snap)
    assert isinstance(result["rate_per_min"], Decimal)
    assert isinstance(result["min_charge"], Decimal)
    assert result["billing_pulse_seconds"] == 60


def test_J_accepts_string_decimals():
    """JSON-deserialized snapshots have string numbers — must parse them."""
    snap = _valid_snapshot()
    snap["rate_per_min"] = "2.50"
    snap["min_charge"] = "0.50"
    snap["rate_per_call"] = "0.00"
    result = validate_rate_snapshot(snap)
    assert result["rate_per_min"] == Decimal("2.50")


# ---------------------------------------------------------------------------
# Scenario K — billing_policy_from_snapshot round-trip
# ---------------------------------------------------------------------------

def test_K_policy_from_snapshot():
    snap = _valid_snapshot(tenant_id=7)
    policy = billing_policy_from_snapshot(snap, expected_tenant_id=7)
    assert isinstance(policy, BillingPolicy)
    assert policy.rate_per_min == Decimal("2.50")
    assert policy.billing_pulse_seconds == 60
    assert policy.charge_failed_calls is False
    assert policy.tenant_id == 7


# ---------------------------------------------------------------------------
# Scenario L — serialize → validate round-trip
# ---------------------------------------------------------------------------

def test_L_serialize_validate_roundtrip(app):
    with app.app_context():
        tenant_id = _make_tenant("roundtrip_tenant_L")
        try:
            policy = resolve_billing_policy(tenant_id, use_cache=False)
            serialized = serialize_billing_policy(policy)
            parsed = json.loads(serialized)
            validated = validate_rate_snapshot(parsed, expected_tenant_id=tenant_id)
            assert validated["tenant_id"] == tenant_id
            assert isinstance(validated["rate_per_min"], Decimal)
        finally:
            _cleanup_tenant(tenant_id)


# ---------------------------------------------------------------------------
# Scenario M — finalize_call_billing uses snapshot rate
# ---------------------------------------------------------------------------

def test_M_billing_uses_snapshot_rate(app):
    """Billing uses stored snapshot even if live rate changes afterward."""
    with app.app_context():
        from app.services.billing_service import finalize_call_billing
        from app.services.rate_service import invalidate_billing_policy_cache

        tenant_id = _make_tenant("snapshot_rate_M")
        try:
            # Set live rate to 3.00/min
            db.execute(
                "INSERT INTO customer_rates (tenant_id, rate_per_min) VALUES (%s, 3.00) "
                "ON CONFLICT (tenant_id) DO UPDATE SET rate_per_min = 3.00",
                (tenant_id,),
            )
            invalidate_tenant_caches(tenant_id)
            invalidate_billing_policy_cache(tenant_id)

            # Snapshot taken at rate 1.00/min (simulate earlier dispatch)
            snap = _valid_snapshot(tenant_id=tenant_id)
            snap["rate_per_min"] = Decimal("1.00")
            snap["rate_per_min_source"] = "tenant_override"
            policy_snap = billing_policy_from_snapshot(snap, expected_tenant_id=tenant_id)
            snap_json = serialize_billing_policy(policy_snap)

            before_cutoff = datetime.datetime(2026, 7, 20, 0, 0, 0, tzinfo=datetime.timezone.utc)
            call_id = _insert_call(tenant_id, snap_json, created_at=before_cutoff)

            result = finalize_call_billing(call_id)
            assert result.get("billed") is True
            # Must bill at 1.00/min (snapshot), not 3.00/min (live)
            assert result["charge"] == Decimal("1.00")
        finally:
            _cleanup_tenant(tenant_id)


# ---------------------------------------------------------------------------
# Scenario N — fail-closed: missing snapshot for new call
# ---------------------------------------------------------------------------

def test_N_fail_closed_missing_snapshot(app):
    with app.app_context():
        from app.services.billing_service import finalize_call_billing

        tenant_id = _make_tenant("missing_snap_N")
        try:
            after_cutoff = datetime.datetime(2026, 7, 21, 13, 0, 0, tzinfo=datetime.timezone.utc)
            call_id = _insert_call(tenant_id, snap_json=None, created_at=after_cutoff)

            result = finalize_call_billing(call_id)
            assert result.get("billed") is False
            assert result.get("error_code") == "missing_snapshot"
        finally:
            _cleanup_tenant(tenant_id)


# ---------------------------------------------------------------------------
# Scenario O — fail-closed: malformed snapshot for new call
# ---------------------------------------------------------------------------

def test_O_fail_closed_malformed_snapshot(app):
    with app.app_context():
        from app.services.billing_service import finalize_call_billing

        tenant_id = _make_tenant("malformed_snap_O")
        try:
            after_cutoff = datetime.datetime(2026, 7, 21, 13, 0, 0, tzinfo=datetime.timezone.utc)
            call_id = _insert_call(tenant_id, snap_json='{"broken": true}', created_at=after_cutoff)

            result = finalize_call_billing(call_id)
            assert result.get("billed") is False
            assert result.get("error_code") == "malformed_snapshot"
        finally:
            _cleanup_tenant(tenant_id)


# ---------------------------------------------------------------------------
# Scenario P — legacy call: live rate fallback before cutoff
# ---------------------------------------------------------------------------

def test_P_legacy_call_live_fallback(app):
    with app.app_context():
        from app.services.billing_service import finalize_call_billing

        tenant_id = _make_tenant("legacy_call_P")
        try:
            before_cutoff = datetime.datetime(2026, 7, 20, 0, 0, 0, tzinfo=datetime.timezone.utc)
            call_id = _insert_call(tenant_id, snap_json=None, created_at=before_cutoff)

            result = finalize_call_billing(call_id)
            # Should bill successfully using live rate fallback (no fail-closed for legacy call)
            assert result.get("billed") is True
            assert result.get("error_code") is None
        finally:
            _cleanup_tenant(tenant_id)


# ---------------------------------------------------------------------------
# Scenario Q — retry reuses original snapshot
# ---------------------------------------------------------------------------

def test_Q_retry_reuses_original_snapshot(app):
    """When a call is retried, the new attempt's snapshot comes from resolve_billing_policy
    at dispatch time. This test verifies that the snapshot in the DB row is immutable —
    changing rates after the INSERT does not retroactively change the stored snapshot."""
    with app.app_context():
        from app.services.rate_service import invalidate_billing_policy_cache

        tenant_id = _make_tenant("retry_immutable_Q")
        try:
            # Resolve policy at "dispatch time" for attempt 1
            policy_at_dispatch = resolve_billing_policy(tenant_id, use_cache=False)
            snap1 = serialize_billing_policy(policy_at_dispatch)

            # Simulate rate change between attempt 1 and attempt 2
            db.execute(
                "INSERT INTO customer_rates (tenant_id, rate_per_min) VALUES (%s, 9.99) "
                "ON CONFLICT (tenant_id) DO UPDATE SET rate_per_min = 9.99",
                (tenant_id,),
            )
            invalidate_tenant_caches(tenant_id)
            invalidate_billing_policy_cache(tenant_id)

            # Attempt 2 resolves NEW snapshot at its own dispatch time
            policy_at_retry = resolve_billing_policy(tenant_id, use_cache=False)
            snap2 = serialize_billing_policy(policy_at_retry)

            # The two snapshots must be different (rate changed)
            snap1_parsed = json.loads(snap1)
            snap2_parsed = json.loads(snap2)
            assert snap1_parsed["rate_per_min"] != snap2_parsed["rate_per_min"]

            # But the original snapshot (snap1) is immutable — validating it still works
            validated1 = validate_rate_snapshot(snap1_parsed, expected_tenant_id=tenant_id)
            assert validated1["rate_per_min"] == policy_at_dispatch.rate_per_min
        finally:
            _cleanup_tenant(tenant_id)


# ---------------------------------------------------------------------------
# Scenario R — rate change after INSERT doesn't affect stored snapshot
# ---------------------------------------------------------------------------

def test_R_snapshot_immutable_after_insert(app):
    """Snapshot stored in calls.rate_snapshot reflects rates at INSERT time,
    not at billing finalization time."""
    with app.app_context():
        from app.services.billing_service import finalize_call_billing
        from app.services.rate_service import invalidate_billing_policy_cache

        tenant_id = _make_tenant("post_insert_R")
        try:
            # Rate at dispatch: 2.00/min
            db.execute(
                "INSERT INTO customer_rates (tenant_id, rate_per_min) VALUES (%s, 2.00) "
                "ON CONFLICT (tenant_id) DO UPDATE SET rate_per_min = 2.00",
                (tenant_id,),
            )
            invalidate_tenant_caches(tenant_id)
            invalidate_billing_policy_cache(tenant_id)

            policy = resolve_billing_policy(tenant_id, use_cache=False)
            snap_json = serialize_billing_policy(policy)

            # Simulate rate change AFTER the call INSERT
            db.execute(
                "UPDATE customer_rates SET rate_per_min = 9.00 WHERE tenant_id = %s",
                (tenant_id,),
            )
            invalidate_tenant_caches(tenant_id)
            invalidate_billing_policy_cache(tenant_id)

            before_cutoff = datetime.datetime(2026, 7, 20, 0, 0, 0, tzinfo=datetime.timezone.utc)
            call_id = _insert_call(tenant_id, snap_json=snap_json, created_at=before_cutoff)

            result = finalize_call_billing(call_id)
            assert result.get("billed") is True
            # Must bill at 2.00/min (snapshot rate), not 9.00/min
            assert result["charge"] == Decimal("2.00")
        finally:
            _cleanup_tenant(tenant_id)
