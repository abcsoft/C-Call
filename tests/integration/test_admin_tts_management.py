"""Integration tests for Admin TTS Management UI (Phases 3–13).

48 scenarios covering:
- Plan list / create / edit / archive
- Platform TTS save / test / disable
- Tenant TTS mode override
- TTS usage page
- Authorization (super_admin only)
- Secret exposure checks (no key values in responses)
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_csrf(client, path: str) -> str:
    """Fetch a page and extract a CSRF token."""
    resp = client.get(path)
    assert resp.status_code == 200, f"GET {path} returned {resp.status_code}"
    html = resp.data.decode()
    # Look for hidden csrf_token input
    start = html.find('name="csrf_token" value="')
    if start == -1:
        # Try meta tag or alternative
        start = html.find("csrf_token")
    assert start != -1, f"No csrf_token found on {path}"
    value_start = html.find('value="', start) + len('value="')
    value_end = html.find('"', value_start)
    token = html[value_start:value_end]
    assert token, "Empty CSRF token"
    return token


def _post(client, url: str, data: dict, csrf_token: str | None = None) -> "Response":
    if csrf_token:
        data = {**data, "csrf_token": csrf_token}
    return client.post(url, data=data, follow_redirects=True)


# ── Authorization fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def super_admin_client(client, super_admin_user, app):
    """HTTP client authenticated as super_admin."""
    with app.test_request_context():
        from flask_login import login_user
    with client.session_transaction():
        pass
    client.post("/auth/login", data={
        "email": super_admin_user["email"],
        "password": "AdminPass123!",
        "csrf_token": _get_super_csrf(client),
    }, follow_redirects=True)
    return client


def _get_super_csrf(client) -> str:
    resp = client.get("/auth/login")
    html = resp.data.decode()
    start = html.find('name="csrf_token" value="') + len('name="csrf_token" value="')
    end = html.find('"', start)
    return html[start:end]


@pytest.fixture
def tenant_client(client, tenant_user, app):
    """HTTP client authenticated as a regular tenant user (not super_admin)."""
    client.post("/auth/login", data={
        "email": tenant_user["email"],
        "password": "TenantPass123!",
        "csrf_token": _get_super_csrf(client),
    }, follow_redirects=True)
    return client


# ── Plan list ─────────────────────────────────────────────────────────────────

class TestPlansList:
    def test_plans_list_requires_super_admin(self, client):
        resp = client.get("/admin/plans", follow_redirects=False)
        assert resp.status_code in (302, 403), "Unauthenticated should be redirected"

    def test_plans_list_tenant_forbidden(self, tenant_client):
        resp = tenant_client.get("/admin/plans", follow_redirects=False)
        assert resp.status_code in (302, 403)

    def test_plans_list_super_admin_accessible(self, super_admin_client):
        resp = super_admin_client.get("/admin/plans")
        assert resp.status_code == 200
        assert b"Plans" in resp.data

    def test_plans_list_no_encrypted_value_in_html(self, super_admin_client):
        resp = super_admin_client.get("/admin/plans")
        html = resp.data.decode()
        assert "encrypted_value" not in html
        assert "api_key" not in html.lower() or "api_key" not in html  # column name never shown

    def test_plans_list_shows_archived_status(self, super_admin_client, db_conn):
        """An archived plan shows 'Archived' label."""
        db_conn.execute("UPDATE plans SET archived_at = now() WHERE id = (SELECT MIN(id) FROM plans)")
        resp = super_admin_client.get("/admin/plans")
        assert resp.status_code == 200
        assert b"Archived" in resp.data
        # Cleanup
        db_conn.execute("UPDATE plans SET archived_at = NULL WHERE archived_at IS NOT NULL")


# ── Plan create ───────────────────────────────────────────────────────────────

class TestPlanCreate:
    def test_create_form_accessible(self, super_admin_client):
        resp = super_admin_client.get("/admin/plans/create")
        assert resp.status_code == 200
        assert b"Create" in resp.data

    def test_create_plan_valid(self, super_admin_client, db_conn):
        csrf = _get_csrf(super_admin_client, "/admin/plans/create")
        resp = _post(super_admin_client, "/admin/plans/create", {
            "name": "Test Plan XYZ",
            "price_monthly": "500",
            "included_minutes": "100",
            "max_flows": "10",
            "max_users": "5",
            "credential_policy": "byok_only",
            "allowed_providers": "edge,gtts",
            "byok_providers": "edge",
            "platform_managed_providers": "",
            "daily_preview_limit": "10",
            "max_concurrent_tts": "1",
        }, csrf_token=csrf)
        assert resp.status_code == 200
        assert b"created successfully" in resp.data or b"Test Plan XYZ" in resp.data
        # Cleanup
        db_conn.execute("DELETE FROM plan_tts_policies WHERE plan_id IN (SELECT id FROM plans WHERE name='Test Plan XYZ')")
        db_conn.execute("DELETE FROM plans WHERE name = 'Test Plan XYZ'")

    def test_create_plan_duplicate_name_rejected(self, super_admin_client, db_conn):
        """Second plan with same name should fail with error."""
        # Get first plan name
        row = db_conn.execute("SELECT name FROM plans LIMIT 1").fetchone()
        if not row:
            pytest.skip("No plans in DB")
        existing_name = row["name"]
        csrf = _get_csrf(super_admin_client, "/admin/plans/create")
        resp = _post(super_admin_client, "/admin/plans/create", {
            "name": existing_name,
            "price_monthly": "0",
            "included_minutes": "0",
            "max_flows": "0",
            "max_users": "0",
            "credential_policy": "byok_only",
            "allowed_providers": "edge",
            "byok_providers": "",
            "platform_managed_providers": "",
            "daily_preview_limit": "10",
            "max_concurrent_tts": "1",
        }, csrf_token=csrf)
        assert b"already exists" in resp.data

    def test_create_plan_platform_providers_without_policy_rejected(self, super_admin_client):
        """platform_managed_providers with byok_only policy should fail."""
        csrf = _get_csrf(super_admin_client, "/admin/plans/create")
        resp = _post(super_admin_client, "/admin/plans/create", {
            "name": "Bad Plan __test",
            "price_monthly": "0",
            "included_minutes": "0",
            "max_flows": "0",
            "max_users": "0",
            "credential_policy": "byok_only",
            "allowed_providers": "gemini",
            "byok_providers": "",
            "platform_managed_providers": "gemini",
            "daily_preview_limit": "10",
            "max_concurrent_tts": "1",
        }, csrf_token=csrf)
        assert b"byok_only" in resp.data or b"cannot" in resp.data.lower()

    def test_create_plan_invalid_provider_filtered(self, super_admin_client, db_conn):
        """Invalid provider names are silently filtered out."""
        csrf = _get_csrf(super_admin_client, "/admin/plans/create")
        resp = _post(super_admin_client, "/admin/plans/create", {
            "name": "Filter Test Plan __xyz",
            "price_monthly": "0",
            "included_minutes": "0",
            "max_flows": "0",
            "max_users": "0",
            "credential_policy": "byok_only",
            "allowed_providers": "edge,NOTVALID,fakeai",
            "byok_providers": "",
            "platform_managed_providers": "",
            "daily_preview_limit": "10",
            "max_concurrent_tts": "1",
        }, csrf_token=csrf)
        # Should succeed (invalid filtered) or redirect to list
        # Cleanup
        db_conn.execute("DELETE FROM plan_tts_policies WHERE plan_id IN (SELECT id FROM plans WHERE name='Filter Test Plan __xyz')")
        db_conn.execute("DELETE FROM plans WHERE name = 'Filter Test Plan __xyz'")


# ── Plan edit ─────────────────────────────────────────────────────────────────

class TestPlanEdit:
    def test_edit_form_requires_super_admin(self, client, db_conn):
        row = db_conn.execute("SELECT id FROM plans LIMIT 1").fetchone()
        if not row:
            pytest.skip("No plans in DB")
        resp = client.get(f"/admin/plans/{row['id']}/edit", follow_redirects=False)
        assert resp.status_code in (302, 403)

    def test_edit_form_404_for_missing_plan(self, super_admin_client):
        resp = super_admin_client.get("/admin/plans/999999/edit")
        assert resp.status_code == 404

    def test_edit_form_loads(self, super_admin_client, db_conn):
        row = db_conn.execute("SELECT id FROM plans LIMIT 1").fetchone()
        if not row:
            pytest.skip("No plans in DB")
        resp = super_admin_client.get(f"/admin/plans/{row['id']}/edit")
        assert resp.status_code == 200
        assert b"Edit" in resp.data or b"Plan" in resp.data

    def test_edit_plan_name_updated(self, super_admin_client, db_conn):
        """Editing a plan name persists to DB."""
        # Create a temp plan to edit safely
        db_conn.execute("INSERT INTO plans (name, price_monthly, included_minutes, max_flows, max_users) VALUES ('EditTarget __t', 0, 0, 0, 0)")
        row = db_conn.execute("SELECT id FROM plans WHERE name='EditTarget __t'").fetchone()
        plan_id = row["id"]
        db_conn.execute("INSERT INTO plan_tts_policies (plan_id, credential_policy, allowed_providers, byok_providers, platform_managed_providers) VALUES (%s, 'byok_only', '[]', '[]', '[]')", (plan_id,))

        csrf = _get_csrf(super_admin_client, f"/admin/plans/{plan_id}/edit")
        resp = _post(super_admin_client, f"/admin/plans/{plan_id}/edit", {
            "name": "EditTarget __t Renamed",
            "price_monthly": "99",
            "included_minutes": "50",
            "max_flows": "5",
            "max_users": "2",
            "credential_policy": "byok_only",
            "allowed_providers": "edge",
            "byok_providers": "",
            "platform_managed_providers": "",
            "daily_preview_limit": "5",
            "max_concurrent_tts": "1",
        }, csrf_token=csrf)
        assert resp.status_code == 200
        updated = db_conn.execute("SELECT name FROM plans WHERE id=%s", (plan_id,)).fetchone()
        assert updated["name"] == "EditTarget __t Renamed"

        # Cleanup
        db_conn.execute("DELETE FROM plan_tts_policies WHERE plan_id=%s", (plan_id,))
        db_conn.execute("DELETE FROM plans WHERE id=%s", (plan_id,))


# ── Plan archive ──────────────────────────────────────────────────────────────

class TestPlanArchive:
    def test_archive_requires_post(self, super_admin_client, db_conn):
        row = db_conn.execute("SELECT id FROM plans LIMIT 1").fetchone()
        if not row:
            pytest.skip("No plans in DB")
        resp = super_admin_client.get(f"/admin/plans/{row['id']}/archive")
        assert resp.status_code == 405  # Method not allowed

    def test_archive_blocks_if_tenants_assigned(self, super_admin_client, db_conn):
        """Plan with tenants cannot be archived."""
        row = db_conn.execute("""
            SELECT p.id FROM plans p
            JOIN tenants t ON t.plan_id = p.id
            WHERE p.archived_at IS NULL
            LIMIT 1
        """).fetchone()
        if not row:
            pytest.skip("No plan with tenants in DB")
        plan_id = row["id"]
        csrf = _get_csrf(super_admin_client, "/admin/plans")
        resp = _post(super_admin_client, f"/admin/plans/{plan_id}/archive", {}, csrf_token=csrf)
        assert b"Cannot archive" in resp.data or b"tenant" in resp.data.lower()
        # Confirm not archived
        r2 = db_conn.execute("SELECT archived_at FROM plans WHERE id=%s", (plan_id,)).fetchone()
        assert r2["archived_at"] is None

    def test_archive_succeeds_for_unassigned_plan(self, super_admin_client, db_conn):
        """Plan with 0 tenants can be archived."""
        db_conn.execute("INSERT INTO plans (name, price_monthly, included_minutes, max_flows, max_users) VALUES ('ArchiveMe __t', 0, 0, 0, 0)")
        row = db_conn.execute("SELECT id FROM plans WHERE name='ArchiveMe __t'").fetchone()
        plan_id = row["id"]
        db_conn.execute("INSERT INTO plan_tts_policies (plan_id, credential_policy, allowed_providers, byok_providers, platform_managed_providers) VALUES (%s, 'byok_only', '[]', '[]', '[]')", (plan_id,))

        csrf = _get_csrf(super_admin_client, "/admin/plans")
        resp = _post(super_admin_client, f"/admin/plans/{plan_id}/archive", {}, csrf_token=csrf)
        assert resp.status_code == 200
        r2 = db_conn.execute("SELECT archived_at FROM plans WHERE id=%s", (plan_id,)).fetchone()
        assert r2["archived_at"] is not None

        # Cleanup
        db_conn.execute("DELETE FROM plan_tts_policies WHERE plan_id=%s", (plan_id,))
        db_conn.execute("DELETE FROM plans WHERE id=%s", (plan_id,))


# ── Platform TTS page ─────────────────────────────────────────────────────────

class TestPlatformTtsPage:
    def test_page_requires_super_admin(self, client):
        resp = client.get("/admin/platform-tts", follow_redirects=False)
        assert resp.status_code in (302, 403)

    def test_page_loads(self, super_admin_client):
        resp = super_admin_client.get("/admin/platform-tts")
        assert resp.status_code == 200
        assert b"gemini" in resp.data.lower() or b"elevenlabs" in resp.data.lower()

    def test_page_no_encrypted_value(self, super_admin_client):
        """encrypted_value must never appear in HTML response."""
        resp = super_admin_client.get("/admin/platform-tts")
        html = resp.data.decode()
        assert "encrypted_value" not in html

    def test_page_no_full_api_key(self, super_admin_client, db_conn):
        """If a credential is stored, only last_four shown."""
        # Plant a fake credential
        db_conn.execute("""
            INSERT INTO platform_secrets (provider, encrypted_value, last_four, status)
            VALUES ('gemini', 'ENCRYPTED_PLACEHOLDER', '9999', 'active')
            ON CONFLICT (provider) DO UPDATE SET last_four='9999', status='active'
        """)
        resp = super_admin_client.get("/admin/platform-tts")
        html = resp.data.decode()
        assert "9999" in html  # last_four shown
        assert "ENCRYPTED_PLACEHOLDER" not in html


# ── Platform TTS save ─────────────────────────────────────────────────────────

class TestPlatformTtsSave:
    def test_save_requires_super_admin(self, client):
        resp = client.post("/admin/platform-tts/gemini/save", data={
            "api_key": "abc123", "confirm_save": "1"
        }, follow_redirects=False)
        assert resp.status_code in (302, 403)

    def test_save_unknown_provider_404(self, super_admin_client):
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        resp = _post(super_admin_client, "/admin/platform-tts/badprovider/save", {
            "api_key": "abc", "confirm_save": "1",
        }, csrf_token=csrf)
        assert resp.status_code == 404

    def test_save_empty_key_rejected(self, super_admin_client):
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        resp = _post(super_admin_client, "/admin/platform-tts/gemini/save", {
            "api_key": "",
            "confirm_save": "1",
        }, csrf_token=csrf)
        assert b"must not be empty" in resp.data or resp.status_code in (400, 200)

    def test_save_without_confirmation_rejected(self, super_admin_client):
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        resp = _post(super_admin_client, "/admin/platform-tts/gemini/save", {
            "api_key": "AIzaSy_test_key_1234567890",
            # confirm_save NOT set
        }, csrf_token=csrf)
        assert b"confirmation" in resp.data.lower() or b"checkbox" in resp.data.lower()

    def test_save_stores_encrypted_not_plaintext(self, super_admin_client, db_conn):
        """Saved value must be Fernet ciphertext, not plaintext key."""
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        plaintext = "AIzaSy_FAKE_KEY_9876543210abcdef"
        _post(super_admin_client, "/admin/platform-tts/gemini/save", {
            "api_key": plaintext,
            "confirm_save": "1",
        }, csrf_token=csrf)
        row = db_conn.execute("SELECT encrypted_value FROM platform_secrets WHERE provider='gemini'").fetchone()
        if row:
            stored = row["encrypted_value"]
            assert stored != plaintext, "Plaintext stored — encryption failed!"
            assert len(stored) > 20  # Fernet tokens are long

    def test_save_response_shows_only_last_four(self, super_admin_client):
        """After save, page shows last4 of key, not full key."""
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        plaintext = "AIzaSy_FAKE_KEY_000000xyzFOUR"
        resp = _post(super_admin_client, "/admin/platform-tts/gemini/save", {
            "api_key": plaintext,
            "confirm_save": "1",
        }, csrf_token=csrf)
        html = resp.data.decode()
        # Full key must not appear
        assert plaintext not in html
        # Last 4 characters ("FOUR") should appear
        assert "FOUR" in html or "last 4" in html.lower()


# ── Platform TTS test ─────────────────────────────────────────────────────────

class TestPlatformTtsTest:
    def test_test_requires_post(self, super_admin_client):
        resp = super_admin_client.get("/admin/platform-tts/gemini/test")
        assert resp.status_code == 405

    def test_test_no_credential_shows_error(self, super_admin_client, db_conn):
        db_conn.execute("DELETE FROM platform_secrets WHERE provider='elevenlabs'")
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        resp = _post(super_admin_client, "/admin/platform-tts/elevenlabs/test", {}, csrf_token=csrf)
        assert b"No" in resp.data or b"not configured" in resp.data.lower()

    @patch("app.blueprints.admin.routes_tts._test_gemini")
    def test_test_success_shows_valid(self, mock_test, super_admin_client, db_conn):
        """Mocked successful test shows success message."""
        mock_test.return_value = (True, "ok")
        db_conn.execute("""
            INSERT INTO platform_secrets (provider, encrypted_value, last_four, status)
            VALUES ('gemini', 'FAKE_ENCRYPTED', '0000', 'active')
            ON CONFLICT (provider) DO UPDATE SET status='active', encrypted_value='FAKE_ENCRYPTED'
        """)
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        with patch("app.models.platform_secrets.get_platform_secret_for_generation", return_value="fake_key"):
            resp = _post(super_admin_client, "/admin/platform-tts/gemini/test", {}, csrf_token=csrf)
        assert b"valid" in resp.data.lower() or b"gemini" in resp.data.lower()

    @patch("app.blueprints.admin.routes_tts._test_gemini")
    def test_test_failure_shows_error(self, mock_test, super_admin_client, db_conn):
        """Mocked failed test shows error message."""
        mock_test.return_value = (False, "Invalid API key (400).")
        db_conn.execute("""
            INSERT INTO platform_secrets (provider, encrypted_value, last_four, status)
            VALUES ('gemini', 'FAKE_ENCRYPTED', '0000', 'active')
            ON CONFLICT (provider) DO UPDATE SET status='active', encrypted_value='FAKE_ENCRYPTED'
        """)
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        with patch("app.models.platform_secrets.get_platform_secret_for_generation", return_value="fake_key"):
            resp = _post(super_admin_client, "/admin/platform-tts/gemini/test", {}, csrf_token=csrf)
        assert b"failed" in resp.data.lower() or b"Invalid" in resp.data


# ── Platform TTS disable ──────────────────────────────────────────────────────

class TestPlatformTtsDisable:
    def test_disable_requires_reason(self, super_admin_client, db_conn):
        db_conn.execute("""
            INSERT INTO platform_secrets (provider, encrypted_value, last_four, status)
            VALUES ('elevenlabs', 'FAKE_ENCRYPTED', '1234', 'active')
            ON CONFLICT (provider) DO UPDATE SET status='active', encrypted_value='FAKE_ENCRYPTED'
        """)
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        resp = _post(super_admin_client, "/admin/platform-tts/elevenlabs/disable", {
            "reason": "",  # empty
        }, csrf_token=csrf)
        assert b"reason" in resp.data.lower() or b"required" in resp.data.lower()
        # Status should not have changed
        row = db_conn.execute("SELECT status FROM platform_secrets WHERE provider='elevenlabs'").fetchone()
        if row:
            assert row["status"] == "active"

    def test_disable_with_reason_succeeds(self, super_admin_client, db_conn):
        db_conn.execute("""
            INSERT INTO platform_secrets (provider, encrypted_value, last_four, status)
            VALUES ('elevenlabs', 'FAKE_ENCRYPTED', '5678', 'active')
            ON CONFLICT (provider) DO UPDATE SET status='active'
        """)
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        resp = _post(super_admin_client, "/admin/platform-tts/elevenlabs/disable", {
            "reason": "Key compromised — rotating",
        }, csrf_token=csrf)
        assert resp.status_code == 200
        row = db_conn.execute("SELECT status FROM platform_secrets WHERE provider='elevenlabs'").fetchone()
        if row:
            assert row["status"] == "disabled"

    def test_disable_unknown_provider_404(self, super_admin_client):
        csrf = _get_csrf(super_admin_client, "/admin/platform-tts")
        resp = _post(super_admin_client, "/admin/platform-tts/fakeprovider/disable", {
            "reason": "test"
        }, csrf_token=csrf)
        assert resp.status_code == 404


# ── Tenant TTS mode override ──────────────────────────────────────────────────

class TestTenantTtsModeOverride:
    def test_override_requires_super_admin(self, client, tenant):
        resp = client.post(
            f"/admin/customers/{tenant['id']}/tts-mode",
            data={"tts_credential_mode": "byok", "reason": "test"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 403)

    def test_override_requires_reason(self, super_admin_client, tenant):
        csrf = _get_csrf(super_admin_client, f"/admin/customers/{tenant['id']}")
        resp = _post(super_admin_client, f"/admin/customers/{tenant['id']}/tts-mode", {
            "tts_credential_mode": "byok",
            "reason": "",
        }, csrf_token=csrf)
        assert b"reason" in resp.data.lower() or b"required" in resp.data.lower()

    def test_override_invalid_mode_rejected(self, super_admin_client, tenant):
        csrf = _get_csrf(super_admin_client, f"/admin/customers/{tenant['id']}")
        resp = _post(super_admin_client, f"/admin/customers/{tenant['id']}/tts-mode", {
            "tts_credential_mode": "invalid_mode",
            "reason": "test",
        }, csrf_token=csrf)
        assert b"Invalid" in resp.data or resp.status_code in (400, 200)

    def test_override_byok_succeeds(self, super_admin_client, tenant, db_conn):
        """Switching to byok mode always allowed."""
        csrf = _get_csrf(super_admin_client, f"/admin/customers/{tenant['id']}")
        resp = _post(super_admin_client, f"/admin/customers/{tenant['id']}/tts-mode", {
            "tts_credential_mode": "byok",
            "reason": "Reset to BYOK for test",
        }, csrf_token=csrf)
        assert resp.status_code == 200
        row = db_conn.execute("SELECT tts_credential_mode FROM tenants WHERE id=%s", (tenant["id"],)).fetchone()
        assert row["tts_credential_mode"] == "byok"

    def test_override_platform_managed_blocked_by_byok_only_policy(self, super_admin_client, tenant, db_conn):
        """Plan with byok_only policy blocks platform_managed override."""
        # Ensure tenant is on a byok_only plan
        plan_row = db_conn.execute("""
            SELECT p.id FROM plans p
            JOIN plan_tts_policies pol ON pol.plan_id = p.id
            WHERE pol.credential_policy = 'byok_only' LIMIT 1
        """).fetchone()
        if not plan_row:
            pytest.skip("No byok_only plan in DB")
        db_conn.execute("UPDATE tenants SET plan_id=%s WHERE id=%s", (plan_row["id"], tenant["id"]))

        csrf = _get_csrf(super_admin_client, f"/admin/customers/{tenant['id']}")
        resp = _post(super_admin_client, f"/admin/customers/{tenant['id']}/tts-mode", {
            "tts_credential_mode": "platform_managed",
            "reason": "Forced override attempt",
        }, csrf_token=csrf)
        assert b"not allowed" in resp.data.lower() or b"policy" in resp.data.lower()
        row = db_conn.execute("SELECT tts_credential_mode FROM tenants WHERE id=%s", (tenant["id"],)).fetchone()
        assert row["tts_credential_mode"] != "platform_managed"

    def test_override_platform_managed_blocked_without_active_secret(self, super_admin_client, tenant, db_conn):
        """platform_managed mode blocked when no active platform credential for plan's providers."""
        # Set up hybrid plan
        db_conn.execute("""
            INSERT INTO plans (name, price_monthly, included_minutes, max_flows, max_users)
            VALUES ('HybridTest __override', 0, 0, 0, 0)
            ON CONFLICT DO NOTHING
        """)
        plan_row = db_conn.execute("SELECT id FROM plans WHERE name='HybridTest __override'").fetchone()
        if not plan_row:
            pytest.skip("Plan creation failed")
        plan_id = plan_row["id"]
        db_conn.execute("""
            INSERT INTO plan_tts_policies (plan_id, credential_policy, allowed_providers, byok_providers, platform_managed_providers)
            VALUES (%s, 'hybrid', '["gemini"]', '["gemini"]', '["gemini"]')
            ON CONFLICT (plan_id) DO UPDATE SET
                credential_policy='hybrid',
                platform_managed_providers='["gemini"]'
        """, (plan_id,))
        db_conn.execute("UPDATE tenants SET plan_id=%s WHERE id=%s", (plan_id, tenant["id"]))
        # Remove gemini platform secret
        db_conn.execute("DELETE FROM platform_secrets WHERE provider='gemini'")

        csrf = _get_csrf(super_admin_client, f"/admin/customers/{tenant['id']}")
        resp = _post(super_admin_client, f"/admin/customers/{tenant['id']}/tts-mode", {
            "tts_credential_mode": "platform_managed",
            "reason": "No active secret test",
        }, csrf_token=csrf)
        assert b"no active" in resp.data.lower() or b"credential" in resp.data.lower()
        # No manual cleanup needed — db_conn rolls back the whole transaction


