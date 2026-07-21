from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.db import db

logger = logging.getLogger("ccall")

# ---------------------------------------------------------------------------
# Platform-wide defaults — last resort when no plan or customer override
# These are used by the platform fallback policy below.
# ---------------------------------------------------------------------------
DEFAULT_RATE_PER_MIN = Decimal("0.80")
DEFAULT_RATE_PER_CALL = Decimal("0.00")
DEFAULT_MIN_CHARGE = Decimal("0.80")
DEFAULT_BILLING_PULSE_SECONDS = 60
DEFAULT_MINIMUM_BILLABLE_SECONDS = 60
DEFAULT_CHARGE_FAILED_CALLS = False
DEFAULT_CURRENCY = "BDT"

# Supported currency codes
_SUPPORTED_CURRENCIES = frozenset({"BDT", "USD"})

# Valid source labels
_VALID_SOURCES = frozenset({"tenant_override", "plan_default", "platform_default"})

# Current snapshot schema version
POLICY_VERSION = 1

Source = Literal["tenant_override", "plan_default", "platform_default"]


# ---------------------------------------------------------------------------
# BillingPolicy — immutable, per-field resolved
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BillingPolicy:
    """Per-field resolved billing policy. Immutable after construction.

    All monetary fields use Decimal.  Pulse must be >= 1.  Minimum billable
    seconds must be >= 0.  Rates must be non-negative.  tenant_id must be set
    for any snapshot written to the calls table.
    """

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
    charge_failed_calls_source: Source

    plan_id: int | None = None
    currency: str = DEFAULT_CURRENCY
    tenant_id: int | None = None
    policy_version: int = POLICY_VERSION
    resolved_at: str | None = None

    def as_dict(self) -> dict:
        """Serialisable dict for rate_snapshot storage (JSON-round-trip safe)."""
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
        """Backward-compat dict consumed by calculate_charge_and_duration."""
        return {
            "rate_per_min": self.rate_per_min,
            "rate_per_call": self.rate_per_call,
            "min_charge": self.min_charge,
            "billing_pulse_seconds": self.billing_pulse_seconds,
            "minimum_billable_seconds": self.minimum_billable_seconds,
            "charge_failed_calls": self.charge_failed_calls,
        }


# ---------------------------------------------------------------------------
# Snapshot validation and deserialization
# ---------------------------------------------------------------------------

_REQUIRED_SNAPSHOT_KEYS = frozenset({
    "rate_per_min", "rate_per_call", "min_charge",
    "billing_pulse_seconds", "minimum_billable_seconds",
    "charge_failed_calls",
})


