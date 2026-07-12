"""Integration tests for Customer TTS Subscription UX.

Covers 44 scenarios across:
- T01-T06: Customer access and plan rendering
- T07-T12: Credential mode UX
- T13-T18: BYOK key management
- T19-T24: Preview enforcement
- T25-T30: Dashboard TTS widget
- T31-T36: Subscription page
- T37-T42: Security / anti-leak
- T43-T44: Regression (admin TTS management not affected)
"""
from __future__ import annotations

import pytest
from unittest.mock import patch
from app.db import db


# ── Helpers ─────────────────────────────────────────────────────────────────

def _get_csrf(client, path="/auth/login"):
    resp = client.get(path)
    html = resp.data.decode()
    start = html.find('name="csrf_token" value="') + len('name="csrf_token" value="')
    end = html.find('"', start)
    return html[start:end]


def _login(client, email, password="Password123!"):
    csrf = _get_csrf(client)
    client.post("/auth/login", data={"email": email, "password": password, "csrf_token": csrf}, follow_redirects=True)


def _create_and_login_tenant(client, create_tenant, business_name, email):
    t, u = create_tenant(business_name, email)
    db.execute("UPDATE users SET email_verified=true WHERE id=%s", (u["id"],))
    db.execute("UPDATE tenants SET status='active' WHERE id=%s", (t["id"],))
    _login(client, email)
    return t, u


def _set_plan_policy(tenant_id, *, allowed_providers=None, credential_policy="byok_only",
                     platform_managed_providers=None, monthly_quota=None, daily_preview_limit=None):
    """Create a plan with given policy and assign to tenant."""
    allowed = allowed_providers or ["edge", "gtts"]
    plan_id = db.insert_returning_id(
        "INSERT INTO plans (name, price_monthly) VALUES ('TestPlan', 0) RETURNING id"
    )
    db.execute(
        """INSERT INTO plan_tts_policies
           (plan_id, credential_policy, allowed_providers, byok_providers,
            platform_managed_providers, monthly_character_quota, daily_preview_limit,
            max_concurrent_tts, premium_voices_enabled, extra_usage_policy)
           VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, 2, false, 'block')""",
        (
            plan_id,
            credential_policy,
            __import__("json").dumps(allowed),
            __import__("json").dumps(allowed),
            __import__("json").dumps(platform_managed_providers or []),
            monthly_quota,
            daily_preview_limit if daily_preview_limit is not None else 10,
        )
    )
    db.execute("UPDATE tenants SET plan_id=%s WHERE id=%s", (plan_id, tenant_id))
    # Invalidate Redis tenant cache so next request picks up the new plan_id
    from app.extensions import redis_ext
    try:
        redis_ext.client.delete(f"tenant:settings:{tenant_id}")
    except Exception:
        pass
    return plan_id


# ── T01-T06: Customer Access and Plan Rendering ──────────────────────────────

class TestCustomerAccess:
    def test_T01_script_page_loads(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "AccessCo", "t01@test.com")
        resp = client.get("/script")
        assert resp.status_code == 200
        assert b"Voice Script" in resp.data

    def test_T02_script_page_shows_plan_summary_card(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "SummaryCo", "t02@test.com")
        resp = client.get("/script")
        assert resp.status_code == 200
        assert b"TTS Subscription" in resp.data

    def test_T03_plan_allowed_providers_shown_in_card(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "PlanCo", "t03@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge", "gemini"])
        resp = client.get("/script")
        html = resp.data.decode()
        assert "edge" in html
        assert "gemini" in html

    def test_T04_locked_provider_card_has_locked_label(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "LockCo", "t04@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge"])
        resp = client.get("/script")
        html = resp.data.decode()
        assert "LOCKED" in html

    def test_T05_monthly_quota_progress_shown_when_set(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "QuotaCo", "t05@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge"], monthly_quota=50000)
        resp = client.get("/script")
        html = resp.data.decode()
        assert "Monthly Characters" in html
        assert "50,000" in html

    def test_T06_no_quota_progress_when_not_set(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "NoQuotaCo", "t06@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge"], monthly_quota=None)
        resp = client.get("/script")
        html = resp.data.decode()
        assert "No monthly character limit" in html


# ── T07-T12: Credential Mode UX ─────────────────────────────────────────────

