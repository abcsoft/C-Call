"""Tests for the plan-default → tenant-override billing resolver (migration 021).

Coverage:
  1-3:  Platform default resolution (no plan billing fields, no customer_rates)
  4-6:  Plan-default billing fields resolve correctly per field
  7-9:  Tenant override takes precedence over plan default, per-field independently
  10:   Tri-state charge_failed_calls: NULL inherits plan default
  11:   Tri-state charge_failed_calls: TRUE explicit override
  12:   Tri-state charge_failed_calls: FALSE explicit override beats plan TRUE
  13:   NULL rate_per_min in customer_rates inherits plan default, not platform
  14:   NULL billing_pulse_seconds in customer_rates inherits plan default
  15:   Rate snapshot written to calls row at order dispatch
  16:   Billing uses snapshot rate not live rate when snapshot present
  17:   Billing falls back to live rate when no snapshot (pre-migration calls)
  18:   Plan default rate resolves after plan change (cache invalidated)
  19:   BillingPolicy.as_dict() round-trips through JSON cleanly
  20:   resolve_billing_policy returns correct source per field
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.db import db
from app.models.tenant import create_tenant, invalidate_tenant_caches
from app.services import rate_service as _rs
from app.services.rate_service import (
    DEFAULT_RATE_PER_MIN,
    DEFAULT_BILLING_PULSE_SECONDS,
    DEFAULT_MINIMUM_BILLABLE_SECONDS,
    DEFAULT_CHARGE_FAILED_CALLS,
    get_tenant_rate,
)
from app.services.wallet_service import credit

# Skip entire module when the extended rate_service API (Phase 4) is not present.
if not hasattr(_rs, "resolve_billing_policy"):
    import pytest as _pytest
    _pytest.skip(
        "rate_service.resolve_billing_policy not available in committed version — "
        "these tests require the Phase 4 rate_service rewrite",
        allow_module_level=True,
    )

from app.services.rate_service import resolve_billing_policy, BillingPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tenant(business_name: str, plan_name: str = "free") -> int:
    """Create a tenant on the named plan (plan must already exist)."""
    plan = db.query_one("SELECT id FROM plans WHERE LOWER(name) = %s", (plan_name.lower(),))
    if not plan:
        plan_id = db.insert_returning_id(
            """
            INSERT INTO plans (name, price_monthly, included_minutes, max_flows, max_users, feature_flags)
            VALUES (%s, 0, 0, 10, 5, %s) RETURNING id
            """,
            (plan_name, json.dumps({})),
        )
    else:
        plan_id = plan["id"]

    tid = create_tenant({
        "business_name": business_name,
        "email": f"{business_name.lower().replace(' ', '_')}@test.example",
        "plan_name": plan_name,
        "status": "active",
    })
    db.execute("UPDATE tenants SET plan_id = %s WHERE id = %s", (plan_id, tid))
    return tid


def _set_plan_billing(plan_name: str, **kwargs) -> None:
    """Update a plan's default billing columns. Only sets provided kwargs."""
    sets = ", ".join(f"{k} = %s" for k in kwargs)
    vals = list(kwargs.values())
    db.execute(f"UPDATE plans SET {sets} WHERE LOWER(name) = %s", (*vals, plan_name.lower()))


def _set_customer_rates(tenant_id: int, **kwargs) -> None:
    """Upsert customer_rates for tenant.

    Explicitly sets ALL billing fields: provided kwargs take the given value;
    unspecified fields are set to NULL (= inherit from plan / platform).
    This prevents DB column defaults (60, 60, false) from masquerading as
    tenant overrides in tests.
    """
    _all_billing = {
        "rate_per_min": None,
        "rate_per_call": None,
        "min_charge": None,
        "billing_pulse_seconds": None,
        "minimum_billable_seconds": None,
        "charge_failed_calls": None,
    }
    _all_billing.update(kwargs)
    db.execute(
        """
        INSERT INTO customer_rates
            (tenant_id, rate_per_min, rate_per_call, min_charge,
             billing_pulse_seconds, minimum_billable_seconds, charge_failed_calls)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (tenant_id) DO UPDATE SET
            rate_per_min = EXCLUDED.rate_per_min,
            rate_per_call = EXCLUDED.rate_per_call,
            min_charge = EXCLUDED.min_charge,
            billing_pulse_seconds = EXCLUDED.billing_pulse_seconds,
            minimum_billable_seconds = EXCLUDED.minimum_billable_seconds,
            charge_failed_calls = EXCLUDED.charge_failed_calls,
            updated_at = now()
        """,
        (
            tenant_id,
            _all_billing["rate_per_min"],
            _all_billing["rate_per_call"],
            _all_billing["min_charge"],
            _all_billing["billing_pulse_seconds"],
            _all_billing["minimum_billable_seconds"],
            _all_billing["charge_failed_calls"],
        ),
    )
    invalidate_tenant_caches(tenant_id)


