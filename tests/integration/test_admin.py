# tests/integration/test_admin.py
"""Admin panel integration tests — tenant isolation, impersonation,
audited actions, and revenue/number-pool invariants."""
from __future__ import annotations

import datetime
from decimal import Decimal

import bcrypt

from app.db import db
from app.models.tenant import create_tenant
from app.models.user import create_user
from app.services.number_service import assign_number, add_to_pool
from app.services.wallet_service import credit as credit_wallet


def _login(client, email: str, password: str):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _make_super_admin(email: str, password: str = "AdminPass123!") -> int:
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    return db.insert_returning_id(
        "INSERT INTO users (tenant_id, email, password_hash, role, email_verified) "
        "VALUES (NULL, %s, %s, 'super_admin', true) RETURNING id",
        (email, password_hash),
    )


def _make_tenant_admin(business_name: str, email: str, password: str = "TenantPass123!") -> tuple[int, int]:
    tenant_id = create_tenant({"business_name": business_name, "email": f"biz-{email}", "status": "active"})
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    user_id = db.insert_returning_id(
        "INSERT INTO users (tenant_id, email, password_hash, role, email_verified) "
        "VALUES (%s, %s, %s, 'admin', true) RETURNING id",
        (tenant_id, email, password_hash),
    )
    return tenant_id, user_id


def _make_provider() -> int:
    from app.models.sip_provider import add_sip_provider
    return add_sip_provider("Test Trunk", "sip.test.local", 5060, "testuser", "testpass")


def test_super_admin_can_access_all_tenants(client, app):
    with app.app_context():
        _make_super_admin("super1@ccall.com")
        tenant_id, _ = _make_tenant_admin("Alpha Store", "alpha1@store.com")

    _login(client, "super1@ccall.com", "AdminPass123!")
    resp = client.get(f"/admin/customers/{tenant_id}")
    assert resp.status_code == 200
    assert b"Alpha Store" in resp.data


def test_tenant_admin_cannot_access_admin_panel(client, app):
    with app.app_context():
        _make_tenant_admin("Beta Store", "beta1@store.com")

    _login(client, "beta1@store.com", "TenantPass123!")
    resp = client.get("/admin/dashboard")
    assert resp.status_code == 403


def test_impersonation_creates_audit_log(client, app):
    with app.app_context():
        admin_id = _make_super_admin("super2@ccall.com")
        tenant_id, _ = _make_tenant_admin("Gamma Store", "gamma1@store.com")

    _login(client, "super2@ccall.com", "AdminPass123!")
    resp = client.post(
        f"/admin/customers/{tenant_id}/impersonate",
        data={"reason": "Investigating webhook failure"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        row = db.query_one(
            "SELECT * FROM audit_log WHERE action = 'impersonate_start' AND actor_user_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (admin_id,),
        )
        assert row is not None
        assert row["target_type"] == "tenant"
        assert row["target_tenant_id"] == tenant_id


def test_impersonation_scoped_to_target_tenant(client, app):
    with app.app_context():
        _make_super_admin("super3@ccall.com")
        tenant_a, _ = _make_tenant_admin("Delta Store", "delta1@store.com")
        tenant_b, _ = _make_tenant_admin("Epsilon Store", "epsilon1@store.com")
        credit_wallet(tenant_a, Decimal("250.00"), "seed")
        credit_wallet(tenant_b, Decimal("999.00"), "seed")

    _login(client, "super3@ccall.com", "AdminPass123!")
    client.post(f"/admin/customers/{tenant_a}/impersonate", data={"reason": "support"}, follow_redirects=True)

    resp = client.get("/dashboard")
    body = resp.get_data(as_text=True)
    assert "Delta Store" in body
    assert "999.00" not in body  # tenant B's balance must never leak into tenant A's impersonated session


def test_manual_wallet_credit_requires_reason(client, app):
    with app.app_context():
        _make_super_admin("super4@ccall.com")
        tenant_id, _ = _make_tenant_admin("Zeta Store", "zeta1@store.com")
        balance_before = Decimal("0.0000")

    _login(client, "super4@ccall.com", "AdminPass123!")
    resp = client.post(
        f"/admin/customers/{tenant_id}/wallet/adjust",
        data={"action": "credit", "amount": "100.00", "description": ""},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "reason is required" in resp.get_data(as_text=True).lower()

    with app.app_context():
        wallet = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tenant_id,))
        assert Decimal(str(wallet["balance"])) == balance_before


def test_suspend_tenant_blocks_api_calls(client, app):
    import hashlib
    import secrets as secrets_mod

    with app.app_context():
        tenant_id, _ = _make_tenant_admin("Eta Store", "eta1@store.com")
        raw_key = f"cc_live_{secrets_mod.token_urlsafe(32)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        db.execute(
            "INSERT INTO api_keys (tenant_id, name, key_hash, key_prefix, is_active) VALUES (%s, 'Test Key', %s, %s, true)",
            (tenant_id, key_hash, raw_key[:12]),
        )
        db.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (tenant_id,))

    resp = client.post(
        "/api/v1/orders",
        json={"order_id": "WC-1", "phone": "01712345678", "amount": "100"},
        headers={"X-API-Key": raw_key},
    )
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "TENANT_INACTIVE"


def test_revenue_report_totals_are_correct(app):
    from app.services.revenue_service import get_revenue_report

    with app.app_context():
        tenant_id, _ = _make_tenant_admin("Theta Store", "theta1@store.com")
        today = datetime.date.today()

        credit_wallet(tenant_id, Decimal("1000.00"), "seed")
        wallet = db.query_one("SELECT id FROM wallets WHERE tenant_id = %s", (tenant_id,))
        db.execute(
            "INSERT INTO wallet_transactions (wallet_id, tenant_id, type, amount, balance_after, description) "
            "VALUES (%s, %s, 'debit', %s, %s, 'call charge')",
            (wallet["id"], tenant_id, Decimal("45.50"), Decimal("954.50")),
        )

        report = get_revenue_report(today, today)
        tenant_row = next((r for r in report["per_tenant"] if r["tenant_id"] == tenant_id), None)
        assert tenant_row is not None
        assert tenant_row["revenue"] == Decimal("45.50")
        assert report["total_revenue"] >= Decimal("45.50")


def test_number_assigned_to_two_tenants_prevented(app):
    with app.app_context():
        tenant_a, _ = _make_tenant_admin("Iota Store", "iota1@store.com")
        tenant_b, _ = _make_tenant_admin("Kappa Store", "kappa1@store.com")
        provider_id = _make_provider()
        number = add_to_pool("01799900001", provider_id, "dedicated", Decimal("50.00"))

        assign_number(tenant_a, number["id"], "dedicated")

        try:
            assign_number(tenant_b, number["id"], "dedicated")
            assert False, "Expected ValueError when assigning an already-dedicated number to a second tenant"
        except ValueError as exc:
            assert "already assigned" in str(exc).lower()