def validate_rate_snapshot(
    snapshot: Any,
    expected_tenant_id: int | None = None,
) -> dict:
    """Validate a raw snapshot dict (parsed from calls.rate_snapshot jsonb).

    Returns the validated dict with Decimal values on success.
    Raises ValueError with a descriptive message on any schema or semantic violation.

    Strict rules (new call context):
    - Required keys present
    - Rates non-negative Decimal
    - Pulse >= 1
    - Minimum billable seconds >= 0
    - charge_failed_calls is boolean
    - currency supported
    - policy_version present and == POLICY_VERSION (if set)
    - tenant_id matches expected_tenant_id when provided
    - No sensitive fields
    """
    if not isinstance(snapshot, dict):
        raise ValueError(f"rate_snapshot is not a dict (type={type(snapshot).__name__})")

    missing = _REQUIRED_SNAPSHOT_KEYS - snapshot.keys()
    if missing:
        raise ValueError(f"rate_snapshot missing required keys: {sorted(missing)}")

    # Sensitive field guard
    for bad in ("password", "secret", "key", "token", "credential"):
        for k in snapshot:
            if bad in k.lower():
                raise ValueError(f"rate_snapshot contains potentially sensitive field: {k!r}")

    try:
        rpm = Decimal(str(snapshot["rate_per_min"]))
    except InvalidOperation:
        raise ValueError(f"rate_snapshot.rate_per_min is not a valid Decimal: {snapshot['rate_per_min']!r}")
    if rpm < 0:
        raise ValueError(f"rate_snapshot.rate_per_min must be non-negative, got {rpm}")
    if rpm > Decimal("1000"):
        raise ValueError(f"rate_snapshot.rate_per_min exceeds maximum (1000), got {rpm}")

    try:
        rpc = Decimal(str(snapshot["rate_per_call"]))
    except InvalidOperation:
        raise ValueError(f"rate_snapshot.rate_per_call is not a valid Decimal: {snapshot['rate_per_call']!r}")
    if rpc < 0:
        raise ValueError(f"rate_snapshot.rate_per_call must be non-negative, got {rpc}")

    try:
        mc = Decimal(str(snapshot["min_charge"]))
    except InvalidOperation:
        raise ValueError(f"rate_snapshot.min_charge is not a valid Decimal: {snapshot['min_charge']!r}")
    if mc < 0:
        raise ValueError(f"rate_snapshot.min_charge must be non-negative, got {mc}")

    pulse = snapshot["billing_pulse_seconds"]
    if not isinstance(pulse, int) or pulse < 1:
        try:
            pulse = int(pulse)
        except (TypeError, ValueError):
            raise ValueError(f"rate_snapshot.billing_pulse_seconds is not a positive int: {pulse!r}")
        if pulse < 1:
            raise ValueError(f"rate_snapshot.billing_pulse_seconds must be >= 1, got {pulse}")

    mbs = snapshot["minimum_billable_seconds"]
    try:
        mbs = int(mbs)
    except (TypeError, ValueError):
        raise ValueError(f"rate_snapshot.minimum_billable_seconds is not an int: {mbs!r}")
    if mbs < 0:
        raise ValueError(f"rate_snapshot.minimum_billable_seconds must be >= 0, got {mbs}")

    cfc = snapshot["charge_failed_calls"]
    if not isinstance(cfc, bool):
        if isinstance(cfc, str) and cfc.lower() in ("true", "false"):
            cfc = cfc.lower() == "true"
        elif cfc in (0, 1):
            cfc = bool(cfc)
        else:
            raise ValueError(f"rate_snapshot.charge_failed_calls must be boolean, got {cfc!r}")

    currency = snapshot.get("currency", DEFAULT_CURRENCY)
    if currency not in _SUPPORTED_CURRENCIES:
        raise ValueError(f"rate_snapshot.currency {currency!r} not supported; valid: {sorted(_SUPPORTED_CURRENCIES)}")

    if "policy_version" not in snapshot:
        raise ValueError("rate_snapshot missing required key: policy_version")
    pv = snapshot["policy_version"]
    if int(pv) != POLICY_VERSION:
        raise ValueError(f"rate_snapshot.policy_version {pv} != expected {POLICY_VERSION}")

    if expected_tenant_id is not None:
        snap_tid = snapshot.get("tenant_id")
        if snap_tid is not None and int(snap_tid) != expected_tenant_id:
            raise ValueError(
                f"rate_snapshot.tenant_id {snap_tid} does not match expected tenant {expected_tenant_id}"
            )

    snap_tid = snapshot.get("tenant_id")
    validated: dict = {
        "rate_per_min": rpm,
        "rate_per_call": rpc,
        "min_charge": mc,
        "billing_pulse_seconds": int(pulse),
        "minimum_billable_seconds": int(mbs),
        "charge_failed_calls": bool(cfc),
        "currency": currency,
        "policy_version": int(pv),
    }
    if snap_tid is not None:
        validated["tenant_id"] = int(snap_tid)
    # Preserve source tracking fields if present
    for src_key in ("rate_per_min_source", "rate_per_call_source", "min_charge_source",
                    "billing_pulse_seconds_source", "minimum_billable_seconds_source",
                    "charge_failed_calls_source"):
        if src_key in snapshot:
            validated[src_key] = snapshot[src_key]
    if "plan_id" in snapshot:
        validated["plan_id"] = snapshot["plan_id"]
    if "resolved_at" in snapshot:
        validated["resolved_at"] = snapshot["resolved_at"]
    return validated


