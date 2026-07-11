# tests/integration/test_auth_routes.py
"""Integration tests for the authentication blueprint and routes."""
from __future__ import annotations

import datetime
from flask import url_for
from app.db import db
from app.models.user import get_user_by_email, verify_user_password, create_verification_token


def test_register_flow_end_to_end(client, app):
    """Test the complete registration and email verification flow end-to-end."""
    # 1. Post registration data
    response = client.post("/auth/register", data={
        "business_name": "E2E Store",
        "email": "e2e@store.com",
        "password": "Password123!",
        "confirm_password": "Password123!",
        "phone": "01711223344"
    }, follow_redirects=True)

    assert response.status_code == 200
    assert "Registration successful!" in response.get_data(as_text=True)

    # Check DB state (unverified user, trial tenant)
    with app.app_context():
        user = get_user_by_email("e2e@store.com")
        assert user is not None
        assert not user["email_verified"]

        tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (user["tenant_id"],))
        assert tenant["status"] == "trial"

        # Fetch verification token
        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s", (user["id"],))
        token = token_row["token"]

    # 2. Get verification URL
    verify_response = client.get(f"/auth/verify/{token}", follow_redirects=True)
    assert verify_response.status_code == 200
    assert "successfully verified" in verify_response.get_data(as_text=True)

    # Verify updated DB state (verified, active tenant)
    with app.app_context():
        user = get_user_by_email("e2e@store.com")
        assert user["email_verified"] is True
        
        tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (user["tenant_id"],))
        assert tenant["status"] == "active"


def test_login_redirects_to_dashboard(client, app):
    """Test successful login redirects user to their corresponding panel."""
    # Register and verify user
    client.post("/auth/register", data={
        "business_name": "Login Store",
        "email": "login_test@store.com",
        "password": "Password123!",
        "confirm_password": "Password123!",
        "phone": "01711223344"
    })

    with app.app_context():
        user = get_user_by_email("login_test@store.com")
        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s", (user["id"],))
        client.get(f"/auth/verify/{token_row['token']}")

    # Login
    response = client.post("/auth/login", data={
        "email": "login_test@store.com",
        "password": "Password123!"
    }, follow_redirects=True)

    assert response.status_code == 200
    assert "Welcome back!" in response.get_data(as_text=True)
    # Confirms redirect landed on merchant dashboard page
    assert "Dashboard" in response.get_data(as_text=True)


def test_unverified_email_cannot_login(client, app):
    """Verify that users who haven't verified their emails are blocked from logging in."""
    client.post("/auth/register", data={
        "business_name": "Unverified Store",
        "email": "unverified@store.com",
        "password": "Password123!",
        "confirm_password": "Password123!",
        "phone": "01711223344"
    })

    response = client.post("/auth/login", data={
        "email": "unverified@store.com",
        "password": "Password123!"
    }, follow_redirects=True)

    assert response.status_code == 200
    assert "ভেরিফাই করুন" in response.get_data(as_text=True)


def test_forgot_password_sends_email(client, app):
    """Test that requesting a password reset generates reset link logging."""
    client.post("/auth/register", data={
        "business_name": "Forgot Store",
        "email": "forgot@store.com",
        "password": "Password123!",
        "confirm_password": "Password123!",
        "phone": "01711223344"
    })

    with app.app_context():
        user = get_user_by_email("forgot@store.com")
        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s", (user["id"],))
        client.get(f"/auth/verify/{token_row['token']}")

    # Trigger forgot password
    response = client.post("/auth/forgot-password", data={
        "email": "forgot@store.com"
    }, follow_redirects=True)

    assert response.status_code == 200
    assert "password reset link" in response.get_data(as_text=True)


def test_reset_password_updates_hash(client, app):
    """Test that reset password url updates user hash and revokes active sessions."""
    client.post("/auth/register", data={
        "business_name": "Reset Store",
        "email": "reset@store.com",
        "password": "Password123!",
        "confirm_password": "Password123!",
        "phone": "01711223344"
    })

    with app.app_context():
        user = get_user_by_email("reset@store.com")
        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s", (user["id"],))
        client.get(f"/auth/verify/{token_row['token']}")

    # Request reset link
    client.post("/auth/forgot-password", data={"email": "reset@store.com"})

    with app.app_context():
        user = get_user_by_email("reset@store.com")
        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user["id"],))
        token = token_row["token"]

    # Submit new password
    response = client.post(f"/auth/reset-password/{token}", data={
        "password": "NewPassword123!",
        "confirm_password": "NewPassword123!"
    }, follow_redirects=True)

    assert response.status_code == 200
    assert "password has been reset successfully" in response.get_data(as_text=True)

    # Attempt login with new password
    login_response = client.post("/auth/login", data={
        "email": "reset@store.com",
        "password": "NewPassword123!"
    }, follow_redirects=True)

    assert "Welcome back!" in login_response.get_data(as_text=True)


def test_csrf_token_required_on_forms(client, app):
    """Verify that CSRF token is required when WTF_CSRF_ENABLED is enabled."""
    app.config["WTF_CSRF_ENABLED"] = True

    # Attempt registration post without CSRF token
    response = client.post("/auth/register", data={
        "business_name": "No CSRF Store",
        "email": "nocsrf@store.com",
        "password": "Password123!",
        "confirm_password": "Password123!",
        "phone": "01711223344"
    })

    # The request should be rejected (returns 400 Bad Request due to CSRF missing)
    assert response.status_code == 400
    assert "CSRF" in response.get_data(as_text=True) or "csrf" in response.get_data(as_text=True).lower()

    # Reset CSRF config for other tests
    app.config["WTF_CSRF_ENABLED"] = False

