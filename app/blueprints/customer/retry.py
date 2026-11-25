# E:\c-call-ivr\app\blueprints\customer\retry.py
"""Retry & DND settings page — GET/POST /retry, GET /api/retry-rules.

Persists into the same retry_rules table app/services/retry_service.py
reads at call time. The "auto retry" master toggle is stored in
tenants.settings (JSONB) and, when off, is also written onto every
retry_rules row as is_enabled=false — schedule_retry() already checks
is_enabled per attempt, so turning the master toggle off actually stops
the engine without any change needed there.
"""
from __future__ import annotations

import datetime
from typing import Any

from flask import flash, g, jsonify, redirect, render_template, request, url_for

from app.blueprints.auth.decorators import require_login, require_role
from app.blueprints.customer import customer_bp
from app.models.tenant import get_tenant_by_id, update_tenant_settings
from app.services.retry_service import get_retry_rules, save_retry_rules

VALID_RETRY_ON = {"no_answer", "busy", "rejected"}
# UI only exposes these two per spec — "rejected" stays valid in
# VALID_RETRY_ON for any legacy stored rows, just not offered as a choice.
UI_RETRY_ON_OPTIONS = ["no_answer", "busy"]
MIN_DELAY_MINUTES = 5
MAX_ATTEMPTS_CAP = 5

DELAY_UNIT_MINUTES = {"minutes": 1, "hours": 60, "days": 1440}


def _minutes_to_value_unit(minutes: int) -> tuple[int, str]:
    """Reverse mapping for preloading saved delay_minutes into the
    numeric value+unit fields — picks the largest exact unit so e.g.
    1440 shows as '1 day' instead of '1440 minutes'."""
    if minutes % DELAY_UNIT_MINUTES["days"] == 0 and minutes >= DELAY_UNIT_MINUTES["days"]:
        return minutes // DELAY_UNIT_MINUTES["days"], "days"
    if minutes % DELAY_UNIT_MINUTES["hours"] == 0 and minutes >= DELAY_UNIT_MINUTES["hours"]:
        return minutes // DELAY_UNIT_MINUTES["hours"], "hours"
    return minutes, "minutes"


def _parse_time(value: str, fallback: datetime.time) -> datetime.time:
    try:
        h, m = (int(x) for x in value.split(":")[:2])
        return datetime.time(h, m)
    except (ValueError, AttributeError):
        return fallback