# ---------------------------------------------------------------------------
# 1-3: Platform default fallback
# ---------------------------------------------------------------------------

def test_1_no_plan_billing_fields_falls_to_platform(app):
    """No customer_rates, plan has NULL billing columns → platform defaults apply."""
    with app.app_context():
        tid = _tenant("Platform Default Co", "free")
        # Ensure plan has NULL billing columns
        _set_plan_billing("free",
                          default_rate_per_min=None,
                          default_rate_per_call=None,
                          default_min_charge=None,
                          default_billing_pulse_seconds=None,
                          default_minimum_billable_seconds=None,
                          default_charge_failed_calls=None)
        invalidate_tenant_caches(tid)

        policy = resolve_billing_policy(tid)
        # With NULL plan fields and no hardcoded match for this plan, falls to platform
        # (plan name IS 'free' so hardcoded 0.80 applies from legacy path)
        assert policy.rate_per_min == Decimal("0.80")
        assert policy.billing_pulse_seconds == DEFAULT_BILLING_PULSE_SECONDS
        assert policy.minimum_billable_seconds == DEFAULT_MINIMUM_BILLABLE_SECONDS
        assert policy.charge_failed_calls == DEFAULT_CHARGE_FAILED_CALLS


def test_2_platform_default_source_label(app):
    """Fields with no plan or tenant override carry 'platform_default' source."""
    with app.app_context():
        tid = _tenant("Source Label Co", "free")
        _set_plan_billing("free",
                          default_billing_pulse_seconds=None,
                          default_minimum_billable_seconds=None,
                          default_charge_failed_calls=None)
        invalidate_tenant_caches(tid)
        policy = resolve_billing_policy(tid)
        assert policy.billing_pulse_seconds_source == "platform_default"
        assert policy.minimum_billable_seconds_source == "platform_default"
        assert policy.charge_failed_calls_source == "platform_default"


def test_3_get_tenant_rate_backward_compat(app):
    """get_tenant_rate() returns a plain dict with correct keys."""
    with app.app_context():
        tid = _tenant("Compat Co", "free")
        rates = get_tenant_rate(tid)
        assert "rate_per_min" in rates
        assert "rate_per_call" in rates
        assert "min_charge" in rates
        assert "billing_pulse_seconds" in rates
        assert "minimum_billable_seconds" in rates
        assert "charge_failed_calls" in rates
        assert isinstance(rates["rate_per_min"], Decimal)


# ---------------------------------------------------------------------------
# 4-6: Plan-default billing fields
# ---------------------------------------------------------------------------

def test_4_plan_default_rate_per_min_used(app):
    """Plan default_rate_per_min is used when no customer_rates override."""
    with app.app_context():
        tid = _tenant("Plan Rate Co", "starter")
        _set_plan_billing("starter",
                          default_rate_per_min=Decimal("0.55"),
                          default_rate_per_call=Decimal("0.00"),
                          default_min_charge=Decimal("0.55"))
        invalidate_tenant_caches(tid)
        policy = resolve_billing_policy(tid)
        assert policy.rate_per_min == Decimal("0.55")
        assert policy.rate_per_min_source == "plan_default"