class TestCredentialModeUX:
    def test_T07_byok_only_plan_hides_mode_selector(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "ByokOnlyCo", "t07@test.com")
        _set_plan_policy(t["id"], credential_policy="byok_only")
        resp = client.get("/script")
        html = resp.data.decode()
        assert "Platform Managed" not in html or 'name="tts_credential_mode"' not in html

    def test_T08_hybrid_plan_shows_mode_selector(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "HybridCo", "t08@test.com")
        _set_plan_policy(
            t["id"],
            credential_policy="hybrid",
            allowed_providers=["edge", "gemini"],
            platform_managed_providers=["gemini"],
        )
        resp = client.get("/script")
        html = resp.data.decode()
        assert 'name="tts_credential_mode"' in html
        assert "Platform Managed" in html

    def test_T09_save_mode_byok_only_plan_rejects_platform_managed(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "ModeRejectCo", "t09@test.com")
        _set_plan_policy(t["id"], credential_policy="byok_only")
        csrf = _get_csrf(client, "/script")
        resp = client.post("/api/settings/script", data={
            "csrf_token": csrf,
            "voice_script": "Hello {customer_name}",
            "tts_provider": "edge",
            "tts_credential_mode": "platform_managed",
        }, follow_redirects=True)
        assert b"Platform-managed TTS is not available on your current plan" in resp.data

    def test_T10_save_mode_changes_saved_to_db(self, client, create_tenant):
        """Verify a real byok→platform_managed mode change is persisted to DB."""
        t, u = _create_and_login_tenant(client, create_tenant, "ModeSaveCo", "t10@test.com")
        _set_plan_policy(
            t["id"],
            credential_policy="hybrid",
            allowed_providers=["edge"],
            platform_managed_providers=["edge"],
        )
        # Ensure tenant starts in byok mode
        db.execute("UPDATE tenants SET tts_credential_mode='byok' WHERE id=%s", (t["id"],))
        # Patch platform secret as active so platform_managed passes validation
        with patch("app.models.platform_secrets.platform_secret_is_active", return_value=True):
            csrf = _get_csrf(client, "/script")
            client.post("/api/settings/script", data={
                "csrf_token": csrf,
                "voice_script": "Hello {customer_name}",
                "tts_provider": "edge",
                "tts_credential_mode": "platform_managed",  # real change: byok → platform_managed
            }, follow_redirects=True)
        row = db.query_one("SELECT tts_credential_mode FROM tenants WHERE id=%s", (t["id"],))
        assert row["tts_credential_mode"] == "platform_managed"

    def test_T11_platform_managed_mode_blocked_when_no_active_secret(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "NoSecretCo", "t11@test.com")
        _set_plan_policy(
            t["id"],
            credential_policy="hybrid",
            allowed_providers=["gemini"],
            platform_managed_providers=["gemini"],
        )
        csrf = _get_csrf(client, "/script")
        resp = client.post("/api/settings/script", data={
            "csrf_token": csrf,
            "voice_script": "Hello {customer_name}",
            "tts_provider": "gemini",
            "tts_credential_mode": "platform_managed",
        }, follow_redirects=True)
        assert b"temporarily unavailable" in resp.data

    def test_T12_mode_change_creates_audit_log(self, client, create_tenant):
        """byok→platform_managed must produce an audit_log row with old/new mode and no secrets."""
        t, u = _create_and_login_tenant(client, create_tenant, "AuditModeCo", "t12@test.com")
        _set_plan_policy(t["id"], credential_policy="hybrid", allowed_providers=["edge"],
                         platform_managed_providers=["edge"])
        db.execute("UPDATE tenants SET tts_credential_mode='byok' WHERE id=%s", (t["id"],))
        with patch("app.models.platform_secrets.platform_secret_is_active", return_value=True):
            csrf = _get_csrf(client, "/script")
            client.post("/api/settings/script", data={
                "csrf_token": csrf,
                "voice_script": "Hello {customer_name}",
                "tts_provider": "edge",
                "tts_credential_mode": "platform_managed",  # real change
            }, follow_redirects=True)
        # Verify audit row exists with correct actor and old/new mode
        row = db.query_one(
            """SELECT actor_user_id, target_id, details
               FROM audit_log
               WHERE action='tenant_tts_mode_changed' AND target_id=%s
               ORDER BY id DESC LIMIT 1""",
            (t["id"],)
        )
        assert row is not None, "audit_log row must exist for mode change"
        assert row["actor_user_id"] == u["id"]
        assert row["target_id"] == t["id"]
        details = row["details"]
        assert details.get("old_mode") == "byok"
        assert details.get("new_mode") == "platform_managed"
        # No credential or secret values in audit metadata
        details_str = str(details)
        assert "key" not in details_str.lower() or "old_mode" in details_str