def billing_policy_from_snapshot(
    snapshot: Any,
    expected_tenant_id: int | None = None,
) -> BillingPolicy:
    """Deserialise a validated snapshot dict back to a BillingPolicy.

    Raises ValueError on any validation failure (fail-closed).
    """
    rates = validate_rate_snapshot(snapshot, expected_tenant_id=expected_tenant_id)
    d = snapshot if isinstance(snapshot, dict) else {}

    def _src(key: str) -> Source:
        v = d.get(key, "platform_default")
        return v if v in _VALID_SOURCES else "platform_default"

    return BillingPolicy(
        rate_per_min=rates["rate_per_min"],
        rate_per_min_source=_src("rate_per_min_source"),
        rate_per_call=rates["rate_per_call"],
        rate_per_call_source=_src("rate_per_call_source"),
        min_charge=rates["min_charge"],
        min_charge_source=_src("min_charge_source"),
        billing_pulse_seconds=rates["billing_pulse_seconds"],
        billing_pulse_seconds_source=_src("billing_pulse_seconds_source"),
        minimum_billable_seconds=rates["minimum_billable_seconds"],
        minimum_billable_seconds_source=_src("minimum_billable_seconds_source"),
        charge_failed_calls=rates["charge_failed_calls"],
        charge_failed_calls_source=_src("charge_failed_calls_source"),
        plan_id=d.get("plan_id"),
        currency=d.get("currency", DEFAULT_CURRENCY),
        tenant_id=d.get("tenant_id"),
        policy_version=int(d.get("policy_version", POLICY_VERSION)),
        resolved_at=d.get("resolved_at"),
    )


def serialize_billing_policy(policy: BillingPolicy) -> str:
    """Serialise a BillingPolicy to a JSON string suitable for calls.rate_snapshot."""
    return json.dumps(policy.as_dict())


# ---------------------------------------------------------------------------
# Cache invalidation (public API)
# ---------------------------------------------------------------------------