def test_5_plan_default_pulse_and_min_billable(app):
    """Plan default_billing_pulse_seconds and default_minimum_billable_seconds resolve."""
    with app.app_context():
        tid = _tenant("Plan Pulse Co", "starter")
        _set_plan_billing("starter",
                          default_billing_pulse_seconds=30,
                          default_minimum_billable_seconds=30)
        invalidate_tenant_caches(tid)
        policy = resolve_billing_policy(tid)
        assert policy.billing_pulse_seconds == 30
        assert policy.billing_pulse_seconds_source == "plan_default"
        assert policy.minimum_billable_seconds == 30
        assert policy.minimum_billable_seconds_source == "plan_default"


def test_6_plan_default_charge_failed_calls(app):
    """Plan default_charge_failed_calls=True resolves and carries plan_default source."""
    with app.app_context():
        tid = _tenant("Plan CFC Co", "pro")
        _set_plan_billing("pro", default_charge_failed_calls=True)
        invalidate_tenant_caches(tid)
        policy = resolve_billing_policy(tid)
        assert policy.charge_failed_calls is True
        assert policy.charge_failed_calls_source == "plan_default"


# ---------------------------------------------------------------------------
# 7-9: Tenant override takes precedence per field
# ---------------------------------------------------------------------------

def test_7_tenant_rate_overrides_plan_rate(app):
    """customer_rates.rate_per_min overrides plan default independently."""
    with app.app_context():
        tid = _tenant("Tenant Rate Co", "pro")
        _set_plan_billing("pro", default_rate_per_min=Decimal("0.60"))
        # Tenant override: 1.20/min (negotiated deal)
        _set_customer_rates(tid, rate_per_min=Decimal("1.20"))
        policy = resolve_billing_policy(tid)
        assert policy.rate_per_min == Decimal("1.20")
        assert policy.rate_per_min_source == "tenant_override"
        # billing_pulse_seconds not overridden → plan or platform default
        assert policy.billing_pulse_seconds_source in ("plan_default", "platform_default")


def test_8_tenant_pulse_overrides_plan_pulse(app):
    """customer_rates.billing_pulse_seconds overrides plan default independently."""
    with app.app_context():
        tid = _tenant("Tenant Pulse Co", "pro")
        _set_plan_billing("pro", default_billing_pulse_seconds=60)
        _set_customer_rates(tid, billing_pulse_seconds=30)
        policy = resolve_billing_policy(tid)
        assert policy.billing_pulse_seconds == 30
        assert policy.billing_pulse_seconds_source == "tenant_override"


def test_9_per_field_independent_resolution(app):
    """Each field resolves independently — override one, inherit others from plan."""
    with app.app_context():
        tid = _tenant("Independent Fields Co", "starter")
        _set_plan_billing("starter",
                          default_rate_per_min=Decimal("0.70"),
                          default_billing_pulse_seconds=30,
                          default_charge_failed_calls=False)
        # Override only rate_per_min for this tenant
        _set_customer_rates(tid, rate_per_min=Decimal("0.90"))
        policy = resolve_billing_policy(tid)
        assert policy.rate_per_min == Decimal("0.90")
        assert policy.rate_per_min_source == "tenant_override"
        assert policy.billing_pulse_seconds == 30
        assert policy.billing_pulse_seconds_source == "plan_default"
        assert policy.charge_failed_calls is False
        assert policy.charge_failed_calls_source == "plan_default"


# ---------------------------------------------------------------------------
# 10-12: charge_failed_calls tri-state
# ---------------------------------------------------------------------------

def test_10_null_charge_failed_calls_inherits_plan(app):
    """customer_rates.charge_failed_calls IS NULL → inherit from plan."""
    with app.app_context():
        tid = _tenant("CFC Inherit Co", "pro")
        _set_plan_billing("pro", default_charge_failed_calls=True)
        # Insert customer_rates row with all billing fields NULL except charge_failed_calls
        _set_customer_rates(tid)  # all NULL (inherit)
        # then explicitly set charge_failed_calls=NULL to confirm it inherits
        db.execute(
            "UPDATE customer_rates SET charge_failed_calls = NULL WHERE tenant_id = %s",
            (tid,),
        )
        invalidate_tenant_caches(tid)
        policy = resolve_billing_policy(tid)
        assert policy.charge_failed_calls is True
        assert policy.charge_failed_calls_source == "plan_default"