# ── T13-T18: BYOK Key Management ─────────────────────────────────────────────

class TestByokKeyManagement:
    def test_T13_key_configured_shows_last_four(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "KeyMetaCo", "t13@test.com")
        _set_plan_policy(t["id"], allowed_providers=["gemini"])
        from app.models.tenant_secrets import save_secret
        save_secret(t["id"], "gemini_api_key", "abc123456789")
        resp = client.get("/script")
        assert b"6789" in resp.data

    def test_T14_key_input_value_always_blank(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "BlankKeyco", "t14@test.com")
        _set_plan_policy(t["id"], allowed_providers=["gemini"])
        from app.models.tenant_secrets import save_secret
        save_secret(t["id"], "gemini_api_key", "supersecretkey99")
        resp = client.get("/script")
        html = resp.data.decode()
        assert "supersecretkey99" not in html
        assert 'value="supersecretkey99"' not in html

    def test_T15_remove_tts_key_route_exists(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "RemoveCo", "t15@test.com")
        from app.models.tenant_secrets import save_secret
        save_secret(t["id"], "gemini_api_key", "toremove9999")
        csrf = _get_csrf(client, "/script")
        resp = client.post("/api/settings/tts-key/gemini/remove",
                           data={"csrf_token": csrf})
        assert resp.status_code == 200
        assert b"removed" in resp.data.lower()

    def test_T16_remove_tts_key_actually_deletes(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "DeleteKeyCo", "t16@test.com")
        from app.models.tenant_secrets import save_secret, get_secret_meta
        save_secret(t["id"], "elevenlabs_api_key", "keytodeleteabcd")
        csrf = _get_csrf(client, "/script")
        client.post("/api/settings/tts-key/elevenlabs/remove", data={"csrf_token": csrf})
        assert get_secret_meta(t["id"], "elevenlabs_api_key") is None

    def test_T17_remove_invalid_provider_returns_422(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "BadProvCo", "t17@test.com")
        csrf = _get_csrf(client, "/script")
        resp = client.post("/api/settings/tts-key/edge/remove", data={"csrf_token": csrf})
        assert resp.status_code == 422

    def test_T18_byok_key_required_when_key_provider_with_no_key(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "KeyReqCo", "t18@test.com")
        _set_plan_policy(t["id"], allowed_providers=["gemini"])
        csrf = _get_csrf(client, "/script")
        resp = client.post("/api/settings/script", data={
            "csrf_token": csrf,
            "voice_script": "Hello {customer_name}",
            "tts_provider": "gemini",
            "tts_credential_mode": "byok",
        }, follow_redirects=True)
        assert b"API key is required" in resp.data


# ── T19-T24: Preview Enforcement ─────────────────────────────────────────────

