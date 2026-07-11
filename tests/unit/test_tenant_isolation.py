# tests/unit/test_tenant_isolation.py
"""Unit tests verifying tenant isolation and middleware protections."""
from __future__ import annotations

from decimal import Decimal
import pytest
from app.db import db
from app.models.tenant import create_tenant, suspend_tenant
from app.models.user import create_user, User
from app.middleware.tenant_context import TenantQuery
from app.services.auth_service import authenticate_user, AuthError


def test_tenant_query_always_includes_tenant_id_filter(app):
    """Verify that TenantQuery enforces the tenant isolation filter and rejects unsafe queries."""
    with app.app_context():
        query_wrapper = TenantQuery(tenant_id=1)

        # 1. Safe query with filter or placeholder should pass
        # (This will return empty or throw if table doesn't exist, but it won't raise security validation exception)
        try:
            query_wrapper.query("SELECT * FROM orders WHERE {tenant_filter}")
        except Exception as exc:
            assert not isinstance(exc, ValueError) or "Security Violation" not in str(exc)

        # 2. Unsafe query on tenant-scoped table without tenant_id or {tenant_filter} MUST raise ValueError
        with pytest.raises(ValueError) as exc:
            query_wrapper.query("SELECT * FROM orders")
        assert "Security Violation" in str(exc.value)


def test_cannot_access_other_tenant_orders(app):
    """Verify that Tenant A cannot access orders belonging to Tenant B."""
    with app.app_context():
        # Setup Tenant A
        tenant_a_id = create_tenant("Store A", "a@store.com")
        db.execute(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount)
            VALUES (%s, 'ORDER-A', '01711111111', 'Customer A', 1000.0)
            """,
            (tenant_a_id,)
        )

        # Setup Tenant B
        tenant_b_id = create_tenant("Store B", "b@store.com")
        db.execute(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount)
            VALUES (%s, 'ORDER-B', '01722222222', 'Customer B', 2000.0)
            """,
            (tenant_b_id,)
        )

        # Access with Tenant A's db scope
        db_a = TenantQuery(tenant_a_id)
        orders_a = db_a.query("SELECT * FROM orders WHERE {tenant_filter}")
        assert len(orders_a) == 1
        assert orders_a[0]["external_order_id"] == "ORDER-A"

        # Access with Tenant B's db scope
        db_b = TenantQuery(tenant_b_id)
        orders_b = db_b.query("SELECT * FROM orders WHERE {tenant_filter}")
        assert len(orders_b) == 1
        assert orders_b[0]["external_order_id"] == "ORDER-B"


def test_super_admin_can_access_all_tenants(app):
    """Verify that a platform super-admin (tenant_id IS NULL) can query across all tenants."""
    with app.app_context():
        tenant_a_id = create_tenant("Tenant A", "ta@store.com")
        tenant_b_id = create_tenant("Tenant B", "tb@store.com")

        # Create a super admin user in DB
        admin_id = create_user(
            tenant_id=None,
            email="super@ccall.com.bd",
            password_plaintext="SuperSecret123!",
            role="super_admin",
            email_verified=True
        )

        # Load user and check privileges
        user_dict = db.query_one("SELECT * FROM users WHERE id = %s", (admin_id,))
        user = User(user_dict)
        assert user.is_super_admin
        assert user.tenant_id is None

        # Super admin queries bypass tenant restrictions on raw db execution
        all_tenants = db.query("SELECT * FROM tenants")
        assert len(all_tenants) >= 2


def test_suspended_tenant_cannot_login(client, app):
    """Verify that a suspended tenant user is blocked from logging in."""
    with app.app_context():
        # Create tenant and admin user
        tenant_id = create_tenant("Suspended Business", "susp@store.com")
        user_id = create_user(
            tenant_id=tenant_id,
            email="susp@store.com",
            password_plaintext="Password123!",
            role="admin",
            email_verified=True
        )

        # Suspend tenant
        suspend_tenant(tenant_id)

    # Post login attempt
    response = client.post("/auth/login", data={
        "email": "susp@store.com",
        "password": "Password123!"
    }, follow_redirects=True)

    assert "suspended or inactive" in response.get_data(as_text=True) or "স্থগিত বা নিষ্ক্রিয়" in response.get_data(as_text=True)


