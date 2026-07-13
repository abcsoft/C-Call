"""Admin routes: Plan management, Platform TTS credentials, TTS usage.

All routes require @require_super_admin.
State-changing routes require CSRF.
No secret values in URLs, GET responses, logs, or HTML source.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.blueprints.admin import admin_bp
from app.blueprints.auth.decorators import require_super_admin
from app.db import db
from app.middleware.audit import audit_log
from app.models.plan_tts_policy import (
    VALID_PROVIDERS,
    get_plan_tts_policy,
)
from app.models.platform_secrets import (
    ALLOWED_PROVIDERS as PLATFORM_PROVIDERS,
    disable_platform_secret,
    get_platform_secret_meta,
    list_platform_secret_meta,
    platform_secret_is_active,
    save_platform_secret,
    update_test_status,
)
logger = logging.getLogger("ccall")

# ── helpers ───────────────────────────────────────────────────────────────────

CANONICAL_PROVIDERS = sorted(VALID_PROVIDERS)
CREDENTIAL_POLICIES = ("byok_only", "platform_managed", "hybrid")


def _all_plans_with_policies() -> list[dict[str, Any]]:
    """Return all plans joined with TTS policies and tenant counts. No N+1."""
    rows = db.query(
        """
        SELECT
            p.id,
            p.name,
            p.price_monthly,
            p.included_minutes,
            p.max_flows,
            p.max_users,
            p.feature_flags,
            p.created_at,
            p.archived_at,
            pol.id           AS policy_id,
            pol.credential_policy,
            pol.allowed_providers,
            pol.byok_providers,
            pol.platform_managed_providers,
            pol.monthly_character_quota,
            pol.monthly_audio_seconds_quota,
            pol.daily_preview_limit,
            pol.extra_usage_policy,
            pol.max_concurrent_tts,
            pol.premium_voices_enabled,
            COALESCE(tc.cnt, 0) AS tenant_count
        FROM plans p
        LEFT JOIN plan_tts_policies pol ON pol.plan_id = p.id
        LEFT JOIN (
            SELECT plan_id, COUNT(*) AS cnt FROM tenants GROUP BY plan_id
        ) tc ON tc.plan_id = p.id
        ORDER BY p.id
        """
    )
    result = []
    for row in rows:
        d = dict(row)
        for field in ("allowed_providers", "byok_providers", "platform_managed_providers"):
            v = d.get(field)
            if v is None:
                d[field] = []
            elif isinstance(v, str):
                try:
                    d[field] = json.loads(v)
                except (ValueError, TypeError):
                    d[field] = []
        if d.get("feature_flags") is None:
            d["feature_flags"] = {}
        result.append(d)
    return result


def _get_plan_or_404(plan_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM plans WHERE id = %s", (plan_id,))
    if not row:
        abort(404)
    return dict(row)


def _parse_providers_input(raw: str) -> list[str]:
    """Parse comma-separated provider input, filter to valid providers."""
    if not raw:
        return []
    items = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return [p for p in items if p in VALID_PROVIDERS]


def _validate_plan_form(form) -> tuple[dict, list[str]]:
    """Validate plan create/edit form. Returns (data_dict, errors_list)."""
    errors = []
    data: dict[str, Any] = {}

    name = (form.get("name") or "").strip()
    if not name:
        errors.append("Plan name is required.")
    data["name"] = name

    try:
        data["price_monthly"] = Decimal(form.get("price_monthly") or "0")
        if data["price_monthly"] < 0:
            errors.append("Price cannot be negative.")
    except InvalidOperation:
        errors.append("Invalid price value.")
        data["price_monthly"] = Decimal("0")

    for field, label in [
        ("included_minutes", "Included minutes"),
        ("max_flows", "Max flows"),
        ("max_users", "Max users"),
    ]:
        try:
            val = int(form.get(field) or 0)
            if val < 0:
                errors.append(f"{label} cannot be negative.")
            data[field] = val
        except (ValueError, TypeError):
            errors.append(f"Invalid {label} value.")
            data[field] = 0

    # TTS policy fields
    credential_policy = form.get("credential_policy", "byok_only")
    if credential_policy not in CREDENTIAL_POLICIES:
        errors.append("Invalid credential policy.")
        credential_policy = "byok_only"
    data["credential_policy"] = credential_policy

    allowed = _parse_providers_input(form.get("allowed_providers", ""))
    byok = _parse_providers_input(form.get("byok_providers", ""))
    platform = _parse_providers_input(form.get("platform_managed_providers", ""))

    # Subset checks
    invalid_byok = [p for p in byok if p not in allowed]
    if invalid_byok:
        errors.append(f"BYOK providers {invalid_byok} not in allowed_providers.")

    invalid_plat = [p for p in platform if p not in allowed]
    if invalid_plat:
        errors.append(f"Platform providers {invalid_plat} not in allowed_providers.")

    # Policy consistency
    if credential_policy == "byok_only" and platform:
        errors.append("byok_only policy cannot have platform-managed providers.")
        platform = []

    if credential_policy == "platform_managed" and not platform:
        errors.append("platform_managed policy requires at least one platform provider.")

    data["allowed_providers"] = allowed
    data["byok_providers"] = byok
    data["platform_managed_providers"] = platform

    try:
        quota_char = form.get("monthly_character_quota", "").strip()
        data["monthly_character_quota"] = int(quota_char) if quota_char else None
        if data["monthly_character_quota"] is not None and data["monthly_character_quota"] < 0:
            errors.append("Monthly character quota cannot be negative.")
    except ValueError:
        errors.append("Invalid monthly character quota.")
        data["monthly_character_quota"] = None

    try:
        quota_sec = form.get("monthly_audio_seconds_quota", "").strip()
        data["monthly_audio_seconds_quota"] = int(quota_sec) if quota_sec else None
        if data["monthly_audio_seconds_quota"] is not None and data["monthly_audio_seconds_quota"] < 0:
            errors.append("Monthly audio quota cannot be negative.")
    except ValueError:
        errors.append("Invalid monthly audio quota.")
        data["monthly_audio_seconds_quota"] = None

    try:
        data["daily_preview_limit"] = int(form.get("daily_preview_limit") or 10)
        if data["daily_preview_limit"] < 0:
            errors.append("Daily preview limit cannot be negative.")
    except (ValueError, TypeError):
        errors.append("Invalid daily preview limit.")
        data["daily_preview_limit"] = 10

    try:
        data["max_concurrent_tts"] = int(form.get("max_concurrent_tts") or 1)
        if data["max_concurrent_tts"] < 1:
            errors.append("Max concurrent TTS must be at least 1.")
    except (ValueError, TypeError):
        errors.append("Invalid max concurrent TTS.")
        data["max_concurrent_tts"] = 1

    data["premium_voices_enabled"] = bool(form.get("premium_voices_enabled"))
    data["extra_usage_policy"] = "block"  # only allowed value for now

    return data, errors


# ── Plan list ─────────────────────────────────────────────────────────────────

@admin_bp.route("/plans", methods=["GET"])
@require_super_admin
def plans_list():
    plans = _all_plans_with_policies()
    return render_template(
        "admin/plans/list.html",
        plans=plans,
        canonical_providers=CANONICAL_PROVIDERS,
    )


# ── Plan create ───────────────────────────────────────────────────────────────

@admin_bp.route("/plans/create", methods=["GET", "POST"])
@require_super_admin
def plan_create():
    if request.method == "GET":
        return render_template(
            "admin/plans/form.html",
            plan=None,
            policy=None,
            canonical_providers=CANONICAL_PROVIDERS,
            credential_policies=CREDENTIAL_POLICIES,
            form_values={},
        )

    data, errors = _validate_plan_form(request.form)
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template(
            "admin/plans/form.html",
            plan=None,
            policy=None,
            canonical_providers=CANONICAL_PROVIDERS,
            credential_policies=CREDENTIAL_POLICIES,
            form_values=dict(request.form),
        ), 422

    # Duplicate name check
    existing = db.query_one("SELECT id FROM plans WHERE name = %s", (data["name"],))
    if existing:
        flash(f"A plan named '{data['name']}' already exists.", "error")
        return render_template(
            "admin/plans/form.html",
            plan=None,
            policy=None,
            canonical_providers=CANONICAL_PROVIDERS,
            credential_policies=CREDENTIAL_POLICIES,
            form_values=dict(request.form),
        ), 422

    # Atomic create: plan + TTS policy in one transaction
    try:
        with db.transaction():
            plan_id = db.insert_returning_id(
                """
                INSERT INTO plans (name, price_monthly, included_minutes, max_flows, max_users, feature_flags)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
                """,
                (
                    data["name"],
                    data["price_monthly"],
                    data["included_minutes"],
                    data["max_flows"],
                    data["max_users"],
                    json.dumps({}),
                ),
            )
            db.execute(
                """
                INSERT INTO plan_tts_policies (
                    plan_id, credential_policy, allowed_providers, byok_providers,
                    platform_managed_providers, monthly_character_quota,
                    monthly_audio_seconds_quota, daily_preview_limit,
                    extra_usage_policy, max_concurrent_tts, premium_voices_enabled
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    plan_id,
                    data["credential_policy"],
                    json.dumps(data["allowed_providers"]),
                    json.dumps(data["byok_providers"]),
                    json.dumps(data["platform_managed_providers"]),
                    data["monthly_character_quota"],
                    data["monthly_audio_seconds_quota"],
                    data["daily_preview_limit"],
                    data["extra_usage_policy"],
                    data["max_concurrent_tts"],
                    data["premium_voices_enabled"],
                ),
            )
    except Exception as exc:
        logger.error("plan_create transaction failed: %s", exc, exc_info=True)
        flash("Failed to create plan. Please try again.", "error")
        return render_template(
            "admin/plans/form.html",
            plan=None,
            policy=None,
            canonical_providers=CANONICAL_PROVIDERS,
            credential_policies=CREDENTIAL_POLICIES,
            form_values=dict(request.form),
        ), 500

    audit_log(
        "plan_created",
        current_user.id,
        "plan",
        plan_id,
        {
            "name": data["name"],
            "credential_policy": data["credential_policy"],
            "allowed_providers": data["allowed_providers"],
            "platform_managed_providers": data["platform_managed_providers"],
        },
    )
    flash(f"Plan '{data['name']}' created successfully.", "success")
    return redirect(url_for("admin.plans_list"))


