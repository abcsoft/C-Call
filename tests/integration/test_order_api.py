import hashlib
import json
from app.db import db
from app.models.tenant import create_tenant
from app.models.wallet import credit_balance

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

def test_api_key_verification(client, app):
    with app.app_context():
        tenant_id = create_tenant("API Tenant", "api@tenant.com")
        api_key = _create_api_key(tenant_id, key_str="cc_live_verifykey123")
    
    # 1. Missing header
    res_missing = client.post("/api/v1/orders", json={})
    assert res_missing.status_code == 401
    assert b"API_KEY_MISSING" in res_missing.data
    
    # 2. Invalid API key
    res_invalid = client.post("/api/v1/orders", headers={"X-API-Key": "invalid_key"}, json={})
    assert res_invalid.status_code == 401
    assert b"UNAUTHORIZED" in res_invalid.data

def test_order_creation_insufficient_balance(client, app):
    with app.app_context():
        tenant_id = create_tenant("Insufficient Tenant", "insufficient@tenant.com")
        api_key = _create_api_key(tenant_id, key_str="cc_live_insufficient123")
        
    payload = {
        "order_id": "WC-100",
        "phone": "01700000000",
        "customer_name": "Rahim",
        "amount": 1000.0
    }
    
    res = client.post(
        "/api/v1/orders",
        headers={"X-API-Key": api_key},
        json=payload
    )
    # Balance is 0, so should return 402 Insufficient Balance
    assert res.status_code == 402
    assert b"INSUFFICIENT_BALANCE" in res.data

def test_order_creation_success_and_idempotency(client, app):
    from decimal import Decimal
    with app.app_context():
        tenant_id = create_tenant("Success Tenant", "success@tenant.com")
        api_key = _create_api_key(tenant_id, key_str="cc_live_successkey123")
        credit_balance(tenant_id, Decimal("100.0"), "Deposit")
        # Add a provider first
        provider_id = db.insert_returning_id(
            """
            INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password)
            VALUES ('Mock Provider', 'sip.mock.com', 5060, 'user', 'pass')
            RETURNING id
            """
        )
        # Add a phone number CLI so order dispatch does not fail
        db.execute(
            """
            INSERT INTO phone_numbers (number, provider_id, number_type, status, monthly_cost)
            VALUES ('01799999999', %s, 'shared', 'active', 0.0)
            """,
            (provider_id,)
        )
        
    payload = {
        "order_id": "WC-200",
        "phone": "01700000000",
        "customer_name": "Rahim",
        "amount": 1000.0
    }
    
    # First submit (should succeed)
    res1 = client.post(
        "/api/v1/orders",
        headers={"X-API-Key": api_key},
        json=payload
    )
    assert res1.status_code == 200
    data1 = json.loads(res1.data)
    assert data1["success"] is True
    assert data1["order_id"] == "WC-200"
    internal_id = data1["internal_id"]
    
    # Second submit with same order_id (should return same order - idempotency)
    res2 = client.post(
        "/api/v1/orders",
        headers={"X-API-Key": api_key},
        json=payload
    )
    assert res2.status_code == 200
    data2 = json.loads(res2.data)
    assert data2["success"] is True
    assert data2["internal_id"] == internal_id
    assert "queued successfully" in data2["message"]

