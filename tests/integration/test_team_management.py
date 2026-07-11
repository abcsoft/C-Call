# tests/integration/test_team_management.py
import datetime
import pytest
from flask import url_for
from flask_login import current_user
from app.db import db
from app.models.user import get_user_by_email, verify_user_password, User
from app.services import team_service

@pytest.fixture(autouse=True)
def raise_plan_limits(app):
    """Ensure plan limits are raised for all tests by default so invitations don't get blocked."""
    with app.app_context():
        db.execute("UPDATE plans SET max_users = 5")


def test_plan_limit_enforcement(client, app, create_tenant, auth_client):
    tenant, admin = create_tenant("Limit Test Corp", "limit_admin@test.com")
    
    with app.app_context():
        # Set max_users to 1 to trigger limit immediately
        db.execute("UPDATE plans SET max_users = 1 WHERE id = %s", (tenant["plan_id"],))
    
    auth_client((tenant, admin))
    
    # Try inviting a new user. It should fail.
    response = client.post("/api/team/invite", data={
        "email": "another_user@test.com",
        "role": "agent"
    }, follow_redirects=True)
    
    assert response.status_code == 200
    assert b"Upgrade plan to add more team members" in response.data

    with app.app_context():
        # Verify no invitation was created in the DB
        invite = db.query_one("SELECT * FROM team_invitations WHERE email = 'another_user@test.com'")
        assert invite is None


def test_invitation_lifecycle(client, app, create_tenant, auth_client):
    tenant, admin = create_tenant("Lifecycle Corp", "lifecycle_admin@test.com")
    auth_client((tenant, admin))
    
    # 1. Admin sends invitation
    response = client.post("/api/team/invite", data={
        "email": "new_agent@test.com",
        "role": "agent"
    }, follow_redirects=True)
    
    assert response.status_code == 200
    assert b"Invitation sent to new_agent@test.com successfully" in response.data
    
    with app.app_context():
        # Verify invitation row exists
        invite = db.query_one("SELECT * FROM team_invitations WHERE email = 'new_agent@test.com'")
        assert invite is not None
        assert invite["role"] == "agent"
        assert invite["used_at"] is None
        assert invite["expires_at"] > datetime.datetime.now(datetime.timezone.utc)
        token = invite["token"]
        
        # Verify placeholder user row exists
        placeholder_user = get_user_by_email("new_agent@test.com")
        assert placeholder_user is not None
        assert not placeholder_user["email_verified"]

    # Log out admin
    client.get("/auth/logout")
    
    # 2. GET accept-invite page
    accept_get = client.get(f"/auth/accept-invite/{token}")
    assert accept_get.status_code == 200
    assert b"Join the Team" in accept_get.data
    assert b"new_agent@test.com" in accept_get.data
    assert b"Lifecycle Corp" in accept_get.data

    # 3. POST accept-invite with password to join
    accept_post = client.post(f"/auth/accept-invite/{token}", data={
        "password": "NewSecretPassword123!",
        "confirm_password": "NewSecretPassword123!"
    }, follow_redirects=True)
    
    assert accept_post.status_code == 200
    assert b"Welcome to the team!" in accept_post.data
    assert b"Dashboard" in accept_post.data

    with app.app_context():
        # Check user is updated and verified
        user = get_user_by_email("new_agent@test.com")
        assert user["email_verified"] is True
        assert verify_user_password(user["password_hash"], "NewSecretPassword123!")
        
        # Check membership is active
        membership = db.query_one(
            "SELECT * FROM user_tenant_memberships WHERE user_id = %s AND tenant_id = %s",
            (user["id"], tenant["id"])
        )
        assert membership is not None
        assert membership["role"] == "agent"
        assert membership["is_active"] is True
        
        # Check invitation is marked used
        invite_updated = db.query_one("SELECT * FROM team_invitations WHERE id = %s", (invite["id"],))
        assert invite_updated["used_at"] is not None