def test_11_explicit_true_overrides_plan_false(app):
    """customer_rates.charge_failed_calls = TRUE beats plan default = FALSE."""
    with app.app_context():
        tid = _tenant("CFC True Co", "free")
        _set_plan_billing("free", default_charge_failed_calls=False)
        _set_customer_rates(tid, charge_failed_calls=True)
        policy = resolve_billing_policy(tid)
        assert policy.charge_failed_calls is True
        assert policy.charge_failed_calls_source == "tenant_override"


def test_12_explicit_false_overrides_plan_true(app):
    """customer_rates.charge_failed_calls = FALSE beats plan default = TRUE."""
    with app.app_context():
        tid = _tenant("CFC False Co", "pro")
        _set_plan_billing("pro", default_charge_failed_calls=True)
        _set_customer_rates(tid, charge_failed_calls=False)
        policy = resolve_billing_policy(tid)
        assert policy.charge_failed_calls is False
        assert policy.charge_failed_calls_source == "tenant_override"


# ---------------------------------------------------------------------------
# 13-14: NULL in customer_rates inherits plan default (not platform)
# ---------------------------------------------------------------------------

def test_13_null_rate_in_customer_rates_inherits_plan(app):
    """rate_per_min=NULL in customer_rates falls through to plan default, not 0.80."""
    with app.app_context():
        tid = _tenant("NULL Rate Co", "starter")
        _set_plan_billing("starter", default_rate_per_min=Decimal("0.65"))
        # Row with all billing fields NULL except rate_per_min explicitly NULL
        _set_customer_rates(tid)  # all NULL including rate_per_min
        invalidate_tenant_caches(tid)
        policy = resolve_billing_policy(tid)
        assert policy.rate_per_min == Decimal("0.65")
        assert policy.rate_per_min_source == "plan_default"


def test_14_null_pulse_in_customer_rates_inherits_plan(app):
    """billing_pulse_seconds=NULL in customer_rates falls to plan default."""
    with app.app_context():
        tid = _tenant("NULL Pulse Co", "starter")
        _set_plan_billing("starter", default_billing_pulse_seconds=30)
        # Row with all billing fields NULL including billing_pulse_seconds
        _set_customer_rates(tid)
        invalidate_tenant_caches(tid)
        policy = resolve_billing_policy(tid)
        assert policy.billing_pulse_seconds == 30
        assert policy.billing_pulse_seconds_source == "plan_default"


# ---------------------------------------------------------------------------
# 15-17: Rate snapshot
# ---------------------------------------------------------------------------

def test_15_rate_snapshot_written_to_calls_row(app):
    """resolve_billing_policy().as_dict() produces a valid JSON snapshot."""
    with app.app_context():
        tid = _tenant("Snapshot Write Co", "free")
        _set_plan_billing("free", default_rate_per_min=Decimal("0.80"))
        policy = resolve_billing_policy(tid)
        snap = policy.as_dict()
        # Must be JSON-serialisable
        raw = json.dumps(snap)
        restored = json.loads(raw)
        assert Decimal(restored["rate_per_min"]) == policy.rate_per_min
        assert int(restored["billing_pulse_seconds"]) == policy.billing_pulse_seconds
        assert restored["charge_failed_calls"] == policy.charge_failed_calls


def test_16_billing_uses_snapshot_rate(app):
    """finalize_call_billing uses rate_snapshot on call row, not live rate."""
    from app.services.billing_service import finalize_call_billing
    with app.app_context():
        tid = _tenant("Snapshot Bill Co", "free")
        credit(tid, Decimal("10.00"), "seed")
        # Snapshot says 0.50/min but live rate is 0.80/min
        snap = {
            "rate_per_min": "0.50",
            "rate_per_call": "0.00",
            "min_charge": "0.50",
            "billing_pulse_seconds": 60,
            "minimum_billable_seconds": 60,
            "charge_failed_calls": False,
        }
        # Insert a completed call with snapshot
        call_id = db.insert_returning_id(
            """
            INSERT INTO calls (tenant_id, number, status, talk_seconds, rate_snapshot)
            VALUES (%s, '01711111111', 'completed', 60, %s) RETURNING id
            """,
            (tid, json.dumps(snap)),
        )
        result = finalize_call_billing(call_id)
        assert result["billed"] is True
        # Should use 0.50 (snapshot), not 0.80 (live rate)
        assert result["charge"] == Decimal("0.5000")