def test_cross_tenant_isolation(client, app):
    from decimal import Decimal
    # Setup Tenant A and Tenant B
    with app.app_context():
        tenant_a_id = create_tenant("Tenant A", "t_a@tenant.com")
        api_key_a = _create_api_key(tenant_a_id, key_str="cc_live_tenant_a_key")
        credit_balance(tenant_a_id, Decimal("100.0"), "Deposit")
        
        tenant_b_id = create_tenant("Tenant B", "t_b@tenant.com")
        api_key_b = _create_api_key(tenant_b_id, key_str="cc_live_tenant_b_key")
        credit_balance(tenant_b_id, Decimal("100.0"), "Deposit")

        # Add a provider first
        provider_id = db.insert_returning_id(
            """
            INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password)
            VALUES ('Mock Provider', 'sip.mock.com', 5060, 'user', 'pass')
            RETURNING id
            """
        )
        # Add a phone number CLI so order dispatch does not fail
        db.execute(
            """
            INSERT INTO phone_numbers (number, provider_id, number_type, status, monthly_cost)
            VALUES ('01799999999', %s, 'shared', 'active', 0.0)
            """,
            (provider_id,)
        )

    # Create order in Tenant A
    payload_a = {
        "order_id": "ORDER-A",
        "phone": "01711111111",
        "customer_name": "Tenant A Customer",
        "amount": 500.0
    }
    res_create = client.post(
        "/api/v1/orders",
        headers={"X-API-Key": api_key_a},
        json=payload_a
    )
    assert res_create.status_code == 200
    order_a_internal_id = json.loads(res_create.data)["internal_id"]

    # 1. Tenant B tries to retrieve Tenant A's order details -> should return 404 (Not Found) for isolation
    res_get = client.get(
        f"/api/v1/orders/{order_a_internal_id}",
        headers={"X-API-Key": api_key_b}
    )
    assert res_get.status_code == 404
    
    # 2. Tenant B tries to trigger manual recall on Tenant A's order -> should return 404
    res_recall = client.post(
        f"/api/v1/orders/{order_a_internal_id}/recall",
        headers={"X-API-Key": api_key_b}
    )
    assert res_recall.status_code == 404

def test_api_get_and_list_orders(client, app):
    from decimal import Decimal
    with app.app_context():
        tenant_id = create_tenant("Query Tenant", "query@tenant.com")
        api_key = _create_api_key(tenant_id, key_str="cc_live_querykey123")
        credit_balance(tenant_id, Decimal("200.0"), "Deposit")
        
        provider_id = db.insert_returning_id(
            """
            INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password)
            VALUES ('Query Provider', 'sip.query.com', 5060, 'user', 'pass')
            RETURNING id
            """
        )
        db.execute(
            """
            INSERT INTO phone_numbers (number, provider_id, number_type, status, monthly_cost)
            VALUES ('01788888888', %s, 'shared', 'active', 0.0)
            """,
            (provider_id,)
        )
        
        # Create an order
        payload = {
            "order_id": "WC-QUERY-1",
            "phone": "01700000000",
            "customer_name": "Query Customer",
            "amount": 250.0
        }
        res_create = client.post(
            "/api/v1/orders",
            headers={"X-API-Key": api_key},
            json=payload
        )
        assert res_create.status_code == 200
        order_data = json.loads(res_create.data)
        internal_id = order_data["internal_id"]
        
    # Get by external_id
    res_get_ext = client.get(
        "/api/v1/orders/WC-QUERY-1",
        headers={"X-API-Key": api_key}
    )
    assert res_get_ext.status_code == 200
    data_ext = json.loads(res_get_ext.data)
    assert data_ext["success"] is True
    assert data_ext["data"]["external_order_id"] == "WC-QUERY-1"
    assert data_ext["data"]["id"] == internal_id
    
    # Get by internal_id (numeric fallback)
    res_get_int = client.get(
        f"/api/v1/orders/{internal_id}",
        headers={"X-API-Key": api_key}
    )
    assert res_get_int.status_code == 200
    data_int = json.loads(res_get_int.data)
    assert data_int["success"] is True
    assert data_int["data"]["external_order_id"] == "WC-QUERY-1"

    # Get non-existent
    res_get_miss = client.get(
        "/api/v1/orders/MISSING-ORDER",
        headers={"X-API-Key": api_key}
    )
    assert res_get_miss.status_code == 404

    # List orders
    res_list = client.get(
        "/api/v1/orders?page=1&per_page=10",
        headers={"X-API-Key": api_key}
    )
    assert res_list.status_code == 200
    list_data = json.loads(res_list.data)
    assert list_data["success"] is True
    assert list_data["total"] == 1
    assert list_data["data"][0]["external_order_id"] == "WC-QUERY-1"

    # List orders with filtering — durable queue: order stays CALL_QUEUED until worker dispatches
    res_list_filt = client.get(
        "/api/v1/orders?status=CALL_QUEUED",
        headers={"X-API-Key": api_key}
    )
    assert res_list_filt.status_code == 200
    assert json.loads(res_list_filt.data)["total"] == 1

    # List orders with non-matching filter
    res_list_none = client.get(
        "/api/v1/orders?status=CONFIRMED",
        headers={"X-API-Key": api_key}
    )
    assert res_list_none.status_code == 200
    assert json.loads(res_list_none.data)["total"] == 0