# ── Plan edit ─────────────────────────────────────────────────────────────────

@admin_bp.route("/plans/<int:plan_id>/edit", methods=["GET", "POST"])
@require_super_admin
def plan_edit(plan_id: int):
    plan = _get_plan_or_404(plan_id)
    policy = get_plan_tts_policy(plan_id)

    if request.method == "GET":
        return render_template(
            "admin/plans/form.html",
            plan=plan,
            policy=policy,
            canonical_providers=CANONICAL_PROVIDERS,
            credential_policies=CREDENTIAL_POLICIES,
            form_values={},
        )

    data, errors = _validate_plan_form(request.form)
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template(
            "admin/plans/form.html",
            plan=plan,
            policy=policy,
            canonical_providers=CANONICAL_PROVIDERS,
            credential_policies=CREDENTIAL_POLICIES,
            form_values=dict(request.form),
        ), 422

    # Duplicate name check (exclude self)
    existing = db.query_one(
        "SELECT id FROM plans WHERE name = %s AND id != %s",
        (data["name"], plan_id),
    )
    if existing:
        flash(f"A plan named '{data['name']}' already exists.", "error")
        return render_template(
            "admin/plans/form.html",
            plan=plan,
            policy=policy,
            canonical_providers=CANONICAL_PROVIDERS,
            credential_policies=CREDENTIAL_POLICIES,
            form_values=dict(request.form),
        ), 422

    # Downgrade warning: how many tenants are in platform_managed mode on this plan?
    affected_count = 0
    new_policy_blocks_platform = (data["credential_policy"] == "byok_only")
    if new_policy_blocks_platform:
        row = db.query_one(
            "SELECT COUNT(*) AS cnt FROM tenants WHERE plan_id = %s AND tts_credential_mode = 'platform_managed'",
            (plan_id,),
        )
        affected_count = int(row["cnt"]) if row else 0

    # If downgrade confirmation not provided, show warning page
    if affected_count > 0 and not request.form.get("confirm_downgrade"):
        flash(
            f"Warning: {affected_count} tenant(s) are currently using platform-managed TTS on this plan. "
            "Saving will deny their TTS generation until they switch back to BYOK. "
            "Check 'Confirm downgrade' to proceed.",
            "warning",
        )
        return render_template(
            "admin/plans/form.html",
            plan=plan,
            policy=policy,
            canonical_providers=CANONICAL_PROVIDERS,
            credential_policies=CREDENTIAL_POLICIES,
            form_values=dict(request.form),
            affected_count=affected_count,
        ), 200

    # Build before-diff for audit (no secrets)
    before_snap = {
        "name": plan.get("name"),
        "price_monthly": str(plan.get("price_monthly", "")),
        "credential_policy": policy.get("credential_policy"),
        "allowed_providers": policy.get("allowed_providers"),
        "platform_managed_providers": policy.get("platform_managed_providers"),
    }

    try:
        with db.transaction():
            db.execute(
                """
                UPDATE plans SET
                    name = %s, price_monthly = %s, included_minutes = %s,
                    max_flows = %s, max_users = %s
                WHERE id = %s
                """,
                (
                    data["name"], data["price_monthly"], data["included_minutes"],
                    data["max_flows"], data["max_users"], plan_id,
                ),
            )
            db.execute(
                """
                UPDATE plan_tts_policies SET
                    credential_policy = %s,
                    allowed_providers = %s,
                    byok_providers = %s,
                    platform_managed_providers = %s,
                    monthly_character_quota = %s,
                    monthly_audio_seconds_quota = %s,
                    daily_preview_limit = %s,
                    extra_usage_policy = %s,
                    max_concurrent_tts = %s,
                    premium_voices_enabled = %s,
                    updated_at = now()
                WHERE plan_id = %s
                """,
                (
                    data["credential_policy"],
                    json.dumps(data["allowed_providers"]),
                    json.dumps(data["byok_providers"]),
                    json.dumps(data["platform_managed_providers"]),
                    data["monthly_character_quota"],
                    data["monthly_audio_seconds_quota"],
                    data["daily_preview_limit"],
                    data["extra_usage_policy"],
                    data["max_concurrent_tts"],
                    data["premium_voices_enabled"],
                    plan_id,
                ),
            )
    except Exception as exc:
        logger.error("plan_edit transaction failed: %s", exc, exc_info=True)
        flash("Failed to update plan. Please try again.", "error")
        return render_template(
            "admin/plans/form.html",
            plan=plan,
            policy=policy,
            canonical_providers=CANONICAL_PROVIDERS,
            credential_policies=CREDENTIAL_POLICIES,
            form_values=dict(request.form),
        ), 500

    after_snap = {
        "name": data["name"],
        "price_monthly": str(data["price_monthly"]),
        "credential_policy": data["credential_policy"],
        "allowed_providers": data["allowed_providers"],
        "platform_managed_providers": data["platform_managed_providers"],
    }
    audit_log(
        "plan_updated",
        current_user.id,
        "plan",
        plan_id,
        {"before": before_snap, "after": after_snap, "affected_tenants": affected_count},
    )
    flash(f"Plan '{data['name']}' updated.", "success")
    return redirect(url_for("admin.plans_list"))


