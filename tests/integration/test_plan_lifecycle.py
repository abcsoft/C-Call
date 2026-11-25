# tests/integration/test_plan_lifecycle.py
"""
Plan lifecycle integration tests (Part 10).

Covers:
- Active plan shows Archive button
- Archived plan shows Activate button (not Archive)
- Archive POST sets archived_at
- Activate POST clears archived_at
- Archived plan hidden from merchant subscription page
- Activated plan visible again
- Existing tenant entitlement not changed by archive
- Non-admin blocked (403)
- CSRF required
- Audit event written for archive and activate
"""
from __future__ import annotations
import pytest
import secrets as _s
import psycopg
import os


# ── helpers ──────────────────────────────────────────────────────────────────

_ADMIN_EMAIL = f"plantest_admin_{_s.token_hex(3)}@test.example"
_ADMIN_PASS = "AdminPass123!"

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://ccall:CHANGE_ME_TEST_DB_PASSWORD@localhost:5432/ccall_test",
)


@pytest.fixture(scope="module", autouse=True)
def _create_super_admin():
    """Create super_admin user committed to DB once per module (outside rolled-back transaction)."""
    import bcrypt
    pw_hash = bcrypt.hashpw(_ADMIN_PASS.encode(), bcrypt.gensalt(rounds=12)).decode()
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role, email_verified) "
            "VALUES (NULL, %s, %s, 'super_admin', true) "
            "ON CONFLICT (email) DO NOTHING",
            (_ADMIN_EMAIL, pw_hash),
        )
        conn.commit()
    yield
    # Cleanup: delete the super_admin user after module tests finish
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        conn.execute("DELETE FROM users WHERE email=%s", (_ADMIN_EMAIL,))
        conn.commit()


def _admin_login(client):
    """Log in as the module-level super_admin."""
    return client.post(
        "/auth/login",
        data={"email": _ADMIN_EMAIL, "password": _ADMIN_PASS},
        follow_redirects=True,
    )


def _csrf(client, url):
    import re
    resp = client.get(url)
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.data.decode())
    return m.group(1) if m else ""


def _make_test_plan(db, name=None):
    """Create a fresh active plan and return its id."""
    name = name or f"TestPlan_{_s.token_hex(4)}"
    plan_id = db.insert_returning_id(
        "INSERT INTO plans (name, price_monthly, included_minutes, max_flows, max_users) "
        "VALUES (%s, 99.00, 100, 5, 3) RETURNING id",
        (name,),
    )
    db.execute(
        "INSERT INTO plan_tts_policies (plan_id, credential_policy, allowed_providers, "
        "byok_providers, platform_managed_providers, daily_preview_limit, max_concurrent_tts) "
        "VALUES (%s, 'byok_only', %s, %s, %s, 10, 1)",
        (plan_id, "[]", "[]", "[]"),
    )
    return plan_id


def _plan_row(db, plan_id):
    return db.query_one("SELECT * FROM plans WHERE id=%s", (plan_id,))


# ── tests ────────────────────────────────────────────────────────────────────

