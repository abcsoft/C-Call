from __future__ import annotations

import math
import json
from decimal import Decimal
from app.db import db

# Default platform rates
DEFAULT_RATE_PER_MIN = Decimal("0.80")
DEFAULT_RATE_PER_CALL = Decimal("0.00")
DEFAULT_MIN_CHARGE = Decimal("0.80")
DEFAULT_BILLING_PULSE_SECONDS = 60
DEFAULT_MINIMUM_BILLABLE_SECONDS = 60
DEFAULT_CHARGE_FAILED_CALLS = False

# Statuses that represent pre-connection failure (no carrier cost incurred)
_PRE_CONNECTION_STATUSES = frozenset({"no_answer", "busy", "failed", "unreachable", "timeout"})


def get_tenant_rate(tenant_id: int) -> dict[str, any]:
    """Retrieve call rates for the tenant, checking customer_rates override,
    then plan defaults, and finally falling back to platform defaults.
    """
    from app.extensions import redis_ext
    import json

    cache_key = f"tenant:rate:{tenant_id}"
    try:
        cached = redis_ext.client.get(cache_key)
        if cached:
            val = cached.decode("utf-8") if isinstance(cached, bytes) else cached
            d = json.loads(val)
            return {
                "rate_per_min": Decimal(d["rate_per_min"]),
                "rate_per_call": Decimal(d["rate_per_call"]),
                "min_charge": Decimal(d["min_charge"]),
                "billing_pulse_seconds": int(d["billing_pulse_seconds"]) if d.get("billing_pulse_seconds") is not None else DEFAULT_BILLING_PULSE_SECONDS,
                "minimum_billable_seconds": int(d["minimum_billable_seconds"]) if d.get("minimum_billable_seconds") is not None else DEFAULT_MINIMUM_BILLABLE_SECONDS,
                "charge_failed_calls": d.get("charge_failed_calls") == "True" or d.get("charge_failed_calls") is True
            }
    except Exception as e:
        import logging
        logging.getLogger("ccall").error(f"Redis get rate cache failed: {e}")

    # 1. Check customer_rates first (per-tenant override)
    row = db.query_one(
        """
        SELECT rate_per_min, rate_per_call, min_charge,
               billing_pulse_seconds, minimum_billable_seconds, charge_failed_calls
        FROM customer_rates WHERE tenant_id = %s
        """,
        (tenant_id,)
    )
    rates = None
    if row:
        rates = {
            "rate_per_min": Decimal(str(row["rate_per_min"] if row["rate_per_min"] is not None else DEFAULT_RATE_PER_MIN)),
            "rate_per_call": Decimal(str(row["rate_per_call"] if row["rate_per_call"] is not None else DEFAULT_RATE_PER_CALL)),
            "min_charge": Decimal(str(row["min_charge"] if row["min_charge"] is not None else DEFAULT_MIN_CHARGE)),
            "billing_pulse_seconds": row["billing_pulse_seconds"] if row["billing_pulse_seconds"] is not None else DEFAULT_BILLING_PULSE_SECONDS,
            "minimum_billable_seconds": row["minimum_billable_seconds"] if row["minimum_billable_seconds"] is not None else DEFAULT_MINIMUM_BILLABLE_SECONDS,
            "charge_failed_calls": bool(row["charge_failed_calls"])
        }

    # 2. Fall back to plan default rate
    if not rates:
        plan_row = db.query_one(
            """
            SELECT p.name, p.feature_flags
            FROM tenants t
            JOIN plans p ON t.plan_id = p.id
            WHERE t.id = %s
            """,
            (tenant_id,)
        )
        if plan_row:
            plan_name = plan_row["name"].lower()
            feature_flags = plan_row["feature_flags"] or {}
            if isinstance(feature_flags, str):
                try:
                    feature_flags = json.loads(feature_flags)
                except Exception:
                    feature_flags = {}

            # Check if rate is specified in plan feature flags
            rate_per_min = feature_flags.get("rate_per_min")
            rate_per_call = feature_flags.get("rate_per_call")
            min_charge = feature_flags.get("min_charge")

            if rate_per_min is not None or rate_per_call is not None or min_charge is not None:
                rates = {
                    "rate_per_min": Decimal(str(rate_per_min)) if rate_per_min is not None else DEFAULT_RATE_PER_MIN,
                    "rate_per_call": Decimal(str(rate_per_call)) if rate_per_call is not None else DEFAULT_RATE_PER_CALL,
                    "min_charge": Decimal(str(min_charge)) if min_charge is not None else DEFAULT_MIN_CHARGE,
                    "billing_pulse_seconds": DEFAULT_BILLING_PULSE_SECONDS,
                    "minimum_billable_seconds": DEFAULT_MINIMUM_BILLABLE_SECONDS,
                    "charge_failed_calls": DEFAULT_CHARGE_FAILED_CALLS
                }
            else:
                # Hardcoded fallback rates based on plan name
                if plan_name == "free":
                    rates = {
                        "rate_per_min": Decimal("0.80"),
                        "rate_per_call": Decimal("0.00"),
                        "min_charge": Decimal("0.80"),
                        "billing_pulse_seconds": DEFAULT_BILLING_PULSE_SECONDS,
                        "minimum_billable_seconds": DEFAULT_MINIMUM_BILLABLE_SECONDS,
                        "charge_failed_calls": DEFAULT_CHARGE_FAILED_CALLS
                    }
                elif plan_name == "starter":
                    rates = {
                        "rate_per_min": Decimal("0.70"),
                        "rate_per_call": Decimal("0.00"),
                        "min_charge": Decimal("0.70"),
                        "billing_pulse_seconds": DEFAULT_BILLING_PULSE_SECONDS,
                        "minimum_billable_seconds": DEFAULT_MINIMUM_BILLABLE_SECONDS,
                        "charge_failed_calls": DEFAULT_CHARGE_FAILED_CALLS
                    }
                elif plan_name == "pro":
                    rates = {
                        "rate_per_min": Decimal("0.60"),
                        "rate_per_call": Decimal("0.00"),
                        "min_charge": Decimal("0.60"),
                        "billing_pulse_seconds": DEFAULT_BILLING_PULSE_SECONDS,
                        "minimum_billable_seconds": DEFAULT_MINIMUM_BILLABLE_SECONDS,
                        "charge_failed_calls": DEFAULT_CHARGE_FAILED_CALLS
                    }

    if not rates:
        # 3. Fall back to platform default (0.80 BDT/min)
        rates = {
            "rate_per_min": DEFAULT_RATE_PER_MIN,
            "rate_per_call": DEFAULT_RATE_PER_CALL,
            "min_charge": DEFAULT_MIN_CHARGE,
            "billing_pulse_seconds": DEFAULT_BILLING_PULSE_SECONDS,
            "minimum_billable_seconds": DEFAULT_MINIMUM_BILLABLE_SECONDS,
            "charge_failed_calls": DEFAULT_CHARGE_FAILED_CALLS
        }

    try:
        redis_ext.client.setex(
            cache_key,
            3600,
            json.dumps({k: str(v) for k, v in rates.items()})
        )
    except Exception as e:
        import logging
        logging.getLogger("ccall").error(f"Redis set rate cache failed: {e}")

    return rates


