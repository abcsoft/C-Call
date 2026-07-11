import bcrypt
from flask import url_for
from app.db import db
from app.models.user import get_user_by_email, verify_user_password

def test_auth_registration_and_verification(client, app):
    # 1. Test registration
    response = client.post("/auth/register", data={
        "business_name": "Mega Store",
        "email": "mega@store.com",
        "password": "Password123!",
        "confirm_password": "Password123!"
    }, follow_redirects=True)
    
    assert response.status_code == 200
    assert b"Registration successful!" in response.data
    
    # Verify DB state (unverified admin user, trial tenant)
    with app.app_context():
        user = get_user_by_email("mega@store.com")
        assert user is not None
        assert user["role"] == "admin"
        assert not user["email_verified"]
        
        tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (user["tenant_id"],))
        assert tenant["status"] == "trial"
        
        # Verify password is encrypted with bcrypt
        assert verify_user_password(user["password_hash"], "Password123!")
        
        # Fetch the generated verification token
        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s", (user["id"],))
        assert token_row is not None
        token = token_row["token"]

    # 2. Test email verification endpoint
    verify_response = client.get(f"/auth/verify/{token}", follow_redirects=True)
    assert verify_response.status_code == 200
    assert b"successfully verified" in verify_response.data
    
    # Verify DB state updated
    with app.app_context():
        user = get_user_by_email("mega@store.com")
        assert user["email_verified"] is True
        
        tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (user["tenant_id"],))
        assert tenant["status"] == "active"


def test_login_logout_flow(client, app):
    # Register and verify
    client.post("/auth/register", data={
        "business_name": "Mega Store 2",
        "email": "mega2@store.com",
        "password": "Password123!",
        "confirm_password": "Password123!"
    })
    
    with app.app_context():
        user = get_user_by_email("mega2@store.com")
        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s", (user["id"],))
        token = token_row["token"]
        
    client.get(f"/auth/verify/{token}")
    
    # Test Login
    login_resp = client.post("/auth/login", data={
        "email": "mega2@store.com",
        "password": "Password123!"
    }, follow_redirects=True)
    assert login_resp.status_code == 200
    assert b"Welcome back!" in login_resp.data
    assert b"Dashboard" in login_resp.data

    # Test Logout
    logout_resp = client.get("/auth/logout", follow_redirects=True)
    assert logout_resp.status_code == 200
    assert b"You have been logged out." in logout_resp.data


def test_password_reset_flow(client, app):
    # Register and verify user
    client.post("/auth/register", data={
        "business_name": "Mega Store 3",
        "email": "mega3@store.com",
        "password": "Password123!",
        "confirm_password": "Password123!"
    })
    
    with app.app_context():
        user = get_user_by_email("mega3@store.com")
        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s", (user["id"],))
        client.get(f"/auth/verify/{token_row['token']}")

    # 1. Trigger forgot password
    forgot_resp = client.post("/auth/forgot-password", data={
        "email": "mega3@store.com"
    }, follow_redirects=True)
    assert forgot_resp.status_code == 200
    assert b"password reset link" in forgot_resp.data
    
    # Fetch reset token
    with app.app_context():
        user = get_user_by_email("mega3@store.com")
        # Get the latest token (since we have registration token and forgot token)
        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user["id"],))
        token = token_row["token"]
        
    # 2. Reset password
    reset_resp = client.post(f"/auth/reset-password/{token}", data={
        "password": "NewPassword456!",
        "confirm_password": "NewPassword456!"
    }, follow_redirects=True)
    assert reset_resp.status_code == 200
    assert b"password has been reset successfully" in reset_resp.data
    
    # Try logging in with new password
    login_resp = client.post("/auth/login", data={
        "email": "mega3@store.com",
        "password": "NewPassword456!"
    }, follow_redirects=True)
    assert login_resp.status_code == 200
    assert b"Welcome back!" in login_resp.data