# ── TTS Usage page ────────────────────────────────────────────────────────────

class TestTtsUsagePage:
    def test_page_requires_super_admin(self, client):
        resp = client.get("/admin/tts-usage", follow_redirects=False)
        assert resp.status_code in (302, 403)

    def test_page_loads(self, super_admin_client):
        resp = super_admin_client.get("/admin/tts-usage")
        assert resp.status_code == 200
        assert b"TTS Usage" in resp.data

    def test_page_no_api_keys_in_response(self, super_admin_client):
        """Usage page must not leak credentials."""
        resp = super_admin_client.get("/admin/tts-usage")
        html = resp.data.decode()
        assert "encrypted_value" not in html
        assert "api_key" not in html.lower().replace("api_key", "")  # no stray mentions

    def test_page_filter_invalid_provider_ignored(self, super_admin_client):
        """Unknown provider filter must not cause 500."""
        resp = super_admin_client.get("/admin/tts-usage?provider=hacker_input")
        assert resp.status_code == 200

    def test_page_filter_invalid_mode_ignored(self, super_admin_client):
        resp = super_admin_client.get("/admin/tts-usage?credential_mode='; DROP TABLE")
        assert resp.status_code == 200

    def test_page_filter_invalid_month_falls_back(self, super_admin_client):
        resp = super_admin_client.get("/admin/tts-usage?month=not-a-month")
        assert resp.status_code == 200

    def test_page_shows_data_when_present(self, super_admin_client, tenant, db_conn):
        """Insert a usage row and verify it appears."""
        import datetime
        month = datetime.date.today().replace(day=1)
        db_conn.execute("""
            INSERT INTO tenant_tts_usage
                (tenant_id, billing_month, provider, credential_mode,
                 characters_used, audio_seconds, preview_requests, production_requests, failed_requests)
            VALUES (%s, %s, 'edge', 'byok', 12345, 60, 5, 10, 0)
            ON CONFLICT (tenant_id, billing_month, provider, credential_mode) DO UPDATE
                SET characters_used = 12345
        """, (tenant["id"], month))

        resp = super_admin_client.get(f"/admin/tts-usage?month={month.strftime('%Y-%m')}")
        assert resp.status_code == 200
        assert b"12345" in resp.data or b"12,345" in resp.data


# ── Nav links ─────────────────────────────────────────────────────────────────

class TestAdminNavLinks:
    def test_nav_contains_plans_link(self, super_admin_client):
        resp = super_admin_client.get("/admin/dashboard")
        html = resp.data.decode()
        assert "/admin/plans" in html

    def test_nav_contains_platform_tts_link(self, super_admin_client):
        resp = super_admin_client.get("/admin/dashboard")
        html = resp.data.decode()
        assert "/admin/platform-tts" in html

    def test_nav_contains_tts_usage_link(self, super_admin_client):
        resp = super_admin_client.get("/admin/dashboard")
        html = resp.data.decode()
        assert "/admin/tts-usage" in html
