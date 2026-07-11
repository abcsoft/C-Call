from __future__ import annotations

import json
import hashlib
import datetime
from decimal import Decimal
import pytest
from app.db import db
from app.models.tenant import create_tenant

def _create_api_key(tenant_id, name="Test Key", key_str="cc_live_testkey12345"):
    key_hash = hashlib.sha256(key_str.encode("utf-8")).hexdigest()
    key_prefix = key_str[:12]
    db.execute(
        """
        INSERT INTO api_keys (tenant_id, name, key_hash, key_prefix, is_active)
        VALUES (%s, %s, %s, %s, true)
        """,
        (tenant_id, name, key_hash, key_prefix)
    )
    return key_str

def test_valid_api_key_grants_access(client, app, create_tenant):
    """Test that a valid API key in X-API-Key header authorizes the request."""
    tenant, user = create_tenant("Auth Tenant 1", "auth1@store.com")
    api_key = _create_api_key(tenant["id"], key_str="cc_live_validgrants")

    response = client.get(
        "/api/v1/orders",
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200
    assert response.json["success"] is True

def test_invalid_api_key_returns_401(client, app, create_tenant):
    """Test that an invalid API key yields 401 Unauthorized."""
    tenant, user = create_tenant("Auth Tenant 2", "auth2@store.com")
    _create_api_key(tenant["id"], key_str="cc_live_validgrants")

    response = client.get(
        "/api/v1/orders",
        headers={"X-API-Key": "cc_live_invalidkeyprefix"}
    )
    assert response.status_code == 401
    assert b"UNAUTHORIZED" in response.data

def test_revoked_api_key_returns_401(client, app, create_tenant):
    """Test that a revoked (inactive) API key results in 401 Unauthorized."""
    tenant, user = create_tenant("Auth Tenant 3", "auth3@store.com")
    api_key = _create_api_key(tenant["id"], key_str="cc_live_revokedkey")

    with app.app_context():
        # Deactivate the key
        db.execute("UPDATE api_keys SET is_active = false WHERE tenant_id = %s", (tenant["id"],))

    response = client.get(
        "/api/v1/orders",
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 401
    assert b"UNAUTHORIZED" in response.data

def test_api_key_of_other_tenant_denied(client, app, create_tenant):
    """Test that an API key belonging to another tenant does not authorize accessing private tenant boundaries."""
    tenant_a, user_a = create_tenant("Auth Tenant A", "autha@store.com")
    tenant_b, user_b = create_tenant("Auth Tenant B", "authb@store.com")
    api_key_b = _create_api_key(tenant_b["id"], key_str="cc_live_tenantbkey")

    with app.app_context():
        order_a_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'FAILED') RETURNING id",
            (tenant_a["id"], "ORD-A-2", "01712345678", "Alice", Decimal("100.00"))
        )

    # Try to access Tenant A's order using Tenant B's API key
    response = client.get(
        "/api/v1/orders/ORD-A-2",
        headers={"X-API-Key": api_key_b}
    )
    assert response.status_code == 404

def test_api_key_last_used_at_updated_on_use(client, app, create_tenant):
    """Test that last_used_at timestamp updates in DB upon successful authentication."""
    tenant, user = create_tenant("Auth Tenant 4", "auth4@store.com")
    api_key = _create_api_key(tenant["id"], key_str="cc_live_lastusedat")

    with app.app_context():
        key = db.query_one("SELECT last_used_at FROM api_keys WHERE tenant_id = %s", (tenant["id"],))
        assert key["last_used_at"] is None

    response = client.get(
        "/api/v1/orders",
        headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200

    with app.app_context():
        key = db.query_one("SELECT last_used_at FROM api_keys WHERE tenant_id = %s", (tenant["id"],))
        assert key["last_used_at"] is not None
        # Verify it is close to current time
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = now - key["last_used_at"].replace(tzinfo=datetime.timezone.utc)
        assert diff.total_seconds() < 10
