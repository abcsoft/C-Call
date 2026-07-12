"""Integration-level fixtures shared across integration tests."""
from __future__ import annotations

import pytest


@pytest.fixture
def tenant(create_tenant, app):
    """A single registered tenant (dict)."""
    from app.db import db
    t, u = create_tenant("IntegrationTestCo", "integ_tenant_fixture@example.com")
    db.execute("UPDATE users SET email_verified = true WHERE id = %s", (u["id"],))
    db.execute("UPDATE tenants SET status = 'active' WHERE id = %s", (t["id"],))
    return t


@pytest.fixture
def tenant_user(create_tenant, app):
    """A registered tenant user — NOT super_admin."""
    from app.db import db
    t, u = create_tenant("TenantUserCo", "tenant_user_fixture@example.com")
    db.execute("UPDATE users SET email_verified = true WHERE id = %s", (u["id"],))
    db.execute("UPDATE tenants SET status = 'active' WHERE id = %s", (t["id"],))
    return {**u, "email": u["email"], "tenant_id": t["id"]}


@pytest.fixture
def super_admin_user(app):
    """Create a super_admin user (tenant_id IS NULL) for auth tests."""
    from app.db import db
    import bcrypt
    password_hash = bcrypt.hashpw(b"AdminPass123!", bcrypt.gensalt(rounds=12)).decode()
    existing = db.query_one("SELECT id FROM users WHERE email = 'super_admin_fixture@example.com'")
    if existing:
        db.execute(
            "UPDATE users SET role='super_admin', tenant_id=NULL, email_verified=true WHERE id=%s",
            (existing["id"],),
        )
        return {"id": existing["id"], "email": "super_admin_fixture@example.com"}
    user_id = db.insert_returning_id(
        """
        INSERT INTO users (email, password_hash, role, tenant_id, email_verified)
        VALUES (%s, %s, 'super_admin', NULL, true) RETURNING id
        """,
        ("super_admin_fixture@example.com", password_hash),
    )
    return {"id": user_id, "email": "super_admin_fixture@example.com"}


@pytest.fixture
def super_admin_client(client, super_admin_user, app):
    """HTTP test client logged in as super_admin."""
    # Get login page to grab CSRF token
    resp = client.get("/auth/login")
    html = resp.data.decode()
    start = html.find('name="csrf_token" value="') + len('name="csrf_token" value="')
    end = html.find('"', start)
    csrf = html[start:end]

    client.post("/auth/login", data={
        "email": super_admin_user["email"],
        "password": "AdminPass123!",
        "csrf_token": csrf,
    }, follow_redirects=True)
    return client


@pytest.fixture
def tenant_client(client, tenant_user, app):
    """HTTP test client logged in as a regular tenant user."""
    resp = client.get("/auth/login")
    html = resp.data.decode()
    start = html.find('name="csrf_token" value="') + len('name="csrf_token" value="')
    end = html.find('"', start)
    csrf = html[start:end]

    client.post("/auth/login", data={
        "email": tenant_user["email"],
        "password": "Password123!",
        "csrf_token": csrf,
    }, follow_redirects=True)
    return client