def invalidate_billing_policy_cache(tenant_id: int) -> None:
    """Evict the billing policy Redis cache for a tenant.

    Must be called after every successful mutation of:
    - customer_rates (insert/update/delete)
    - tenant plan assignment
    - plan default billing columns
    - subscription activation / renewal / plan switch
    - tenant suspension / cancellation / reactivation
    """
    from app.extensions import redis_ext
    try:
        redis_ext.client.delete(f"tenant:rate:{tenant_id}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core resolution
# ---------------------------------------------------------------------------

def resolve_billing_policy(
    tenant_id: int,
    *,
    connection=None,
    use_cache: bool = True,
) -> BillingPolicy:
    """Resolve all billing fields for a tenant with per-field precedence:

    1. customer_rates column (NULL = field not overridden for this tenant)
    2. plans.default_* column (NULL = plan has no opinion → platform default)
    3. Platform DEFAULT_* constants

    Each field resolves independently.

    charge_failed_calls tri-state:
      customer_rates.charge_failed_calls IS NULL  → inherit from plan / platform
      customer_rates.charge_failed_calls = TRUE   → explicit "yes" override
      customer_rates.charge_failed_calls = FALSE  → explicit "no" override

    Args:
        tenant_id: Must exist — raises ValueError if not found (fail-closed).
        connection: Optional existing psycopg connection for dispatch-time
            transactional reads (bypasses pool checkout and stale cache).
        use_cache: Set False to force a DB read and skip the Redis cache.
            Ignored when connection= is supplied (connection implies fresh read).

    Raises:
        ValueError: if tenant_id does not exist.
    """
    import datetime as _dt

    # Skip cache when a connection is supplied (dispatch-time transactional read)
    _use_cache = use_cache and (connection is None)

    if _use_cache:
        cached = _policy_from_cache(tenant_id)
        if cached is not None:
            return cached

    def _q_one(sql: str, params: tuple):
        if connection is not None:
            return connection.execute(sql, params).fetchone()
        return db.query_one(sql, params)

    # Fail-closed: tenant must exist
    tenant_row = _q_one(
        "SELECT id, status FROM tenants WHERE id = %s",
        (tenant_id,),
    )
    if not tenant_row:
        raise ValueError(
            f"resolve_billing_policy: tenant {tenant_id} not found — billing policy cannot be resolved"
        )

    # --- customer_rates ---
    cr = _q_one(
        """
        SELECT rate_per_min, rate_per_call, min_charge,
               billing_pulse_seconds, minimum_billable_seconds, charge_failed_calls
        FROM customer_rates WHERE tenant_id = %s
        """,
        (tenant_id,),
    )

    # --- plan defaults (via tenant → plan join) ---
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
        plan_defaults = {
            "rate_per_min": plan_row["default_rate_per_min"],
            "rate_per_call": plan_row["default_rate_per_call"],
            "min_charge": plan_row["default_min_charge"],
            "billing_pulse_seconds": plan_row["default_billing_pulse_seconds"],
            "minimum_billable_seconds": plan_row["default_minimum_billable_seconds"],
            "charge_failed_calls": plan_row["default_charge_failed_calls"],
        }

        # Back-fill from legacy feature_flags JSONB for rate fields if structured columns are NULL
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

        # Hardcoded name-based fallback for plans without structured columns yet
        if plan_defaults["rate_per_min"] is None:
            name = (plan_row.get("plan_name") or "").lower()
            _hardcoded = {
                "free": Decimal("0.80"),
                "starter": Decimal("0.70"),
                "pro": Decimal("0.60"),
            }
            if name in _hardcoded:
                plan_defaults["rate_per_min"] = _hardcoded[name]
                if plan_defaults["min_charge"] is None:
                    plan_defaults["min_charge"] = _hardcoded[name]

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
    if _use_cache:
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
            currency=d.get("currency", DEFAULT_CURRENCY),
            tenant_id=d.get("tenant_id"),
            policy_version=int(d.get("policy_version", POLICY_VERSION)),
            resolved_at=d.get("resolved_at"),
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
# Charge calculation
# ---------------------------------------------------------------------------

_PRE_CONNECTION_FAILURES = frozenset({"no_answer", "busy", "failed", "unreachable"})
# Alias used in tests
_PRE_CONNECTION_STATUSES = _PRE_CONNECTION_FAILURES


def calculate_charge_and_duration(
    talk_seconds: int,
    tenant_id: int,
    call_result: str = "completed",
    *,
    rates: dict | None = None,
) -> tuple[Decimal, int]:
    """Calculate call charge and billable seconds from talk duration and rates.

    Billing eligibility is based on connection evidence (talk_seconds > 0), NOT
    on the call_result status label.  A call with status='canceled' where
    talk_seconds=27 was genuinely answered and carrier cost was incurred.

    Pass `rates` to use a pre-resolved dict (e.g. from a stored rate_snapshot)
    rather than hitting the DB again.
    """
    if rates is None:
        rates = get_tenant_rate(tenant_id)

    if talk_seconds <= 0 and not rates["charge_failed_calls"]:
        return Decimal("0.0000"), 0

    if rates["rate_per_call"] > 0:
        billable_seconds = max(talk_seconds, 0)
        return round(max(rates["rate_per_call"], rates["min_charge"]), 4), billable_seconds

    pulse = int(rates["billing_pulse_seconds"]) or 60
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
    return get_tenant_rate(tenant_id)["min_charge"]