def _validate_and_build_rules(form, max_attempts: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns (rules_list, errors). Empty errors means the payload is
    valid and rules_list is ready for save_retry_rules()."""
    errors: list[str] = []

    if not (1 <= max_attempts <= MAX_ATTEMPTS_CAP):
        errors.append(f"Max attempts must be between 1 and {MAX_ATTEMPTS_CAP}.")
        max_attempts = max(1, min(max_attempts, MAX_ATTEMPTS_CAP))

    dnd_start = _parse_time(form.get("dnd_start", "21:00"), datetime.time(21, 0))
    dnd_end = _parse_time(form.get("dnd_end", "09:00"), datetime.time(9, 0))
    if dnd_start == dnd_end:
        errors.append("DND start and end times must be different.")

    retry_enabled = form.get("retry_enabled") == "on"

    rules: list[dict[str, Any]] = []
    for attempt_no in range(1, max_attempts + 1):
        # Delay is required for every row, must be a positive number.
        delay_value_raw = form.get(f"delay_value_{attempt_no}", "").strip()
        delay_unit = form.get(f"delay_unit_{attempt_no}", "minutes")
        if delay_unit not in DELAY_UNIT_MINUTES:
            delay_unit = "minutes"

        if not delay_value_raw:
            errors.append(f"Attempt {attempt_no}: delay is required.")
            delay_minutes = MIN_DELAY_MINUTES
        else:
            try:
                delay_value = float(delay_value_raw)
            except ValueError:
                errors.append(f"Attempt {attempt_no}: delay must be a positive number.")
                delay_value = 0
            if delay_value <= 0:
                errors.append(f"Attempt {attempt_no}: delay must be a positive number.")
            delay_minutes = int(round(delay_value * DELAY_UNIT_MINUTES[delay_unit]))

        if delay_minutes < MIN_DELAY_MINUTES:
            errors.append(f"Attempt {attempt_no}: delay must be at least {MIN_DELAY_MINUTES} minutes.")

        # At least one retry condition required — no silent fallback.
        retry_on = [v for v in form.getlist(f"retry_on_{attempt_no}") if v in VALID_RETRY_ON]
        if not retry_on:
            errors.append(f"Attempt {attempt_no}: select at least one retry condition (No Answer / Busy).")

        rules.append({
            "attempt_no": attempt_no,
            "delay_minutes": delay_minutes,
            "retry_on": retry_on or ["no_answer", "busy"],
            "dnd_start": dnd_start,
            "dnd_end": dnd_end,
            "max_attempts": max_attempts,
            "is_enabled": retry_enabled,
        })

    return rules, errors


def _validate_and_build_rules_from_json(payload: dict) -> tuple[list[dict[str, Any]], list[str], int, bool]:
    """JSON-body counterpart to _validate_and_build_rules() — accepts the
    {autoRetryEnabled, maxAttempts, retryRules: [...], dndStart, dndEnd}
    structure. Returns (rules_list, errors, max_attempts, retry_enabled)."""
    errors: list[str] = []

    retry_enabled = bool(payload.get("autoRetryEnabled", False))

    try:
        max_attempts = int(payload.get("maxAttempts", 3))
    except (TypeError, ValueError):
        max_attempts = 3
        errors.append("maxAttempts must be an integer.")

    if not (1 <= max_attempts <= MAX_ATTEMPTS_CAP):
        errors.append(f"Max attempts must be between 1 and {MAX_ATTEMPTS_CAP}.")
        max_attempts = max(1, min(max_attempts, MAX_ATTEMPTS_CAP))

    dnd_start = _parse_time(str(payload.get("dndStart", "21:00")), datetime.time(21, 0))
    dnd_end = _parse_time(str(payload.get("dndEnd", "09:00")), datetime.time(9, 0))
    if dnd_start == dnd_end:
        errors.append("DND start and end times must be different.")

    timezone = payload.get("timezone", "Asia/Dhaka")
    if timezone != "Asia/Dhaka":
        errors.append("Only Asia/Dhaka is supported (fixed in MVP).")

    retry_rules_raw = payload.get("retryRules")
    if not isinstance(retry_rules_raw, list):
        errors.append("retryRules must be a list.")
        retry_rules_raw = []

    # retryRules length must match maxAttempts when auto retry is on —
    # only enforced while enabled, since a disabled config may have a
    # stale/partial array from before the merchant turned it off.
    if retry_enabled and len(retry_rules_raw) != max_attempts:
        errors.append(f"retryRules must contain exactly {max_attempts} entries (one per attempt).")

    by_attempt = {}
    for entry in retry_rules_raw:
        if not isinstance(entry, dict):
            errors.append("Each retryRules entry must be an object.")
            continue
        try:
            attempt_no = int(entry.get("attempt"))
        except (TypeError, ValueError):
            errors.append(f"Invalid attempt number: {entry.get('attempt')!r}")
            continue
        by_attempt[attempt_no] = entry

    rules: list[dict[str, Any]] = []
    for attempt_no in range(1, max_attempts + 1):
        entry = by_attempt.get(attempt_no)
        if entry is None:
            errors.append(f"Attempt {attempt_no}: missing from retryRules.")
            rules.append({
                "attempt_no": attempt_no, "delay_minutes": MIN_DELAY_MINUTES,
                "retry_on": ["no_answer", "busy"], "dnd_start": dnd_start, "dnd_end": dnd_end,
                "max_attempts": max_attempts, "is_enabled": retry_enabled,
            })
            continue

        delay_value = entry.get("delayValue")
        delay_unit = entry.get("delayUnit", "minutes")

        if delay_unit not in DELAY_UNIT_MINUTES:
            errors.append(f"Attempt {attempt_no}: delayUnit must be one of minutes, hours, days.")
            delay_unit = "minutes"

        try:
            delay_value_num = float(delay_value)
        except (TypeError, ValueError):
            errors.append(f"Attempt {attempt_no}: delayValue is required and must be a positive number.")
            delay_value_num = 0
        if delay_value_num <= 0:
            errors.append(f"Attempt {attempt_no}: delayValue must be a positive number.")

        delay_minutes = int(round(delay_value_num * DELAY_UNIT_MINUTES[delay_unit]))
        if delay_minutes < MIN_DELAY_MINUTES:
            errors.append(f"Attempt {attempt_no}: delay must be at least {MIN_DELAY_MINUTES} minutes.")

        retry_on_raw = entry.get("retryOn", [])
        if not isinstance(retry_on_raw, list):
            retry_on_raw = []
        retry_on = [v for v in retry_on_raw if v in VALID_RETRY_ON]
        if not retry_on:
            errors.append(f"Attempt {attempt_no}: retryOn must contain at least one valid value (no_answer, busy).")
        invalid_values = [v for v in retry_on_raw if v not in VALID_RETRY_ON]
        if invalid_values:
            errors.append(f"Attempt {attempt_no}: invalid retryOn value(s): {invalid_values}.")

        rules.append({
            "attempt_no": attempt_no,
            "delay_minutes": delay_minutes,
            "retry_on": retry_on or ["no_answer", "busy"],
            "dnd_start": dnd_start,
            "dnd_end": dnd_end,
            "max_attempts": max_attempts,
            "is_enabled": retry_enabled,
        })

    return rules, errors, max_attempts, retry_enabled


@customer_bp.route("/retry", methods=["GET", "POST"])
@require_login
@require_role(["admin"])
def retry_settings():
    tenant_id = g.tenant_id

    if request.method == "POST":
        # API clients (e.g. WooCommerce plugin, external integrations) post
        # the structured JSON body from the task spec; the web form posts
        # form-encoded delay_value_N/delay_unit_N/retry_on_N fields. Both
        # write through the same save_retry_rules()/update_tenant_settings()
        # calls below — same DB writes either way.
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            rules, errors, max_attempts, retry_enabled = _validate_and_build_rules_from_json(payload)

            if errors:
                return jsonify({"success": False, "errors": errors}), 400

            update_tenant_settings(tenant_id, {"retry_enabled": retry_enabled})
            save_retry_rules(tenant_id, rules)
            return jsonify({"success": True, "message": "Retry & DND settings saved."})

        retry_enabled = request.form.get("retry_enabled") == "on"

        if not retry_enabled:
            # Retry disabled: preserve all configured rules but mark them inactive.
            # No per-attempt validation needed — disabled inputs are not submitted.
            update_tenant_settings(tenant_id, {"retry_enabled": False})
            from app.db import db as _db
            _db.execute(
                "UPDATE retry_rules SET is_enabled = FALSE WHERE tenant_id = %s",
                (tenant_id,),
            )
            flash("Auto retry disabled. Previous retry rules preserved.", "success")
            return redirect(url_for("customer.retry_settings"))

        try:
            max_attempts = int(request.form.get("max_attempts", 3))
        except ValueError:
            max_attempts = 3

        rules, errors = _validate_and_build_rules(request.form, max_attempts)

        if errors:
            # One summary flash instead of one per error.
            flash(
                f"Could not save retry settings — please fix {len(errors)} highlighted field(s): "
                + "; ".join(errors),
                "error",
            )
            return redirect(url_for("customer.retry_settings"))

        update_tenant_settings(tenant_id, {"retry_enabled": True})
        save_retry_rules(tenant_id, rules)

        flash("Retry & DND settings saved.", "success")
        return redirect(url_for("customer.retry_settings"))

    tenant = get_tenant_by_id(tenant_id) or {}
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        import json
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}

    rules = get_retry_rules(tenant_id)
    if not rules:
        # Defaults matching create_tenant()'s seed rules — shown even
        # before the merchant has saved anything.
        rules = [
            {"attempt_no": 1, "delay_minutes": 15, "retry_on": ["no_answer", "busy"],
             "dnd_start": datetime.time(21, 0), "dnd_end": datetime.time(9, 0),
             "max_attempts": 3, "is_enabled": True},
            {"attempt_no": 2, "delay_minutes": 30, "retry_on": ["no_answer", "busy"],
             "dnd_start": datetime.time(21, 0), "dnd_end": datetime.time(9, 0),
             "max_attempts": 3, "is_enabled": True},
            {"attempt_no": 3, "delay_minutes": 60, "retry_on": ["no_answer", "busy"],
             "dnd_start": datetime.time(21, 0), "dnd_end": datetime.time(9, 0),
             "max_attempts": 3, "is_enabled": True},
        ]

    # Convert time objects to strings, delay_minutes to {value, unit} —
    # JSON-serializable and matches the numeric value+unit fields the
    # form now submits.
    serializable_rules = []
    for r in rules:
        r_copy = dict(r)
        r_copy["dnd_start"] = r_copy["dnd_start"].strftime("%H:%M") if hasattr(r_copy["dnd_start"], "strftime") else str(r_copy["dnd_start"])
        r_copy["dnd_end"] = r_copy["dnd_end"].strftime("%H:%M") if hasattr(r_copy["dnd_end"], "strftime") else str(r_copy["dnd_end"])
        delay_value, delay_unit = _minutes_to_value_unit(r_copy["delay_minutes"])
        r_copy["delay_value"] = delay_value
        r_copy["delay_unit"] = delay_unit
        serializable_rules.append(r_copy)

    max_attempts = rules[0]["max_attempts"] if rules else 3
    dnd_start = rules[0]["dnd_start"] if rules else datetime.time(21, 0)
    dnd_end = rules[0]["dnd_end"] if rules else datetime.time(9, 0)
    retry_enabled = bool(settings.get("retry_enabled", True))

    return render_template(
        "customer/retry.html",
        rules=serializable_rules,
        max_attempts=max_attempts,
        dnd_start=dnd_start.strftime("%H:%M") if hasattr(dnd_start, "strftime") else str(dnd_start),
        dnd_end=dnd_end.strftime("%H:%M") if hasattr(dnd_end, "strftime") else str(dnd_end),
        retry_enabled=retry_enabled,
        retry_on_options=UI_RETRY_ON_OPTIONS,
        max_attempts_cap=MAX_ATTEMPTS_CAP,
    )


@customer_bp.route("/api/retry-rules", methods=["GET"])
@require_login
@require_role(["admin", "agent", "viewer"])
def api_retry_rules():
    """JSON for form prefill / external API clients — current saved rules
    for this tenant. Returns both the new structured shape (autoRetryEnabled,
    maxAttempts, retryRules) and the original flat "data" list (back-compat
    for anything already consuming that shape)."""
    tenant_id = g.tenant_id
    rules = get_retry_rules(tenant_id)

    tenant = get_tenant_by_id(tenant_id) or {}
    settings = tenant.get("settings") or {}
    if isinstance(settings, str):
        import json
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
    retry_enabled = bool(settings.get("retry_enabled", True))

    def _hm(t):
        return t.strftime("%H:%M") if hasattr(t, "strftime") else t

    retry_rules = [
        {
            "attempt": r["attempt_no"],
            "delayValue": _minutes_to_value_unit(r["delay_minutes"])[0],
            "delayUnit": _minutes_to_value_unit(r["delay_minutes"])[1],
            "retryOn": r["retry_on"],
        }
        for r in rules
    ]

    return jsonify({
        "success": True,
        "autoRetryEnabled": retry_enabled,
        "maxAttempts": rules[0]["max_attempts"] if rules else 3,
        "retryRules": retry_rules,
        "dndStart": _hm(rules[0]["dnd_start"]) if rules else "21:00",
        "dndEnd": _hm(rules[0]["dnd_end"]) if rules else "09:00",
        "timezone": "Asia/Dhaka",
        "data": [
            {
                "attempt_no": r["attempt_no"],
                "delay_minutes": r["delay_minutes"],
                "delay_value": _minutes_to_value_unit(r["delay_minutes"])[0],
                "delay_unit": _minutes_to_value_unit(r["delay_minutes"])[1],
                "retry_on": r["retry_on"],
                "dnd_start": _hm(r["dnd_start"]),
                "dnd_end": _hm(r["dnd_end"]),
                "max_attempts": r["max_attempts"],
                "is_enabled": r["is_enabled"],
            }
            for r in rules
        ],
    })