def calculate_charge_and_duration(talk_seconds: int, tenant_id: int, call_result: str = "completed") -> tuple[Decimal, int]:
    """Calculate the call charge and billable seconds based on talk duration, tenant rate, and pulse rules.
    Returns: (charge_amount, billable_seconds)
    """
    rates = get_tenant_rate(tenant_id)

    # 1. Determine if this call should be charged.
    # Pre-connection failures (no carrier cost) are not billed unless charge_failed_calls=True.
    # "canceled" (remote BYE) is billable only when talk_seconds > 0 (connection evidence).
    is_failed_call = call_result in _PRE_CONNECTION_STATUSES
    if call_result == "canceled" and talk_seconds <= 0:
        is_failed_call = True

    if is_failed_call and not rates["charge_failed_calls"]:
        return Decimal("0.0000"), 0

    # 2. If it is flat rate per call, check if flat rate overrides per-minute billing.
    if rates["rate_per_call"] > 0:
        charge = rates["rate_per_call"]
        billable_seconds = max(talk_seconds, 0)
        final_charge = max(charge, rates["min_charge"])
        return round(final_charge, 4), billable_seconds

    # 3. Pulse billing calculation
    pulse = rates["billing_pulse_seconds"]
    if pulse is None or pulse <= 0:
        pulse = 60  # 0 or null is treated as 60 seconds (1 minute pulse)

    if talk_seconds <= 0:
        rounded_seconds = 0
    else:
        rounded_seconds = math.ceil(talk_seconds / float(pulse)) * pulse

    # Apply minimum billable seconds constraint
    billable_seconds = max(rounded_seconds, rates["minimum_billable_seconds"])

    # Calculate charge: (billable_seconds / 60) * rate_per_min
    minutes = Decimal(str(billable_seconds)) / Decimal("60.0")
    charge = minutes * rates["rate_per_min"]

    # Cap at min_charge
    final_charge = max(charge, rates["min_charge"])
    return round(final_charge, 4), billable_seconds


def calculate_charge(talk_seconds: int, tenant_id: int) -> Decimal:
    """Calculate the final charge for a call based on talk duration (in seconds).
    Formula: ceil(seconds/60) * rate_per_min OR flat rate_per_call, capped at min_charge.
    Round to 4 decimal places.
    """
    charge, _ = calculate_charge_and_duration(talk_seconds, tenant_id, "completed")
    return charge


def estimate_charge(tenant_id: int) -> Decimal:
    """Return the minimum charge of a call for pre-call balance checks."""
    rates = get_tenant_rate(tenant_id)
    return rates["min_charge"]