def test_api_recall_scenarios(client, app):
    from decimal import Decimal
    with app.app_context():
        tenant_id = create_tenant("Recall Tenant", "recall_api@tenant.com")
        api_key = _create_api_key(tenant_id, key_str="cc_live_recallkey123")
        credit_balance(tenant_id, Decimal("50.0"), "Deposit")
        
        provider_id = db.insert_returning_id(
            """
            INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password)
            VALUES ('Recall Provider', 'sip.recall.com', 5060, 'user', 'pass')
            RETURNING id
            """
        )
        db.execute(
            """
            INSERT INTO phone_numbers (number, provider_id, number_type, status, monthly_cost)
            VALUES ('01777777777', %s, 'shared', 'active', 0.0)
            """,
            (provider_id,)
        )
        
        # Create an order
        payload = {
            "order_id": "WC-RECALL-1",
            "phone": "01700000000",
            "customer_name": "Recall Customer",
            "amount": 100.0
        }
        res_create = client.post(
            "/api/v1/orders",
            headers={"X-API-Key": api_key},
            json=payload
        )
        assert res_create.status_code == 200
        order_data = json.loads(res_create.data)
        internal_id = order_data["internal_id"]

    # Recall on non-terminal status (CALLING or CALL_QUEUED) should fail
    res_recall_fail = client.post(
        f"/api/v1/orders/WC-RECALL-1/recall",
        headers={"X-API-Key": api_key}
    )
    assert res_recall_fail.status_code == 404  # Since order_service manual_recall raises ValueError which is handled as 404 in API
    
    # Update status to FAILED in DB
    with app.app_context():
        db.execute("UPDATE orders SET order_status = 'FAILED' WHERE id = %s", (internal_id,))
        
    # Recall should succeed now
    res_recall_ok = client.post(
        f"/api/v1/orders/WC-RECALL-1/recall",
        headers={"X-API-Key": api_key}
    )
    assert res_recall_ok.status_code == 200
    assert json.loads(res_recall_ok.data)["success"] is True

    # Test Recall insufficient balance
    with app.app_context():
        db.execute("UPDATE wallets SET balance = 0 WHERE tenant_id = %s", (tenant_id,))
        db.execute("UPDATE orders SET order_status = 'FAILED' WHERE id = %s", (internal_id,))
        
    res_recall_low_bal = client.post(
        f"/api/v1/orders/WC-RECALL-1/recall",
        headers={"X-API-Key": api_key}
    )
    assert res_recall_low_bal.status_code == 402
    assert b"INSUFFICIENT_BALANCE" in res_recall_low_bal.data

def test_api_validation_errors(client, app):
    with app.app_context():
        tenant_id = create_tenant("Valid Tenant", "valid@tenant.com")
        api_key = _create_api_key(tenant_id, key_str="cc_live_valid123")
        
    # Missing phone number
    payload = {
        "order_id": "WC-VALID-1",
        "customer_name": "No Phone",
        "amount": 100.0
    }
    res = client.post(
        "/api/v1/orders",
        headers={"X-API-Key": api_key},
        json=payload
    )
    assert res.status_code == 400
    assert b"phone number is required" in res.data

