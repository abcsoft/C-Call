# tests/unit/test_auth_service.py
"""Unit tests for the authentication and registration service layer."""
from __future__ import annotations

import datetime
from decimal import Decimal
import pytest
from app.db import db
from app.extensions import redis_ext
from app.models.tenant import get_tenant_by_id
from app.models.user import get_user_by_email, get_verification_token, verify_user_password
from app.services.auth_service import (
    register_tenant,
    verify_email,
    generate_api_key,
    hash_api_key,
    authenticate_user,
)


def test_register_creates_tenant_user_wallet_and_retry_rules(app):
    """Verify registration successfully creates all required records (tenant, user, wallet, retry_rules, and rates)."""
    with app.app_context():
        tenant, user = register_tenant({
            "business_name": "Unit test Store",
            "email": "unit@store.com",
            "password": "Password123!",
            "phone": "01711223344"
        })

        assert tenant["id"] is not None
        assert user["id"] is not None
        assert user["email"] == "unit@store.com"
        assert not user["email_verified"]
        assert tenant["status"] == "trial"

        # Check wallet created
        wallet = db.query_one("SELECT * FROM wallets WHERE tenant_id = %s", (tenant["id"],))
        assert wallet is not None
        assert Decimal(wallet["balance"]) == Decimal("0.0")

        # Check default retry rules (3 rows)
        retry_rules = db.query("SELECT * FROM retry_rules WHERE tenant_id = %s", (tenant["id"],))
        assert len(retry_rules) == 3

        # Check default rates
        rates = db.query_one("SELECT * FROM customer_rates WHERE tenant_id = %s", (tenant["id"],))
        assert rates is not None
        assert Decimal(rates["rate_per_min"]) == Decimal("0.80")


def test_register_duplicate_email_raises_error(app):
    """Verify duplicate email registrations raise a ValueError."""
    with app.app_context():
        register_tenant({
            "business_name": "Store A",
            "email": "dup@store.com",
            "password": "Password123!",
            "phone": "01700000000"
        })

        with pytest.raises(ValueError) as exc:
            register_tenant({
                "business_name": "Store B",
                "email": "dup@store.com",
                "password": "Password123!",
                "phone": "01700000000"
            })
        assert "ইতিমধ্যেই নিবন্ধিত" in str(exc.value)


def test_password_hashed_with_bcrypt(app):
    """Verify passwords are encrypted using bcrypt cost 12."""
    with app.app_context():
        _, user = register_tenant({
            "business_name": "Hash Store",
            "email": "hash@store.com",
            "password": "SecretPassword123",
            "phone": "01700000000"
        })

        user_db = db.query_one("SELECT password_hash FROM users WHERE id = %s", (user["id"],))
        pw_hash = user_db["password_hash"]
        
        # Verify it is a valid bcrypt hash (starts with $2b$)
        assert pw_hash.startswith("$2b$12$")
        assert verify_user_password(pw_hash, "SecretPassword123")
        assert not verify_user_password(pw_hash, "WrongPassword")


def test_api_key_stored_as_sha256_not_plaintext(app):
    """Verify API keys are hashed with SHA-256 and never stored in plaintext."""
    with app.app_context():
        tenant_id = db.insert_returning_id(
            "INSERT INTO tenants (business_name, email) VALUES ('Key Store', 'key@store.com') RETURNING id"
        )
        raw_key = generate_api_key(tenant_id, name="My REST Key")

        # Plaintext key should not be in the database
        key_in_db = db.query_one("SELECT * FROM api_keys WHERE tenant_id = %s", (tenant_id,))
        assert key_in_db is not None
        assert key_in_db["key_hash"] == hash_api_key(raw_key)
        assert raw_key not in key_in_db["key_hash"]


def test_login_fail_increments_redis_counter(client, app):
    """Verify failed login attempts increment the Redis locking counter."""
    email = "fail_counter@store.com"
    redis_client = redis_ext.client
    redis_key = f"login_fail:{email}"
    redis_client.delete(redis_key)

    # Post invalid login
    client.post("/auth/login", data={"email": email, "password": "WrongPassword"})

    # Check counter in Redis
    fail_count = redis_client.get(redis_key)
    assert fail_count is not None
    assert int(fail_count) == 1


def test_login_locked_after_5_failures(client, app):
    """Verify login is locked with a 403 Forbidden after 5 consecutive failures."""
    email = "lockout@store.com"
    redis_client = redis_ext.client
    redis_key = f"login_fail:{email}"
    redis_client.delete(redis_key)

    # Simulate 5 failures
    for _ in range(5):
        client.post("/auth/login", data={"email": email, "password": "WrongPassword"})

    # The 6th attempt should return 403 Forbidden
    response = client.post("/auth/login", data={"email": email, "password": "WrongPassword"})
    assert response.status_code == 403
    assert b"Account locked" in response.data or b"locked" in response.data.lower()

    # Cleanup
    redis_client.delete(redis_key)