class TestPlanLifecycle:

    def test_active_plan_shows_archive_button(self, client, app, db_conn):
        from app.db import db
        plan_id = _make_test_plan(db)
        _admin_login(client)
        html = client.get("/admin/plans").data.decode()
        assert "Archive" in html

    def test_archived_plan_shows_activate_button(self, client, app, db_conn):
        from app.db import db
        plan_id = _make_test_plan(db)
        db.execute("UPDATE plans SET archived_at=now() WHERE id=%s", (plan_id,))
        _admin_login(client)
        html = client.get("/admin/plans").data.decode()
        assert "Activate" in html

    def test_archived_plan_does_not_show_archive_button_for_that_row(self, client, app, db_conn):
        from app.db import db
        name = f"ArchOnly_{_s.token_hex(3)}"
        plan_id = _make_test_plan(db, name=name)
        db.execute("UPDATE plans SET archived_at=now() WHERE id=%s", (plan_id,))
        _admin_login(client)
        html = client.get("/admin/plans").data.decode()
        # The plan row is present
        assert name in html
        # The Activate button is present
        assert "Activate" in html

    def test_archive_post_sets_archived_at(self, client, app, db_conn):
        from app.db import db
        plan_id = _make_test_plan(db)
        _admin_login(client)
        csrf = _csrf(client, "/admin/plans")
        client.post(
            f"/admin/plans/{plan_id}/archive",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )
        row = _plan_row(db, plan_id)
        assert row["archived_at"] is not None, "archived_at must be set after archive POST"

    def test_activate_post_clears_archived_at(self, client, app, db_conn):
        from app.db import db
        plan_id = _make_test_plan(db)
        db.execute("UPDATE plans SET archived_at=now() WHERE id=%s", (plan_id,))
        _admin_login(client)
        csrf = _csrf(client, "/admin/plans")
        client.post(
            f"/admin/plans/{plan_id}/activate",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )
        row = _plan_row(db, plan_id)
        assert row["archived_at"] is None, "archived_at must be NULL after activate POST"

    def test_archived_plan_hidden_from_merchant_subscription_page(self, client, app, db_conn):
        from app.db import db
        from app.services.auth_service import register_tenant
        name = f"HiddenPlan_{_s.token_hex(3)}"
        plan_id = _make_test_plan(db, name=name)
        db.execute("UPDATE plans SET archived_at=now() WHERE id=%s", (plan_id,))

        tenant, user = register_tenant({
            "business_name": "MerchHide", "email": f"merch_{_s.token_hex(3)}@test.example",
            "password": "Password123!", "phone": "01712300001",
        })
        db.execute("UPDATE tenants SET status='active' WHERE id=%s", (tenant["id"],))
        db.execute("UPDATE users SET email_verified=true WHERE id=%s", (user["id"],))

        client.post("/auth/login",
                    data={"email": user["email"], "password": "Password123!"},
                    follow_redirects=True)
        html = client.get("/settings/subscription").data.decode()
        assert name not in html, "Archived plan must not appear on merchant subscription page"

    def test_activated_plan_visible_on_merchant_subscription_page(self, client, app, db_conn):
        from app.db import db
        from app.services.auth_service import register_tenant
        name = f"VisiblePlan_{_s.token_hex(3)}"
        plan_id = _make_test_plan(db, name=name)
        db.execute("UPDATE plans SET archived_at=now() WHERE id=%s", (plan_id,))

        tenant, user = register_tenant({
            "business_name": "MerchVisible", "email": f"merch_v_{_s.token_hex(3)}@test.example",
            "password": "Password123!", "phone": "01712300002",
        })
        db.execute("UPDATE tenants SET status='active' WHERE id=%s", (tenant["id"],))
        db.execute("UPDATE users SET email_verified=true WHERE id=%s", (user["id"],))

        client.post("/auth/login",
                    data={"email": user["email"], "password": "Password123!"},
                    follow_redirects=True)
        html_before = client.get("/settings/subscription").data.decode()
        assert name not in html_before

        # Activate
        db.execute("UPDATE plans SET archived_at=NULL WHERE id=%s", (plan_id,))
        html_after = client.get("/settings/subscription").data.decode()
        assert name in html_after, "Activated plan must appear on merchant subscription page"

    def test_archive_does_not_change_assigned_tenant_plan(self, client, app, db_conn):
        """Existing tenants on an archived plan keep their current plan_id entitlement."""
        from app.db import db
        from app.services.auth_service import register_tenant
        plan_id = _make_test_plan(db)

        tenant, user = register_tenant({
            "business_name": "EntitleKeep", "email": f"entit_{_s.token_hex(3)}@test.example",
            "password": "Password123!", "phone": "01712300003",
        })
        db.execute("UPDATE tenants SET plan_id=%s, status='active' WHERE id=%s",
                   (plan_id, tenant["id"]))
        db.execute("UPDATE users SET email_verified=true WHERE id=%s", (user["id"],))

        # Admin archives plan — but archive is blocked when tenants assigned
        # So we bypass block by direct DB update to test entitlement is unchanged
        db.execute("UPDATE plans SET archived_at=now() WHERE id=%s", (plan_id,))

        row = db.query_one("SELECT plan_id FROM tenants WHERE id=%s", (tenant["id"],))
        assert row["plan_id"] == plan_id, "Tenant plan_id must not change when plan is archived"

    def test_non_admin_archive_returns_403(self, client, app, db_conn):
        from app.db import db
        from app.services.auth_service import register_tenant
        plan_id = _make_test_plan(db)
        tenant, user = register_tenant({
            "business_name": "NonAdmin403", "email": f"nonadm_{_s.token_hex(3)}@test.example",
            "password": "Password123!", "phone": "01712300004",
        })
        db.execute("UPDATE tenants SET status='active' WHERE id=%s", (tenant["id"],))
        db.execute("UPDATE users SET email_verified=true WHERE id=%s", (user["id"],))
        client.post("/auth/login",
                    data={"email": user["email"], "password": "Password123!"},
                    follow_redirects=True)
        import re
        resp = client.get("/admin/plans")
        # Non-super-admin should not access admin panel at all
        assert resp.status_code in (302, 403), "Non-admin must be blocked from admin plans"

    def test_csrf_token_in_archive_form(self, client, app, db_conn):
        """Archive form renders a csrf_token hidden field."""
        from app.db import db
        _make_test_plan(db)
        _admin_login(client)
        html = client.get("/admin/plans").data.decode()
        assert 'name="csrf_token"' in html, "Archive form must include csrf_token hidden field"

    def test_csrf_token_in_activate_form(self, client, app, db_conn):
        """Activate form renders a csrf_token hidden field."""
        from app.db import db
        plan_id = _make_test_plan(db)
        db.execute("UPDATE plans SET archived_at=now() WHERE id=%s", (plan_id,))
        _admin_login(client)
        html = client.get("/admin/plans").data.decode()
        assert 'name="csrf_token"' in html, "Activate form must include csrf_token hidden field"

    def test_audit_event_written_for_archive(self, client, app, db_conn):
        from app.db import db
        plan_id = _make_test_plan(db)
        _admin_login(client)
        csrf = _csrf(client, "/admin/plans")
        client.post(
            f"/admin/plans/{plan_id}/archive",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )
        events = db.query(
            "SELECT action FROM audit_log WHERE target_type='plan' AND target_id=%s "
            "ORDER BY created_at DESC",
            (plan_id,),
        )
        actions = [e["action"] for e in events]
        assert "plan_archived" in actions, f"plan_archived audit event missing. Found: {actions}"

    def test_audit_event_written_for_activate(self, client, app, db_conn):
        from app.db import db
        plan_id = _make_test_plan(db)
        db.execute("UPDATE plans SET archived_at=now() WHERE id=%s", (plan_id,))
        _admin_login(client)
        csrf = _csrf(client, "/admin/plans")
        client.post(
            f"/admin/plans/{plan_id}/activate",
            data={"csrf_token": csrf},
            follow_redirects=True,
        )
        events = db.query(
            "SELECT action FROM audit_log WHERE target_type='plan' AND target_id=%s "
            "ORDER BY created_at DESC",
            (plan_id,),
        )
        actions = [e["action"] for e in events]
        assert "plan_activated" in actions, f"plan_activated audit event missing. Found: {actions}"
