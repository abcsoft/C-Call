from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.db import db

# Platform-wide defaults (last resort — no plan, no tenant override)
DEFAULT_RATE_PER_MIN = Decimal("0.80")
DEFAULT_RATE_PER_CALL = Decimal("0.00")
DEFAULT_MIN_CHARGE = Decimal("0.80")
DEFAULT_BILLING_PULSE_SECONDS = 60
DEFAULT_MINIMUM_BILLABLE_SECONDS = 60
DEFAULT_CHARGE_FAILED_CALLS = False

Source = Literal["tenant_override", "plan_default", "platform_default"]


@dataclass(frozen=True)
class BillingPolicy:
    """Per-field resolved billing policy with source tracking. Immutable after construction."""

    rate_per_min: Decimal
    rate_per_min_source: Source

    rate_per_call: Decimal
    rate_per_call_source: Source

    min_charge: Decimal
    min_charge_source: Source

    billing_pulse_seconds: int
    billing_pulse_seconds_source: Source

    minimum_billable_seconds: int
    minimum_billable_seconds_source: Source

    charge_failed_calls: bool
    # None in customer_rates → not set for this tenant (inherit from plan/platform).
    # True/False → explicit tenant override (even if it matches plan/platform value).
    charge_failed_calls_source: Source

    plan_id: int | None = None
    currency: str = "BDT"
    tenant_id: int | None = None
    policy_version: int = 1
    resolved_at: str | None = None

    def as_dict(self) -> dict:
        """Serialisable dict for rate_snapshot storage."""
        return {
            "rate_per_min": str(self.rate_per_min),
            "rate_per_call": str(self.rate_per_call),
            "min_charge": str(self.min_charge),
            "billing_pulse_seconds": self.billing_pulse_seconds,
            "minimum_billable_seconds": self.minimum_billable_seconds,
            "charge_failed_calls": self.charge_failed_calls,
            "plan_id": self.plan_id,
            "currency": self.currency,
            "tenant_id": self.tenant_id,
            "policy_version": self.policy_version,
            "resolved_at": self.resolved_at,
            "rate_per_min_source": self.rate_per_min_source,
            "rate_per_call_source": self.rate_per_call_source,
            "min_charge_source": self.min_charge_source,
            "billing_pulse_seconds_source": self.billing_pulse_seconds_source,
            "minimum_billable_seconds_source": self.minimum_billable_seconds_source,
            "charge_failed_calls_source": self.charge_failed_calls_source,
        }

    def rates_dict(self) -> dict:
        """Legacy-compat dict consumed by calculate_charge_and_duration."""
        return {
            "rate_per_min": self.rate_per_min,
            "rate_per_call": self.rate_per_call,
            "min_charge": self.min_charge,
            "billing_pulse_seconds": self.billing_pulse_seconds,
            "minimum_billable_seconds": self.minimum_billable_seconds,
            "charge_failed_calls": self.charge_failed_calls,
        }


def invalidate_billing_policy_cache(tenant_id: int) -> None:
    """Evict the billing policy Redis cache for a tenant.

    Call whenever customer_rates or plan billing columns change for this tenant.
    """
    from app.extensions import redis_ext
    try:
        redis_ext.client.delete(f"tenant:rate:{tenant_id}")
    except Exception:
        pass