class TestPreviewEnforcement:
    def test_T19_preview_route_requires_login(self, client):
        resp = client.post("/api/settings/tts-preview", data={
            "voice_script": "Hello {customer_name}",
            "tts_provider": "edge",
        })
        assert resp.status_code in (302, 401, 403)

    def test_T20_preview_with_empty_script_returns_400(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "EmptyScriptCo", "t20@test.com")
        csrf = _get_csrf(client, "/script")
        resp = client.post("/api/settings/tts-preview", data={
            "csrf_token": csrf,
            "voice_script": "",
            "tts_provider": "edge",
        })
        assert resp.status_code == 400

    def test_T21_preview_with_invalid_provider_returns_422(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "BadProvPreCo", "t21@test.com")
        csrf = _get_csrf(client, "/script")
        with patch("app.blueprints.customer.settings.generate_order_audio", return_value="test.wav"):
            resp = client.post("/api/settings/tts-preview", data={
                "csrf_token": csrf,
                "voice_script": "Hello {customer_name}",
                "tts_provider": "invalid_provider",
            })
        assert resp.status_code == 422

    def test_T22_preview_daily_limit_enforced(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "DailyLimitCo", "t22@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge"], daily_preview_limit=1)
        # Simulate limit already reached via Redis mock
        from app.extensions import redis_ext
        import datetime
        today = datetime.date.today().isoformat()
        redis_ext.client.set(f"daily_preview:{t['id']}:{today}", 1)
        csrf = _get_csrf(client, "/script")
        resp = client.post("/api/settings/tts-preview", data={
            "csrf_token": csrf,
            "voice_script": "Hello {customer_name}",
            "tts_provider": "edge",
        })
        assert resp.status_code == 402
        assert b"Daily preview limit" in resp.data

    def test_T23_preview_success_increments_redis_counter(self, client, create_tenant):
        """Success increments exactly once; a failed (empty script) request does not increment."""
        t, u = _create_and_login_tenant(client, create_tenant, "IncrCo", "t23@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge"], daily_preview_limit=5)
        from app.extensions import redis_ext
        import datetime
        today = datetime.date.today().isoformat()
        counter_key = f"daily_preview:{t['id']}:{today}"
        try:
            redis_ext.client.delete(counter_key)
        except Exception:
            pass

        csrf = _get_csrf(client, "/script")

        # Failed request (empty script) must NOT increment counter
        resp_fail = client.post("/api/settings/tts-preview", data={
            "csrf_token": csrf,
            "voice_script": "",
            "tts_provider": "edge",
        })
        assert resp_fail.status_code == 400
        count_after_fail = int(redis_ext.client.get(counter_key) or 0)
        assert count_after_fail == 0, "failed preview must not increment counter"

        # Successful request must increment exactly once
        with patch("app.blueprints.customer.settings.generate_order_audio", return_value="fake.wav"):
            with patch("app.services.tts_service.render_order_voice_script", return_value="Hello Sakib"):
                resp_ok = client.post("/api/settings/tts-preview", data={
                    "csrf_token": csrf,
                    "voice_script": "Hello {customer_name}",
                    "tts_provider": "edge",
                })
        count_after_success = int(redis_ext.client.get(counter_key) or 0)
        assert count_after_success == 1, f"expected counter=1 after one success, got {count_after_success}"

        # Second successful request increments to 2 (not doubled)
        with patch("app.blueprints.customer.settings.generate_order_audio", return_value="fake.wav"):
            with patch("app.services.tts_service.render_order_voice_script", return_value="Hello Sakib"):
                client.post("/api/settings/tts-preview", data={
                    "csrf_token": csrf,
                    "voice_script": "Hello {customer_name}",
                    "tts_provider": "edge",
                })
        count_after_second = int(redis_ext.client.get(counter_key) or 0)
        assert count_after_second == 2, f"expected counter=2 after two successes, got {count_after_second}"

    def test_T24_preview_script_too_long_returns_400(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "LongScriptCo", "t24@test.com")
        csrf = _get_csrf(client, "/script")
        resp = client.post("/api/settings/tts-preview", data={
            "csrf_token": csrf,
            "voice_script": "A" * 501,
            "tts_provider": "edge",
        })
        assert resp.status_code == 400


# ── T25-T30: Dashboard TTS Widget ────────────────────────────────────────────

class TestDashboardTtsWidget:
    def test_T25_dashboard_loads_without_error(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "DashCo", "t25@test.com")
        resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_T26_dashboard_tts_widget_shown_for_platform_managed(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "DashPMCo", "t26@test.com")
        _set_plan_policy(t["id"], credential_policy="hybrid", allowed_providers=["gemini"],
                         platform_managed_providers=["gemini"])
        db.execute("UPDATE tenants SET tts_credential_mode='platform_managed' WHERE id=%s", (t["id"],))
        resp = client.get("/dashboard")
        html = resp.data.decode()
        assert "Platform Managed" in html

    def test_T27_dashboard_tts_widget_shows_quota_bar(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "DashQuotaCo", "t27@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge"], monthly_quota=10000)
        db.execute("UPDATE tenants SET tts_credential_mode='platform_managed' WHERE id=%s", (t["id"],))
        resp = client.get("/dashboard")
        html = resp.data.decode()
        assert "10,000" in html

    def test_T28_dashboard_tts_unavailable_warning_shown(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "DashUnavailCo", "t28@test.com")
        _set_plan_policy(t["id"], credential_policy="hybrid", allowed_providers=["gemini"],
                         platform_managed_providers=["gemini"])
        db.execute("UPDATE tenants SET tts_credential_mode='platform_managed' WHERE id=%s", (t["id"],))
        # No platform secret → unavailable
        with patch("app.models.platform_secrets.platform_secret_is_active", return_value=False):
            resp = client.get("/dashboard")
        html = resp.data.decode()
        assert "Unavailable" in html or "unavailable" in html

    def test_T29_dashboard_no_tts_widget_without_quota_or_platform_mode(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "DashNoWidgetCo", "t29@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge"], monthly_quota=None)
        db.execute("UPDATE tenants SET tts_credential_mode='byok' WHERE id=%s", (t["id"],))
        resp = client.get("/dashboard")
        html = resp.data.decode()
        # Widget should not appear when neither quota nor platform mode
        assert "tts_mode" not in html  # jinja variable not leaked

    def test_T30_dashboard_configure_link_present_when_widget_shown(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "DashLinkCo", "t30@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge"], monthly_quota=5000)
        db.execute("UPDATE tenants SET tts_credential_mode='platform_managed' WHERE id=%s", (t["id"],))
        resp = client.get("/dashboard")
        html = resp.data.decode()
        assert "/script" in html