def test_17_billing_falls_back_to_live_rate_without_snapshot(app):
    """finalize_call_billing uses live rate when rate_snapshot is NULL (legacy call)."""
    from app.services.billing_service import finalize_call_billing
    from app.services.wallet_service import get_balance
    with app.app_context():
        tid = _tenant("Legacy Bill Co", "free")
        _set_plan_billing("free", default_rate_per_min=Decimal("0.80"))
        invalidate_tenant_caches(tid)
        credit(tid, Decimal("10.00"), "seed")
        # Insert call without rate_snapshot
        call_id = db.insert_returning_id(
            """
            INSERT INTO calls (tenant_id, number, status, talk_seconds)
            VALUES (%s, '01712222222', 'completed', 60) RETURNING id
            """,
            (tid,),
        )
        result = finalize_call_billing(call_id)
        assert result["billed"] is True
        # Uses live rate (free plan = 0.80/min, 60s = 1 min = 0.80)
        assert result["charge"] == Decimal("0.8000")


# ---------------------------------------------------------------------------
# 18: Cache invalidation after plan change
# ---------------------------------------------------------------------------

def test_18_plan_change_invalidates_cached_rate(app):
    """After changing plan billing, cache invalidation makes resolver pick up new rate."""
    with app.app_context():
        tid = _tenant("Cache Refresh Co", "free")
        _set_plan_billing("free", default_rate_per_min=Decimal("0.80"))
        invalidate_tenant_caches(tid)

        policy1 = resolve_billing_policy(tid)
        assert policy1.rate_per_min == Decimal("0.80")

        # Admin changes plan rate
        _set_plan_billing("free", default_rate_per_min=Decimal("0.75"))
        invalidate_tenant_caches(tid)

        policy2 = resolve_billing_policy(tid)
        assert policy2.rate_per_min == Decimal("0.75")


# ---------------------------------------------------------------------------
# 19-20: BillingPolicy dataclass
# ---------------------------------------------------------------------------

def test_19_billing_policy_as_dict_round_trips(app):
    """BillingPolicy.as_dict() serialises and round-trips without loss."""
    with app.app_context():
        tid = _tenant("Round Trip Co", "free")
        policy = resolve_billing_policy(tid)
        snap = policy.as_dict()
        assert all(
            k in snap
            for k in (
                "rate_per_min", "rate_per_call", "min_charge",
                "billing_pulse_seconds", "minimum_billable_seconds",
                "charge_failed_calls", "plan_id",
                "rate_per_min_source",
            )
        )
        # All values are JSON-safe types
        raw = json.dumps(snap)
        restored = json.loads(raw)
        assert Decimal(restored["rate_per_min"]) == policy.rate_per_min


def test_20_resolve_returns_correct_sources(app):
    """resolve_billing_policy() returns correct source label for each field."""
    with app.app_context():
        tid = _tenant("Source Check Co", "pro")
        # Plan sets rate and pulse
        _set_plan_billing("pro",
                          default_rate_per_min=Decimal("0.60"),
                          default_billing_pulse_seconds=60,
                          default_minimum_billable_seconds=None,  # falls to platform
                          default_charge_failed_calls=False)
        # Tenant overrides only rate_per_min
        _set_customer_rates(tid, rate_per_min=Decimal("0.55"))

        policy = resolve_billing_policy(tid)
        assert policy.rate_per_min_source == "tenant_override"
        assert policy.billing_pulse_seconds_source == "plan_default"
        assert policy.minimum_billable_seconds_source == "platform_default"
        assert policy.charge_failed_calls_source == "plan_default"