def resolve_billing_policy(tenant_id: int, *, connection=None, use_cache: bool = True) -> BillingPolicy:
    """Resolve all billing fields for a tenant with per-field precedence:

    1. customer_rates column (NULL = field not overridden for this tenant)
    2. plans.default_* column (NULL = plan has no opinion → platform default)
    3. Platform DEFAULT_* constants

    Each field resolves independently — a tenant can override pulse_seconds
    while inheriting rate_per_min from their plan.

    charge_failed_calls tri-state:
      customer_rates.charge_failed_calls IS NULL  → inherit from plan / platform
      customer_rates.charge_failed_calls = TRUE   → explicit "yes" override
      customer_rates.charge_failed_calls = FALSE  → explicit "no" override

    Raises ValueError if tenant_id does not exist (fail-closed).
    Pass use_cache=False to bypass Redis and always query the DB.
    Pass connection= to reuse an existing psycopg connection.
    """
    import datetime as _dt

    if use_cache:
        cached = _policy_from_cache(tenant_id)
        if cached is not None:
            return cached

    def _q_one(sql: str, params: tuple):
        if connection is not None:
            return connection.execute(sql, params).fetchone()
        return db.query_one(sql, params)

    # Fail-closed: tenant must exist before we can resolve billing policy
    tenant_exists = _q_one("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,))
    if not tenant_exists:
        raise ValueError(f"resolve_billing_policy: tenant {tenant_id} not found — cannot resolve billing policy")

    # --- Load customer_rates row (may not exist) ---
    cr = _q_one(
        """
        SELECT rate_per_min, rate_per_call, min_charge,
               billing_pulse_seconds, minimum_billable_seconds, charge_failed_calls
        FROM customer_rates WHERE tenant_id = %s
        """,
        (tenant_id,),
    )

    # --- Load plan defaults (via tenant → plan join) ---
    plan_row = _q_one(
        """
        SELECT p.id AS plan_id,
               p.name AS plan_name,
               p.default_rate_per_min,
               p.default_rate_per_call,
               p.default_min_charge,
               p.default_billing_pulse_seconds,
               p.default_minimum_billable_seconds,
               p.default_charge_failed_calls,
               p.feature_flags
        FROM tenants t
        JOIN plans p ON t.plan_id = p.id
        WHERE t.id = %s
        """,
        (tenant_id,),
    )

    plan_id: int | None = None
    plan_defaults: dict = {}
    if plan_row:
        plan_id = plan_row["plan_id"]
        # Structured default columns take priority over legacy feature_flags JSONB
        plan_defaults = {
            "rate_per_min": plan_row["default_rate_per_min"],
            "rate_per_call": plan_row["default_rate_per_call"],
            "min_charge": plan_row["default_min_charge"],
            "billing_pulse_seconds": plan_row["default_billing_pulse_seconds"],
            "minimum_billable_seconds": plan_row["default_minimum_billable_seconds"],
            "charge_failed_calls": plan_row["default_charge_failed_calls"],
        }

        # Back-fill from legacy feature_flags for rate fields if structured columns are NULL
        ff = plan_row.get("feature_flags") or {}
        if isinstance(ff, str):
            try:
                ff = json.loads(ff)
            except Exception:
                ff = {}
        if plan_defaults["rate_per_min"] is None and ff.get("rate_per_min") is not None:
            plan_defaults["rate_per_min"] = Decimal(str(ff["rate_per_min"]))
        if plan_defaults["rate_per_call"] is None and ff.get("rate_per_call") is not None:
            plan_defaults["rate_per_call"] = Decimal(str(ff["rate_per_call"]))
        if plan_defaults["min_charge"] is None and ff.get("min_charge") is not None:
            plan_defaults["min_charge"] = Decimal(str(ff["min_charge"]))

        # Hardcoded name-based fallback for plans that have no structured columns yet
        if plan_defaults["rate_per_min"] is None:
            name = (plan_row.get("plan_name") or "").lower()
            hardcoded = {
                "free": Decimal("0.80"),
                "starter": Decimal("0.70"),
                "pro": Decimal("0.60"),
            }
            if name in hardcoded:
                plan_defaults["rate_per_min"] = hardcoded[name]
                if plan_defaults["min_charge"] is None:
                    plan_defaults["min_charge"] = hardcoded[name]

    def _resolve_decimal(field: str, platform_val: Decimal) -> tuple[Decimal, Source]:
        if cr and cr.get(field) is not None:
            return Decimal(str(cr[field])), "tenant_override"
        pd = plan_defaults.get(field)
        if pd is not None:
            return Decimal(str(pd)), "plan_default"
        return platform_val, "platform_default"

    def _resolve_int(field: str, platform_val: int) -> tuple[int, Source]:
        if cr and cr.get(field) is not None:
            return int(cr[field]), "tenant_override"
        pd = plan_defaults.get(field)
        if pd is not None:
            return int(pd), "plan_default"
        return platform_val, "platform_default"

    def _resolve_bool(field: str, platform_val: bool) -> tuple[bool, Source]:
        if cr is not None and cr.get(field) is not None:
            return bool(cr[field]), "tenant_override"
        pd = plan_defaults.get(field)
        if pd is not None:
            return bool(pd), "plan_default"
        return platform_val, "platform_default"

    rate_per_min, rpm_src = _resolve_decimal("rate_per_min", DEFAULT_RATE_PER_MIN)
    rate_per_call, rpc_src = _resolve_decimal("rate_per_call", DEFAULT_RATE_PER_CALL)
    min_charge, mc_src = _resolve_decimal("min_charge", DEFAULT_MIN_CHARGE)
    pulse, pulse_src = _resolve_int("billing_pulse_seconds", DEFAULT_BILLING_PULSE_SECONDS)
    min_bill, mb_src = _resolve_int("minimum_billable_seconds", DEFAULT_MINIMUM_BILLABLE_SECONDS)
    charge_failed, cf_src = _resolve_bool("charge_failed_calls", DEFAULT_CHARGE_FAILED_CALLS)

    # min_charge defaults to rate_per_min when not explicitly set at any level
    if mc_src == "platform_default" and min_charge == DEFAULT_MIN_CHARGE:
        if rpm_src != "platform_default":
            min_charge = rate_per_min
            mc_src = rpm_src

    policy = BillingPolicy(
        rate_per_min=rate_per_min,
        rate_per_min_source=rpm_src,
        rate_per_call=rate_per_call,
        rate_per_call_source=rpc_src,
        min_charge=min_charge,
        min_charge_source=mc_src,
        billing_pulse_seconds=max(pulse, 1),
        billing_pulse_seconds_source=pulse_src,
        minimum_billable_seconds=max(min_bill, 0),
        minimum_billable_seconds_source=mb_src,
        charge_failed_calls=charge_failed,
        charge_failed_calls_source=cf_src,
        plan_id=plan_id,
        tenant_id=tenant_id,
        resolved_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
    if use_cache:
        _cache_policy(tenant_id, policy)
    return policy


def _policy_from_cache(tenant_id: int) -> BillingPolicy | None:
    """Return cached BillingPolicy from Redis or None on miss/error."""
    from app.extensions import redis_ext

    cache_key = f"tenant:rate:{tenant_id}"
    try:
        cached = redis_ext.client.get(cache_key)
        if not cached:
            return None
        val = cached.decode("utf-8") if isinstance(cached, bytes) else cached
        d = json.loads(val)
        return BillingPolicy(
            rate_per_min=Decimal(d["rate_per_min"]),
            rate_per_min_source=d.get("rate_per_min_source", "platform_default"),
            rate_per_call=Decimal(d["rate_per_call"]),
            rate_per_call_source=d.get("rate_per_call_source", "platform_default"),
            min_charge=Decimal(d["min_charge"]),
            min_charge_source=d.get("min_charge_source", "platform_default"),
            billing_pulse_seconds=int(d.get("billing_pulse_seconds", DEFAULT_BILLING_PULSE_SECONDS)),
            billing_pulse_seconds_source=d.get("billing_pulse_seconds_source", "platform_default"),
            minimum_billable_seconds=int(d.get("minimum_billable_seconds", DEFAULT_MINIMUM_BILLABLE_SECONDS)),
            minimum_billable_seconds_source=d.get("minimum_billable_seconds_source", "platform_default"),
            charge_failed_calls=d.get("charge_failed_calls") is True or d.get("charge_failed_calls") == "True",
            charge_failed_calls_source=d.get("charge_failed_calls_source", "platform_default"),
            plan_id=d.get("plan_id"),
        )
    except Exception:
        return None


def _cache_policy(tenant_id: int, policy: BillingPolicy) -> None:
    from app.extensions import redis_ext

    cache_key = f"tenant:rate:{tenant_id}"
    try:
        redis_ext.client.setex(cache_key, 3600, json.dumps(policy.as_dict()))
    except Exception:
        pass


def get_tenant_rate(tenant_id: int) -> dict:
    """Return resolved billing rates dict for a tenant (cached, backward-compat)."""
    return resolve_billing_policy(tenant_id).rates_dict()


# ---------------------------------------------------------------------------
# Charge calculation helpers
# ---------------------------------------------------------------------------

_PRE_CONNECTION_FAILURES = {"no_answer", "busy", "failed", "unreachable"}


def calculate_charge_and_duration(
    talk_seconds: int,
    tenant_id: int,
    call_result: str = "completed",
    *,
    rates: dict | None = None,
) -> tuple[Decimal, int]:
    """Calculate call charge and billable seconds from talk duration and rates.

    Billing eligibility is based on connection evidence (talk_seconds > 0), NOT
    on the call_result status label. A call with status='canceled' where
    talk_seconds=27 was genuinely answered — the remote party hung up (SIP BYE)
    after the IVR played, and real carrier cost was incurred.

    Statuses that indicate pre-connection failure (no talk time, no carrier cost):
      no_answer, busy, failed, unreachable
    Only these are non-billable by default (unless charge_failed_calls=True).

    'canceled' with talk_seconds > 0 = answered call, remote terminated → billable.
    'canceled' with talk_seconds = 0  = canceled before answer → not billable.

    Pass `rates` to use a pre-resolved dict (e.g. from a stored rate_snapshot)
    rather than hitting the DB again.
    """
    if rates is None:
        rates = get_tenant_rate(tenant_id)

    is_unanswered_failure = (talk_seconds <= 0) and (call_result in _PRE_CONNECTION_FAILURES)
    if is_unanswered_failure and not rates["charge_failed_calls"]:
        return Decimal("0.0000"), 0

    if talk_seconds <= 0 and not rates["charge_failed_calls"]:
        return Decimal("0.0000"), 0

    if rates["rate_per_call"] > 0:
        charge = rates["rate_per_call"]
        billable_seconds = max(talk_seconds, 0)
        return round(max(charge, rates["min_charge"]), 4), billable_seconds

    pulse = rates["billing_pulse_seconds"]
    if not pulse or pulse <= 0:
        pulse = 60

    if talk_seconds <= 0:
        rounded_seconds = 0
    else:
        rounded_seconds = math.ceil(talk_seconds / float(pulse)) * pulse

    billable_seconds = max(rounded_seconds, rates["minimum_billable_seconds"])
    minutes = Decimal(str(billable_seconds)) / Decimal("60.0")
    charge = minutes * rates["rate_per_min"]
    return round(max(charge, rates["min_charge"]), 4), billable_seconds


def calculate_charge(talk_seconds: int, tenant_id: int) -> Decimal:
    """Calculate final charge for a call based on talk duration in seconds."""
    charge, _ = calculate_charge_and_duration(talk_seconds, tenant_id, "completed")
    return charge


def estimate_charge(tenant_id: int) -> Decimal:
    """Return the minimum charge for a call (used for pre-call balance checks)."""
    rates = get_tenant_rate(tenant_id)
    return rates["min_charge"]