# ── Plan archive ──────────────────────────────────────────────────────────────

@admin_bp.route("/plans/<int:plan_id>/archive", methods=["POST"])
@require_super_admin
def plan_archive(plan_id: int):
    plan = _get_plan_or_404(plan_id)

    # Block archive if tenants are assigned
    row = db.query_one(
        "SELECT COUNT(*) AS cnt FROM tenants WHERE plan_id = %s",
        (plan_id,),
    )
    if row and int(row["cnt"]) > 0:
        flash(
            f"Cannot archive plan '{plan['name']}' — {row['cnt']} tenant(s) are currently assigned. "
            "Reassign them first.",
            "error",
        )
        return redirect(url_for("admin.plans_list"))

    db.execute(
        "UPDATE plans SET archived_at = now() WHERE id = %s AND archived_at IS NULL",
        (plan_id,),
    )
    audit_log(
        "plan_archived",
        current_user.id,
        "plan",
        plan_id,
        {"name": plan.get("name")},
    )
    flash(f"Plan '{plan['name']}' archived.", "success")
    return redirect(url_for("admin.plans_list"))


# ── Platform TTS page ─────────────────────────────────────────────────────────

@admin_bp.route("/platform-tts", methods=["GET"])
@require_super_admin
def platform_tts_page():
    secrets_meta = {p: None for p in sorted(PLATFORM_PROVIDERS)}
    for meta in list_platform_secret_meta():
        secrets_meta[meta["provider"]] = meta

    # Plans using each provider in platform_managed mode
    plans_using = {p: [] for p in sorted(PLATFORM_PROVIDERS)}
    plan_rows = db.query(
        """
        SELECT p.id, p.name, pol.platform_managed_providers
        FROM plans p
        JOIN plan_tts_policies pol ON pol.plan_id = p.id
        WHERE p.archived_at IS NULL
        """
    )
    for row in plan_rows:
        provs = row["platform_managed_providers"] or []
        if isinstance(provs, str):
            try:
                provs = json.loads(provs)
            except Exception:
                provs = []
        for prov in provs:
            if prov in plans_using:
                plans_using[prov].append({"id": row["id"], "name": row["name"]})

    # Monthly usage totals per platform provider
    usage_totals = {}
    today = date.today()
    billing_month = today.replace(day=1)
    usage_rows = db.query(
        """
        SELECT provider, SUM(characters_used) AS total_chars,
               SUM(production_requests + preview_requests) AS total_requests
        FROM tenant_tts_usage
        WHERE credential_mode = 'platform_managed' AND billing_month = %s
        GROUP BY provider
        """,
        (billing_month,),
    )
    for row in usage_rows:
        usage_totals[row["provider"]] = {
            "total_chars": int(row["total_chars"] or 0),
            "total_requests": int(row["total_requests"] or 0),
        }

    return render_template(
        "admin/platform_tts.html",
        secrets_meta=secrets_meta,
        plans_using=plans_using,
        usage_totals=usage_totals,
        providers=sorted(PLATFORM_PROVIDERS),
    )