def test_verify_email_sets_tenant_active(app):
    """Verify successful verification sets user email_verified to true and tenant status to active."""
    with app.app_context():
        tenant, user = register_tenant({
            "business_name": "Verification Store",
            "email": "verify@store.com",
            "password": "Password123!",
            "phone": "01700000000"
        })

        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s", (user["id"],))
        token = token_row["token"]

        # Verify email using the service function
        verified_user = verify_email(token)
        assert verified_user["email_verified"] is True

        # Check tenant status updated to active
        tenant_db = get_tenant_by_id(tenant["id"])
        assert tenant_db["status"] == "active"


def test_expired_verification_token_rejected(app):
    """Verify expired verification tokens are rejected with a ValueError."""
    with app.app_context():
        tenant, user = register_tenant({
            "business_name": "Expiry Store",
            "email": "expired@store.com",
            "password": "Password123!",
            "phone": "01700000000"
        })

        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s", (user["id"],))
        token = token_row["token"]

        # Backdate the token expiry in DB
        past_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
        db.execute("UPDATE email_verifications SET expires_at = %s WHERE token = %s", (past_time, token))

        with pytest.raises(ValueError) as exc:
            verify_email(token)
        assert "মেয়াদ শেষ" in str(exc.value) or "expired" in str(exc.value).lower()


def test_authenticate_user_success(app):
    """Verify that authenticate_user succeeds with valid credentials."""
    with app.app_context():
        tenant, user = register_tenant({
            "business_name": "Auth Success Store",
            "email": "auth_succ@store.com",
            "password": "Password123!",
            "phone": "01700000000"
        })
        
        authenticated = authenticate_user("auth_succ@store.com", "Password123!")
        assert authenticated["id"] == user["id"]
        assert authenticated["email"] == "auth_succ@store.com"


def test_authenticate_user_invalid_email(app):
    """Verify authenticate_user raises AuthError with a non-existent email."""
    with app.app_context():
        from app.services.auth_service import authenticate_user, AuthError
        with pytest.raises(AuthError) as exc:
            authenticate_user("nonexistent@store.com", "Password123!")
        assert "ইমেইল অথবা পাসওয়ার্ডটি সঠিক নয়" in str(exc.value)


def test_authenticate_user_wrong_password(app):
    """Verify authenticate_user raises AuthError with an incorrect password."""
    with app.app_context():
        tenant, user = register_tenant({
            "business_name": "Wrong PW Store",
            "email": "wrongpw@store.com",
            "password": "Password123!",
            "phone": "01700000000"
        })
        
        from app.services.auth_service import authenticate_user, AuthError
        with pytest.raises(AuthError) as exc:
            authenticate_user("wrongpw@store.com", "WrongPassword!")
        assert "ইমেইল অথবা পাসওয়ার্ডটি সঠিক নয়" in str(exc.value)


def test_verify_email_invalid_token(app):
    """Verify verify_email raises ValueError with a non-existent token."""
    with app.app_context():
        with pytest.raises(ValueError) as exc:
            verify_email("invalidtoken123")
        assert "টোকেনটি সঠিক নয়" in str(exc.value)


def test_verify_email_user_not_found(app, monkeypatch):
    """Verify verify_email raises ValueError if the user associated with the token does not exist."""
    with app.app_context():
        tenant, user = register_tenant({
            "business_name": "No User Store",
            "email": "nouser@store.com",
            "password": "Password123!",
            "phone": "01700000000"
        })
        token_row = db.query_one("SELECT token FROM email_verifications WHERE user_id = %s", (user["id"],))
        token = token_row["token"]
        
        # Mock get_user_by_id to return None
        monkeypatch.setattr("app.services.auth_service.get_user_by_id", lambda x: None)
        
        with pytest.raises(ValueError) as exc:
            verify_email(token)
        assert "ইউজার পাওয়া যায়নি" in str(exc.value)


def test_register_tenant_creation_failure(app, monkeypatch):
    """Verify register_tenant raises ValueError if user or tenant record retrieval fails after creation."""
    with app.app_context():
        monkeypatch.setattr("app.services.auth_service.get_user_by_id", lambda x: None)
        
        with pytest.raises(ValueError) as exc:
            register_tenant({
                "business_name": "Failure Store",
                "email": "fail_store@store.com",
                "password": "Password123!",
                "phone": "01700000000"
            })
        assert "নিবন্ধন সম্পন্ন করার সময় ত্রুটি ঘটেছে" in str(exc.value)