def test_multi_tenant_user_switching(client, app, create_tenant, auth_client):
    # 1. Create two tenants
    tenant_a, admin_a = create_tenant("Tenant A", "admin_a@test.com")
    tenant_b, admin_b = create_tenant("Tenant B", "admin_b@test.com")
    
    # 2. Invite admin_a to Tenant B
    auth_client((tenant_b, admin_b))
    client.post("/api/team/invite", data={
        "email": "admin_a@test.com",
        "role": "agent"
    })
    
    with app.app_context():
        invite = db.query_one("SELECT * FROM team_invitations WHERE email = 'admin_a@test.com' AND tenant_id = %s", (tenant_b["id"],))
        assert invite is not None
        token = invite["token"]
        
    # Log out
    client.get("/auth/logout")
    
    # 3. Log in as admin_a (who is currently in Tenant A)
    auth_client((tenant_a, admin_a))
    
    # 4. Accept invite to Tenant B (already logged in/exists, no password required)
    accept_post = client.post(f"/auth/accept-invite/{token}", data={}, follow_redirects=True)
    assert accept_post.status_code == 200
    assert b"Welcome to the team!" in accept_post.data
    
    with app.app_context():
        # admin_a is now switched to Tenant B context
        user = get_user_by_email("admin_a@test.com")
        assert user["tenant_id"] == tenant_b["id"]
        assert user["role"] == "agent"
        
        # Verify they have memberships in both tenants
        memberships = db.query("SELECT * FROM user_tenant_memberships WHERE user_id = %s ORDER BY tenant_id", (user["id"],))
        assert len(memberships) == 2
        assert memberships[0]["tenant_id"] == tenant_a["id"]
        assert memberships[0]["role"] == "admin"
        assert memberships[1]["tenant_id"] == tenant_b["id"]
        assert memberships[1]["role"] == "agent"

    # 5. Switch back to Tenant A
    switch_resp = client.post(f"/settings/team/switch/{tenant_a['id']}", follow_redirects=True)
    assert switch_resp.status_code == 200
    assert b"Switched tenant context successfully" in switch_resp.data
    
    with app.app_context():
        user = get_user_by_email("admin_a@test.com")
        assert user["tenant_id"] == tenant_a["id"]
        assert user["role"] == "admin"


def test_change_role_safeguards(client, app, create_tenant, auth_client):
    tenant, admin = create_tenant("Role Guard Corp", "roleguard_admin@test.com")
    
    # Create another user in this tenant
    with app.app_context():
        from app.models.user import create_user
        agent_id = create_user(tenant["id"], "roleguard_agent@test.com", "Password123!", "agent", True)
        
    auth_client((tenant, admin))
    
    # 1. Admin demotes agent to viewer
    response = client.post(f"/api/team/users/{agent_id}/role", data={
        "role": "viewer"
    }, follow_redirects=True)
    
    assert response.status_code == 200
    assert b"User role updated successfully" in response.data
    
    with app.app_context():
        membership = db.query_one("SELECT role FROM user_tenant_memberships WHERE user_id = %s AND tenant_id = %s", (agent_id, tenant["id"]))
        assert membership["role"] == "viewer"
        
        # Check audit log
        audit = db.query_one("SELECT * FROM audit_log WHERE admin_user_id = %s ORDER BY id DESC LIMIT 1", (admin["id"],))
        assert audit["action"] == "change_role"
        assert f"Changed role of user {agent_id} to viewer" in audit["reason"]

    # 2. Try to change own role (fails)
    response_self = client.post(f"/api/team/users/{admin['id']}/role", data={
        "role": "agent"
    }, follow_redirects=True)
    assert b"Cannot change your own role" in response_self.data

    # 3. Try to demote last admin (fails)
    # We promote the viewer to agent to test that even with other active users, we cannot demote the only admin
    client.post(f"/api/team/users/{agent_id}/role", data={"role": "agent"})
    
    # Log in as agent and try to demote admin (fails because agent has no permission to POST to /api/team/users)
    client.get("/auth/logout")
    with app.app_context():
        agent_user = get_user_by_email("roleguard_agent@test.com")
    auth_client((tenant, agent_user))
    
    response_no_perm = client.post(f"/api/team/users/{admin['id']}/role", data={
        "role": "agent"
    }, follow_redirects=True)
    assert response_no_perm.status_code == 403  # Forbidden (not admin)


def test_remove_user_safeguards(client, app, create_tenant, auth_client):
    tenant, admin = create_tenant("Remove Guard Corp", "removeguard_admin@test.com")
    
    with app.app_context():
        from app.models.user import create_user
        agent_id = create_user(tenant["id"], "removeguard_agent@test.com", "Password123!", "agent", True)
        
    auth_client((tenant, admin))
    
    # 1. Try to remove self (fails)
    response_self = client.delete(f"/api/team/users/{admin['id']}")
    assert response_self.status_code == 400
    assert b"Cannot remove yourself from the team" in response_self.data

    # 2. Remove agent (succeeds)
    response_remove = client.delete(
        f"/api/team/users/{agent_id}",
        headers={"X-CSRFToken": client.environ_base.get("csrf_token", "")}
    )
    assert response_remove.status_code == 200
    
    with app.app_context():
        membership = db.query_one("SELECT * FROM user_tenant_memberships WHERE user_id = %s AND tenant_id = %s", (agent_id, tenant["id"]))
        assert membership is None
        
        # Check audit log
        audit = db.query_one("SELECT * FROM audit_log WHERE admin_user_id = %s ORDER BY id DESC LIMIT 1", (admin["id"],))
        assert audit["action"] == "remove_user"
        assert f"Removed user {agent_id} from team" in audit["reason"]