# ── Save platform credential ──────────────────────────────────────────────────

@admin_bp.route("/platform-tts/<provider>/save", methods=["POST"])
@require_super_admin
def platform_tts_save(provider: str):
    if provider not in PLATFORM_PROVIDERS:
        abort(404)

    api_key = request.form.get("api_key", "").strip()
    reason = request.form.get("reason", "").strip()
    confirmed = bool(request.form.get("confirm_save"))

    if not api_key:
        flash("API key must not be empty.", "error")
        return redirect(url_for("admin.platform_tts_page"))
    if not confirmed:
        flash("Please check the confirmation checkbox.", "error")
        return redirect(url_for("admin.platform_tts_page"))

    was_configured = get_platform_secret_meta(provider) is not None

    try:
        save_platform_secret(provider, api_key, updated_by_user_id=current_user.id)
    except RuntimeError as exc:
        flash(f"Encryption key error: {exc}", "error")
        return redirect(url_for("admin.platform_tts_page"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.platform_tts_page"))

    audit_log(
        "platform_credential_configured" if not was_configured else "platform_credential_replaced",
        current_user.id,
        "platform_secret",
        None,
        {
            "provider": provider,
            "action": "configured" if not was_configured else "replaced",
            "reason": reason or "(no reason given)",
            # No key value, no encrypted value — only last_four via metadata
        },
    )
    flash(f"{provider.title()} platform credential saved (last 4: ****{api_key[-4:]}).", "success")
    return redirect(url_for("admin.platform_tts_page"))


# ── Test platform credential ──────────────────────────────────────────────────

@admin_bp.route("/platform-tts/<provider>/test", methods=["POST"])
@require_super_admin
def platform_tts_test(provider: str):
    if provider not in PLATFORM_PROVIDERS:
        abort(404)

    meta = get_platform_secret_meta(provider)
    if not meta:
        flash(f"No {provider} credential configured.", "error")
        return redirect(url_for("admin.platform_tts_page"))
    if meta.get("status") == "disabled":
        flash(f"{provider} credential is disabled. Re-configure to test.", "warning")
        return redirect(url_for("admin.platform_tts_page"))

    success, message = _test_provider_credential(provider)
    update_test_status(provider, success)

    audit_log(
        "platform_credential_tested",
        current_user.id,
        "platform_secret",
        None,
        {"provider": provider, "result": "ok" if success else "failed"},
    )

    if success:
        flash(f"{provider.title()} credential is valid. ✓", "success")
    else:
        flash(f"{provider.title()} credential test failed: {message}", "error")
    return redirect(url_for("admin.platform_tts_page"))


def _test_provider_credential(provider: str) -> tuple[bool, str]:
    """Make a cheap liveness check for the platform credential.

    SECURITY: decrypted key is used inside this function only; never logged,
    returned in response, or serialized.
    """
    from app.models.platform_secrets import get_platform_secret_for_generation
    api_key = get_platform_secret_for_generation(provider)
    if not api_key:
        return False, "Credential unavailable or decryption failed."

    if provider == "gemini":
        return _test_gemini(api_key)
    if provider == "elevenlabs":
        return _test_elevenlabs(api_key)
    return False, "Unknown provider."


def _test_gemini(api_key: str) -> tuple[bool, str]:
    """Minimal Gemini API liveness check — model list endpoint (no audio generation)."""
    import urllib.request
    import urllib.error
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}&pageSize=1"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                return True, "ok"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code == 400:
            return False, "Invalid API key (400)."
        if code == 403:
            return False, "API key lacks permission (403)."
        if code == 429:
            return False, "Rate limited (429)."
        return False, f"HTTP error {code}."
    except urllib.error.URLError as exc:
        return False, f"Network error: {type(exc.reason).__name__}"
    except Exception:
        return False, "Unexpected error during test."


def _test_elevenlabs(api_key: str) -> tuple[bool, str]:
    """Minimal ElevenLabs API liveness check — user endpoint (no generation)."""
    import urllib.request
    import urllib.error
    url = "https://api.elevenlabs.io/v1/user"
    try:
        req = urllib.request.Request(url, headers={"xi-api-key": api_key}, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                return True, "ok"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code == 401:
            return False, "Invalid API key (401)."
        if code == 429:
            return False, "Rate limited (429)."
        return False, f"HTTP error {code}."
    except urllib.error.URLError as exc:
        return False, f"Network error: {type(exc.reason).__name__}"
    except Exception:
        return False, "Unexpected error during test."


# ── Disable platform credential ───────────────────────────────────────────────

@admin_bp.route("/platform-tts/<provider>/disable", methods=["POST"])
@require_super_admin
def platform_tts_disable(provider: str):
    if provider not in PLATFORM_PROVIDERS:
        abort(404)

    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("A reason is required to disable a platform credential.", "error")
        return redirect(url_for("admin.platform_tts_page"))

    meta = get_platform_secret_meta(provider)
    if not meta:
        flash(f"No {provider} credential exists to disable.", "warning")
        return redirect(url_for("admin.platform_tts_page"))

    disable_platform_secret(provider, updated_by_user_id=current_user.id)

    audit_log(
        "platform_credential_disabled",
        current_user.id,
        "platform_secret",
        None,
        {"provider": provider, "reason": reason},
    )
    flash(f"{provider.title()} credential disabled. Existing BYOK users are unaffected.", "success")
    return redirect(url_for("admin.platform_tts_page"))


# ── TTS usage page ────────────────────────────────────────────────────────────

@admin_bp.route("/tts-usage", methods=["GET"])
@require_super_admin
def tts_usage_page():
    # Filters (validated)
    today = date.today()
    default_month = today.replace(day=1).strftime("%Y-%m")

    filter_month = request.args.get("month", default_month).strip()
    try:
        billing_month = datetime.strptime(filter_month, "%Y-%m").date().replace(day=1)
    except ValueError:
        billing_month = today.replace(day=1)
        filter_month = billing_month.strftime("%Y-%m")

    filter_tenant = request.args.get("tenant_id", "").strip()
    filter_plan = request.args.get("plan_id", "").strip()
    filter_provider = request.args.get("provider", "").strip()
    filter_mode = request.args.get("credential_mode", "").strip()

    tenant_id_int = None
    if filter_tenant:
        try:
            tenant_id_int = int(filter_tenant)
        except ValueError:
            filter_tenant = ""

    plan_id_int = None
    if filter_plan:
        try:
            plan_id_int = int(filter_plan)
        except ValueError:
            filter_plan = ""

    # Validate provider filter
    if filter_provider and filter_provider not in VALID_PROVIDERS:
        filter_provider = ""
    if filter_mode and filter_mode not in ("byok", "platform_managed"):
        filter_mode = ""

    # Build query with filters
    where_clauses = ["u.billing_month = %s"]
    params: list[Any] = [billing_month]

    if tenant_id_int:
        where_clauses.append("u.tenant_id = %s")
        params.append(tenant_id_int)
    if filter_provider:
        where_clauses.append("u.provider = %s")
        params.append(filter_provider)
    if filter_mode:
        where_clauses.append("u.credential_mode = %s")
        params.append(filter_mode)

    where_sql = " AND ".join(where_clauses)

    # Pagination
    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = 50
    offset = (page - 1) * per_page

    total_row = db.query_one(
        f"""
        SELECT COUNT(*) AS cnt
        FROM tenant_tts_usage u
        JOIN tenants t ON t.id = u.tenant_id
        WHERE {where_sql}
        """ + (" AND t.plan_id = %s" if plan_id_int else ""),
        params + ([plan_id_int] if plan_id_int else []),
    )
    total = int(total_row["cnt"]) if total_row else 0

    rows = db.query(
        f"""
        SELECT
            u.tenant_id,
            t.business_name AS tenant_name,
            t.plan_id,
            pl.name AS plan_name,
            u.provider,
            u.credential_mode,
            u.characters_used,
            u.audio_seconds,
            u.preview_requests,
            u.production_requests,
            u.failed_requests,
            u.estimated_provider_cost,
            u.updated_at,
            pol.monthly_character_quota
        FROM tenant_tts_usage u
        JOIN tenants t ON t.id = u.tenant_id
        LEFT JOIN plans pl ON pl.id = t.plan_id
        LEFT JOIN plan_tts_policies pol ON pol.plan_id = t.plan_id
            AND u.credential_mode = 'platform_managed'
        WHERE {where_sql}
        {"AND t.plan_id = %s" if plan_id_int else ""}
        ORDER BY u.characters_used DESC, u.tenant_id
        LIMIT %s OFFSET %s
        """,
        params + ([plan_id_int] if plan_id_int else []) + [per_page, offset],
    )

    # Aggregate totals
    totals_row = db.query_one(
        f"""
        SELECT
            COALESCE(SUM(characters_used), 0) AS total_chars,
            COALESCE(SUM(audio_seconds), 0) AS total_seconds,
            COALESCE(SUM(preview_requests), 0) AS total_previews,
            COALESCE(SUM(production_requests), 0) AS total_production,
            COALESCE(SUM(failed_requests), 0) AS total_failed
        FROM tenant_tts_usage u
        JOIN tenants t ON t.id = u.tenant_id
        WHERE {where_sql}
        {"AND t.plan_id = %s" if plan_id_int else ""}
        """,
        params + ([plan_id_int] if plan_id_int else []),
    )

    # Tenant and plan lists for filter dropdowns
    all_tenants = db.query("SELECT id, business_name FROM tenants ORDER BY business_name")
    all_plans = db.query("SELECT id, name FROM plans WHERE archived_at IS NULL ORDER BY name")
    all_providers = sorted(VALID_PROVIDERS)

    return render_template(
        "admin/tts_usage.html",
        rows=[dict(r) for r in rows],
        totals=dict(totals_row) if totals_row else {},
        total=total,
        page=page,
        per_page=per_page,
        pages=max(1, -(-total // per_page)),
        filter_month=filter_month,
        filter_tenant=filter_tenant,
        filter_plan=filter_plan,
        filter_provider=filter_provider,
        filter_mode=filter_mode,
        all_tenants=[dict(r) for r in all_tenants],
        all_plans=[dict(r) for r in all_plans],
        all_providers=all_providers,
    )


# ── TTS Reservation status ────────────────────────────────────────────────────

@admin_bp.route("/tts-reservations", methods=["GET"])
@require_super_admin
def tts_reservations_page():
    """Admin view: inspect tts_usage_reservations for debugging quota/billing issues."""
    today = date.today()
    default_month = today.replace(day=1).strftime("%Y-%m")

    filter_month = request.args.get("month", default_month).strip()
    try:
        billing_month = datetime.strptime(filter_month, "%Y-%m").date().replace(day=1)
    except ValueError:
        billing_month = today.replace(day=1)
        filter_month = billing_month.strftime("%Y-%m")

    filter_tenant = request.args.get("tenant_id", "").strip()
    filter_status = request.args.get("status", "").strip()
    filter_provider = request.args.get("provider", "").strip()

    tenant_id_int = None
    if filter_tenant:
        try:
            tenant_id_int = int(filter_tenant)
        except ValueError:
            filter_tenant = ""

    VALID_STATUSES = {"reserved", "committed", "released", "failed", "expired"}
    if filter_status and filter_status not in VALID_STATUSES:
        filter_status = ""
    if filter_provider and filter_provider not in VALID_PROVIDERS:
        filter_provider = ""

    where_clauses = ["r.billing_month = %s"]
    params: list[Any] = [billing_month]
    if tenant_id_int:
        where_clauses.append("r.tenant_id = %s")
        params.append(tenant_id_int)
    if filter_status:
        where_clauses.append("r.status = %s")
        params.append(filter_status)
    if filter_provider:
        where_clauses.append("r.provider = %s")
        params.append(filter_provider)

    where_sql = " AND ".join(where_clauses)

    page = max(1, int(request.args.get("page", 1) or 1))
    per_page = 100
    offset = (page - 1) * per_page

    total_row = db.query_one(
        f"SELECT COUNT(*) AS cnt FROM tts_usage_reservations r WHERE {where_sql}",
        params,
    )
    total = int(total_row["cnt"]) if total_row else 0

    rows = db.query(
        f"""
        SELECT
            r.id,
            r.tenant_id,
            t.business_name AS tenant_name,
            r.provider,
            r.credential_mode,
            r.purpose,
            r.billing_month,
            r.characters_reserved,
            r.quota_characters_reserved,
            r.overage_characters_reserved,
            r.estimated_overage_amount,
            r.actual_characters,
            r.actual_overage_amount,
            r.status,
            r.error_code,
            r.wallet_transaction_id,
            r.created_at,
            r.committed_at,
            r.released_at,
            r.expires_at
        FROM tts_usage_reservations r
        JOIN tenants t ON t.id = r.tenant_id
        WHERE {where_sql}
        ORDER BY r.created_at DESC
        LIMIT %s OFFSET %s
        """,
        params + [per_page, offset],
    )

    status_counts = db.query(
        f"""
        SELECT status, COUNT(*) AS cnt,
               COALESCE(SUM(characters_reserved), 0) AS total_chars,
               COALESCE(SUM(estimated_overage_amount), 0) AS total_overage
        FROM tts_usage_reservations r
        WHERE {where_sql}
        GROUP BY status ORDER BY status
        """,
        params,
    )

    all_tenants = db.query("SELECT id, business_name FROM tenants ORDER BY business_name")
    all_providers = sorted(VALID_PROVIDERS)

    return render_template(
        "admin/tts_reservations.html",
        rows=[dict(r) for r in rows],
        status_counts=[dict(r) for r in status_counts],
        total=total,
        page=page,
        per_page=per_page,
        pages=max(1, -(-total // per_page)),
        filter_month=filter_month,
        filter_tenant=filter_tenant,
        filter_status=filter_status,
        filter_provider=filter_provider,
        all_tenants=[dict(r) for r in all_tenants],
        all_providers=all_providers,
        valid_statuses=sorted(VALID_STATUSES),
    )


# ── Tenant TTS mode override ──────────────────────────────────────────────────

@admin_bp.route("/customers/<int:tenant_id>/tts-mode", methods=["POST"])
@require_super_admin
def tenant_tts_mode_override(tenant_id: int):
    from app.models.tenant import get_tenant_by_id
    from app.models.plan_tts_policy import mode_allowed_for_plan
    from app.models.platform_secrets import platform_secret_is_active

    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        abort(404)

    new_mode = request.form.get("tts_credential_mode", "").strip()
    reason = request.form.get("reason", "").strip()

    if new_mode not in ("byok", "platform_managed"):
        flash("Invalid credential mode.", "error")
        return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))
    if not reason:
        flash("A reason is required to change TTS mode.", "error")
        return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))

    plan_id = tenant.get("plan_id")

    if not mode_allowed_for_plan(plan_id, new_mode):
        flash(
            f"Mode '{new_mode}' is not allowed by the tenant's current plan policy.",
            "error",
        )
        return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))

    # Extra check for platform_managed: need at least one active platform secret
    if new_mode == "platform_managed":
        from app.models.plan_tts_policy import get_plan_tts_policy
        policy = get_plan_tts_policy(plan_id)
        platform_providers = policy.get("platform_managed_providers", [])
        any_active = any(platform_secret_is_active(p) for p in platform_providers)
        if not any_active:
            flash(
                "Cannot switch to platform-managed: no active platform credentials for this plan's providers.",
                "error",
            )
            return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))

    old_mode = tenant.get("tts_credential_mode", "byok")

    db.execute(
        "UPDATE tenants SET tts_credential_mode = %s WHERE id = %s",
        (new_mode, tenant_id),
    )

    # Invalidate tenant settings cache if Redis available
    try:
        from app.extensions import redis_ext
        cache_key = f"tenant_settings:{tenant_id}"
        redis_ext.delete(cache_key)
    except Exception:
        pass

    audit_log(
        "tenant_tts_mode_changed",
        current_user.id,
        "tenant",
        tenant_id,
        {
            "old_mode": old_mode,
            "new_mode": new_mode,
            "reason": reason,
            "tenant_name": tenant.get("business_name"),
        },
    )
    flash(
        f"TTS mode for '{tenant.get('business_name')}' changed from {old_mode} to {new_mode}.",
        "success",
    )
    return redirect(url_for("admin.customer_detail", tenant_id=tenant_id))