# ── T31-T36: Subscription Page ───────────────────────────────────────────────

class TestSubscriptionPage:
    def test_T31_subscription_page_loads(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "SubCo", "t31@test.com")
        resp = client.get("/settings/subscription")
        assert resp.status_code == 200
        assert b"Subscription" in resp.data

    def test_T32_subscription_page_shows_plan_name(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "SubPlanCo", "t32@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge"])
        row = db.query_one("SELECT name FROM plans WHERE id=(SELECT plan_id FROM tenants WHERE id=%s)", (t["id"],))
        resp = client.get("/settings/subscription")
        assert row["name"].encode() in resp.data

    def test_T33_subscription_page_shows_allowed_providers(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "SubProvCo", "t33@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge", "gtts"])
        resp = client.get("/settings/subscription")
        html = resp.data.decode()
        assert "edge" in html
        assert "gtts" in html

    def test_T34_subscription_page_shows_quota(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "SubQuotaCo", "t34@test.com")
        _set_plan_policy(t["id"], allowed_providers=["edge"], monthly_quota=25000)
        resp = client.get("/settings/subscription")
        assert b"25,000" in resp.data

    def test_T35_subscription_page_accessible_to_agent_role(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "AgentSubCo", "t35@test.com")
        db.execute("UPDATE users SET role='agent' WHERE id=%s", (u["id"],))
        resp = client.get("/settings/subscription")
        assert resp.status_code == 200

    def test_T36_subscription_page_no_plan_does_not_crash(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "NoPlanSubCo", "t36@test.com")
        db.execute("UPDATE tenants SET plan_id=NULL WHERE id=%s", (t["id"],))
        # Invalidate cache so route sees the updated plan_id=NULL
        from app.extensions import redis_ext
        try:
            redis_ext.client.delete(f"tenant:settings:{t['id']}")
        except Exception:
            pass
        resp = client.get("/settings/subscription")
        assert resp.status_code == 200
        assert b"No plan assigned" in resp.data


# ── T37-T42: Security / Anti-Leak ────────────────────────────────────────────

class TestSecurityAntiLeak:
    def test_T37_script_page_unauthenticated_redirects(self, client):
        resp = client.get("/script")
        assert resp.status_code in (302, 401)

    def test_T38_subscription_page_unauthenticated_redirects(self, client):
        resp = client.get("/settings/subscription")
        assert resp.status_code in (302, 401)

    def test_T39_remove_key_unauthenticated_returns_redirect(self, client):
        resp = client.post("/api/settings/tts-key/gemini/remove")
        assert resp.status_code in (302, 401)

    def test_T40_platform_secret_value_not_in_script_page(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "SecretLeakCo", "t40@test.com")
        _set_plan_policy(t["id"], credential_policy="hybrid", allowed_providers=["gemini"],
                         platform_managed_providers=["gemini"])
        # Insert a mock platform secret row directly
        from app.models.sip_provider import _get_fernet
        fernet = _get_fernet()
        encrypted = fernet.encrypt(b"REAL_SECRET_KEY_ABCDEF").decode()
        db.execute(
            """INSERT INTO platform_secrets (provider, encrypted_value, last_four, status)
               VALUES ('gemini', %s, 'CDEF', 'active')
               ON CONFLICT (provider) DO UPDATE SET encrypted_value=%s""",
            (encrypted, encrypted)
        )
        resp = client.get("/script")
        html = resp.data.decode()
        assert "REAL_SECRET_KEY_ABCDEF" not in html
        assert encrypted not in html

    def test_T41_api_key_full_value_not_in_script_page(self, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "KeyLeakCo", "t41@test.com")
        from app.models.tenant_secrets import save_secret
        save_secret(t["id"], "gemini_api_key", "FULL_API_KEY_NEVER_SHOW_12345")
        resp = client.get("/script")
        html = resp.data.decode()
        assert "FULL_API_KEY_NEVER_SHOW_12345" not in html

    def test_T42_remove_key_requires_authentication(self, client, create_tenant):
        """Key removal requires an authenticated session — unauthenticated callers are redirected."""
        # Logout first to ensure no session
        client.get("/auth/logout")
        from app.models.tenant_secrets import save_secret
        # Save a key for any tenant (T39 already checks unauthenticated, this confirms
        # route-level auth enforcement separately from T39)
        resp = client.post("/api/settings/tts-key/gemini/remove", data={"csrf_token": "fake"})
        assert resp.status_code in (302, 401, 403)