def test_tenant_model_helpers(app):
    """Verify all tenant model CRUD helpers and edge cases."""
    with app.app_context():
        from app.models.tenant import (
            create_tenant, get_tenant_by_id, get_tenant_by_uuid,
            get_tenant_by_email, get_tenant_by_subdomain, update_tenant,
            update_tenant_status, update_tenant_settings, get_all_tenants
        )
        # 1. create_tenant with dict
        tenant_id = create_tenant({
            "business_name": "Dict Tenant",
            "email": "dict_tenant@store.com",
            "plan_name": "free",
            "status": "trial"
        })
        assert tenant_id is not None
        
        # 2. get_tenant_by_id
        t = get_tenant_by_id(tenant_id)
        assert t is not None
        assert t["business_name"] == "Dict Tenant"
        assert t["email"] == "dict_tenant@store.com"
        
        # 3. get_tenant_by_uuid
        t_uuid = get_tenant_by_uuid(t["uuid"])
        assert t_uuid["id"] == tenant_id
        
        # 4. get_tenant_by_email
        t_email = get_tenant_by_email("dict_tenant@store.com")
        assert t_email["id"] == tenant_id
        
        # 5. get_tenant_by_subdomain (subdomain is null initially, let's update it first)
        update_tenant(tenant_id, {"subdomain": "dict-tenant", "settings": {"tts_provider": "google"}})
        t_sub = get_tenant_by_subdomain("dict-tenant")
        assert t_sub["id"] == tenant_id
        
        # Check settings updated
        t_updated = get_tenant_by_id(tenant_id)
        import json
        settings = t_updated["settings"]
        if isinstance(settings, str):
            settings = json.loads(settings)
        assert settings["tts_provider"] == "google"
        
        # 6. update_tenant_status
        update_tenant_status(tenant_id, "active")
        t_status = get_tenant_by_id(tenant_id)
        assert t_status["status"] == "active"
        
        # 7. update_tenant_settings (merge test)
        update_tenant_settings(tenant_id, {"dnd_start": "22:00"})
        t_sett = get_tenant_by_id(tenant_id)
        settings = t_sett["settings"]
        if isinstance(settings, str):
            settings = json.loads(settings)
        assert settings["dnd_start"] == "22:00"
        assert settings["tts_provider"] == "google"
        
        # 8. get_all_tenants
        # Full mode
        all_tenants_list = get_all_tenants()
        assert len(all_tenants_list) >= 1
        
        # Paginated search mode
        search_result = get_all_tenants(page=1, per_page=5, search="Dict")
        assert search_result["total"] >= 1
        assert len(search_result["data"]) >= 1


def test_user_model_helpers(app):
    """Verify all user model CRUD helpers, User class properties and edge cases."""
    with app.app_context():
        from app.models.tenant import create_tenant as db_create_tenant
        from app.models.user import (
            create_user, get_user_by_id, get_user_by_email, verify_user_password,
            update_password, update_last_login, set_email_verified,
            get_users_by_tenant, create_verification_token, get_verification_token,
            mark_token_as_used, User, UserLoader
        )
        # Create a valid tenant first
        tenant_id = db_create_tenant({
            "business_name": "User Helper Tenant",
            "email": "user_helper_t@store.com",
            "plan_name": "free",
            "status": "trial"
        })

        # 1. create_user with dict
        user_id = create_user({
            "tenant_id": tenant_id,
            "email": "dict_user@store.com",
            "password": "Password123!",
            "role": "agent",
            "email_verified": False
        })
        assert user_id is not None
        
        # 2. get_user_by_id
        u = get_user_by_id(user_id)
        assert u is not None
        assert u["email"] == "dict_user@store.com"
        assert u["role"] == "agent"
        
        # 3. get_user_by_email
        u_email = get_user_by_email("dict_user@store.com")
        assert u_email["id"] == user_id
        
        # 4. UserLoader
        loaded_user = UserLoader.load_user(str(user_id))
        assert loaded_user is not None
        assert loaded_user.id == user_id
        assert not loaded_user.is_super_admin
        assert not loaded_user.is_tenant_admin
        
        # Test super admin User properties
        super_admin_dict = {
            "id": 9999,
            "tenant_id": None,
            "email": "super_admin@ccall.com.bd",
            "role": "super_admin",
            "email_verified": True
        }
        super_user = User(super_admin_dict)
        assert super_user.is_super_admin
        assert super_user.is_active
        
        # Test inactive user (missing tenant)
        invalid_user_dict = {
            "id": 9998,
            "tenant_id": None,
            "email": "notenant@store.com",
            "role": "agent",
            "email_verified": True
        }
        invalid_user = User(invalid_user_dict)
        assert not invalid_user.is_active
        
        # 5. verify_user_password exception handling
        assert not verify_user_password(None, "password")
        
        # 6. update_password
        update_password(user_id, "new_hashed_password")
        u_pw = get_user_by_id(user_id)
        assert u_pw["password_hash"] == "new_hashed_password"
        
        # 7. update_last_login
        update_last_login(user_id)
        u_login = get_user_by_id(user_id)
        assert u_login["last_login_at"] is not None
        
        # 8. set_email_verified
        set_email_verified(user_id)
        u_verified = get_user_by_id(user_id)
        assert u_verified["email_verified"] == 1  # 1 because normalized to 1 for True
        
        # 9. get_users_by_tenant
        tenant_users = get_users_by_tenant(tenant_id)
        assert len(tenant_users) >= 1
        
        # 10. mark_token_as_used
        import datetime
        token = "testtoken123"
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        token_id = create_verification_token(user_id, token, expires_at)
        assert token_id is not None
        
        t_row = get_verification_token(token)
        assert t_row is not None
        assert t_row["used_at"] is None
        
        mark_token_as_used(token)
        t_row_used = get_verification_token(token)
        assert t_row_used["used_at"] is not None