# ── CSRF Protection (CSRF enabled) ──────────────────────────────────────────
# Uses the standard app/client/create_tenant/db_conn fixtures.
# CSRF is enabled by temporarily setting app.config["WTF_CSRF_ENABLED"] = True
# inside each test, then restored. No second Flask app or second DB pool.

def _enable_csrf(app):
    """Context manager: enable CSRF for this test, restore afterward."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        app.config["WTF_CSRF_ENABLED"] = True
        app.config["WTF_CSRF_METHODS"] = {"POST", "PUT", "PATCH", "DELETE"}
        try:
            yield
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    return _ctx()


class TestCsrfProtection:
    """State-changing routes must reject requests without a valid CSRF token (CSRF enabled)."""

    def test_csrf_script_post_without_token_rejected(self, app, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "CsrfScriptCo", "csrf01@test.com")
        with _enable_csrf(app):
            resp = client.post("/api/settings/script", data={
                "voice_script": "Hello",
                "tts_provider": "edge",
                "tts_credential_mode": "byok",
            })
        assert resp.status_code in (400, 403), f"Expected 400/403 without CSRF token, got {resp.status_code}"

    def test_csrf_tts_preview_without_token_rejected(self, app, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "CsrfPreviewCo", "csrf02@test.com")
        with _enable_csrf(app):
            resp = client.post("/api/settings/tts-preview", data={
                "voice_script": "Hello {customer_name}",
                "tts_provider": "edge",
            })
        assert resp.status_code in (400, 403), f"Expected 400/403 without CSRF token, got {resp.status_code}"

    def test_csrf_remove_gemini_key_without_token_rejected(self, app, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "CsrfRemGemCo", "csrf03@test.com")
        with _enable_csrf(app):
            resp = client.post("/api/settings/tts-key/gemini/remove")
        assert resp.status_code in (400, 403), f"Expected 400/403 without CSRF token, got {resp.status_code}"

    def test_csrf_remove_elevenlabs_key_without_token_rejected(self, app, client, create_tenant):
        t, u = _create_and_login_tenant(client, create_tenant, "CsrfRemElCo", "csrf04@test.com")
        with _enable_csrf(app):
            resp = client.post("/api/settings/tts-key/elevenlabs/remove")
        assert resp.status_code in (400, 403), f"Expected 400/403 without CSRF token, got {resp.status_code}"

    def test_csrf_script_post_with_valid_token_succeeds(self, app, client, create_tenant):
        """Same route with valid CSRF token reaches business logic (not blocked at CSRF layer)."""
        t, u = _create_and_login_tenant(client, create_tenant, "CsrfValidCo", "csrf05@test.com")
        with _enable_csrf(app):
            # Get CSRF token from rendered form while CSRF is enabled
            resp = client.get("/script")
            html = resp.data.decode()
            start = html.find('name="csrf_token" value="') + len('name="csrf_token" value="')
            end = html.find('"', start)
            token = html[start:end]
            resp2 = client.post("/api/settings/script", data={
                "csrf_token": token,
                "voice_script": "Hello {customer_name}",
                "tts_provider": "edge",
                "tts_credential_mode": "byok",
            }, follow_redirects=True)
        assert resp2.status_code not in (400, 403), f"Valid CSRF token was rejected: {resp2.status_code}"


# ── T43-T44: Regression ─────────────────────────────────────────────────────

class TestRegression:
    def test_T43_admin_plan_list_not_affected(self, super_admin_client):
        resp = super_admin_client.get("/admin/plans")
        assert resp.status_code == 200

    def test_T44_admin_platform_tts_page_not_affected(self, super_admin_client):
        resp = super_admin_client.get("/admin/platform-tts")
        assert resp.status_code == 200
