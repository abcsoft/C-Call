from __future__ import annotations

import json
import hashlib
from decimal import Decimal
from unittest.mock import patch
import pytest
from app.db import db
from app.services import order_service
from app.services.order_service import update_status_from_call

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

def _setup_providers_and_cli():
    # Setup shared phone number CLI for round-robin CLI select
    provider_id = db.insert_returning_id(
        """
        INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password)
        VALUES ('Provider A', 'sip.domain.com', 5060, 'user', 'pass')
        RETURNING id
        """
    )
    db.execute(
        """
        INSERT INTO phone_numbers (number, provider_id, number_type, status, monthly_cost)
        VALUES ('01711112222', %s, 'shared', 'active', 0.0)
        """,
        (provider_id,)
    )

def test_create_order_queues_call_and_returns_order_id(client, app, create_tenant):
    """Test creating an order queues the call and returns the order ID successfully."""
    tenant, user = create_tenant("A-Store", "a@store.com")
    api_key = _create_api_key(tenant["id"], key_str="cc_live_storekey123")
    
    with app.app_context():
        _setup_providers_and_cli()
        db.execute("UPDATE wallets SET balance = 500 WHERE tenant_id = %s", (tenant["id"],))

    def mock_start(phone, flow_id, name="", *args, **kwargs):
        with app.app_context():
            return db.insert_returning_id(
                "INSERT INTO calls (number, status, direction, talk_seconds) VALUES (%s, 'calling', 'outbound', 0) RETURNING id",
                (phone,)
            )

    with patch("call_engine.call_runner.start_call", side_effect=mock_start) as mock_start_call:
        payload = {
            "order_id": "ORD-101",
            "phone": "01712345678",
            "customer_name": "Rahim",
            "amount": 2500.0
        }

        response = client.post(
            "/api/v1/orders",
            headers={"X-API-Key": api_key},
            json=payload
        )
        assert response.status_code == 200
        data = response.json
        assert data["success"] is True
        assert data["order_id"] == "ORD-101"
        assert data["status"] in ("CALL_QUEUED", "CALLING")
        
        # Wait/assert background thread triggered queue_confirmation_call
        import time
        for _ in range(20):
            if mock_start_call.call_count > 0:
                break
            time.sleep(0.05)
            
        assert mock_start_call.call_count == 1

def test_duplicate_order_id_returns_existing_not_error(client, app, create_tenant):
    """Test that submitting a duplicate external order ID returns the existing order."""
    tenant, user = create_tenant("B-Store", "b@store.com")
    api_key = _create_api_key(tenant["id"], key_str="cc_live_duplicatekey")

    with app.app_context():
        _setup_providers_and_cli()
        db.execute("UPDATE wallets SET balance = 500 WHERE tenant_id = %s", (tenant["id"],))

    def mock_start(phone, flow_id, name="", *args, **kwargs):
        with app.app_context():
            return db.insert_returning_id(
                "INSERT INTO calls (number, status, direction, talk_seconds) VALUES (%s, 'calling', 'outbound', 0) RETURNING id",
                (phone,)
            )

    with patch("call_engine.call_runner.start_call", side_effect=mock_start):
        payload = {
            "order_id": "ORD-102",
            "phone": "01712345678",
            "customer_name": "Karim",
            "amount": 150.0
        }

        # First call
        resp1 = client.post("/api/v1/orders", headers={"X-API-Key": api_key}, json=payload)
        assert resp1.status_code == 200
        id1 = resp1.json["internal_id"]

        # Second call
        resp2 = client.post("/api/v1/orders", headers={"X-API-Key": api_key}, json=payload)
        assert resp2.status_code == 200
        id2 = resp2.json["internal_id"]
        
        assert id1 == id2
        assert "already exists" in resp2.json["message"] or "queued successfully" in resp2.json["message"]

def test_invalid_phone_returns_400(client, app, create_tenant):
    """Test validation errors for invalid phone numbers (non-Bangladesh format)."""
    tenant, user = create_tenant("C-Store", "c@store.com")
    api_key = _create_api_key(tenant["id"], key_str="cc_live_invalidphone")

    with app.app_context():
        db.execute("UPDATE wallets SET balance = 500 WHERE tenant_id = %s", (tenant["id"],))

    payload = {
        "order_id": "ORD-103",
        "phone": "invalid_phone_123",
        "customer_name": "Karim",
        "amount": 150.0
    }
    response = client.post("/api/v1/orders", headers={"X-API-Key": api_key}, json=payload)
    assert response.status_code == 400
    assert b"Invalid Bangladesh mobile number format" in response.data

def test_insufficient_balance_returns_402(client, app, create_tenant):
    """Test order creation fails with 402 if tenant balance is less than min_charge."""
    tenant, user = create_tenant("D-Store", "d@store.com")
    api_key = _create_api_key(tenant["id"], key_str="cc_live_lowbal")

    with app.app_context():
        # Set balance to 0 (default rate min_charge is 0.80)
        db.execute("UPDATE wallets SET balance = 0.00 WHERE tenant_id = %s", (tenant["id"],))

    payload = {
        "order_id": "ORD-104",
        "phone": "01712345678",
        "customer_name": "Karim",
        "amount": 150.0
    }
    response = client.post("/api/v1/orders", headers={"X-API-Key": api_key}, json=payload)
    assert response.status_code == 402
    assert b"INSUFFICIENT_BALANCE" in response.data

def test_suspended_tenant_returns_403(client, app, create_tenant):
    """Test that suspended tenants are rejected when creating orders."""
    tenant, user = create_tenant("Suspended Store", "suspended@store.com")
    api_key = _create_api_key(tenant["id"], key_str="cc_live_suspended")

    with app.app_context():
        db.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (tenant["id"],))

    payload = {
        "order_id": "ORD-105",
        "phone": "01712345678",
        "customer_name": "Suspended",
        "amount": 150.0
    }
    response = client.post("/api/v1/orders", headers={"X-API-Key": api_key}, json=payload)
    assert response.status_code == 403
    assert b"suspended or inactive" in response.data

def test_dtmf_1_updates_order_to_confirmed(app, create_tenant):
    """Test that receiving DTMF digit 1 confirms the order."""
    tenant, user = create_tenant("E-Store", "e@store.com")

    with app.app_context():
        with patch("app.services.webhook_service.deliver_async"): # Avoid triggering async webhooks during testing
            order_id = db.insert_returning_id(
                "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'CALLING') RETURNING id",
                (tenant["id"], "ORD-CONF-1", "01712345678", "Sakib", Decimal("100.00"))
            )
            call_id = db.insert_returning_id(
                "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (tenant["id"], order_id, "01712345678", "completed", "outbound", 10)
            )
            db.execute(
                "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no) VALUES (%s, %s, %s, 1)",
                (tenant["id"], order_id, call_id)
            )

            update_status_from_call(
                order_id=order_id,
                tenant_id=tenant["id"],
                call_result="completed",
                dtmf_digit="1",
                talk_seconds=10,
                call_id=call_id
            )

            # Assert status
            order = db.query_one("SELECT order_status, confirmed_at FROM orders WHERE id = %s", (order_id,))
            assert order["order_status"] == "CONFIRMED"
            assert order["confirmed_at"] is not None

def test_dtmf_2_updates_order_to_cancelled(app, create_tenant):
    """Test that receiving DTMF digit 2 cancels the order."""
    tenant, user = create_tenant("F-Store", "f@store.com")

    with app.app_context():
        with patch("app.services.webhook_service.deliver_async"):
            order_id = db.insert_returning_id(
                "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'CALLING') RETURNING id",
                (tenant["id"], "ORD-CONF-2", "01712345678", "Sakib", Decimal("100.00"))
            )
            call_id = db.insert_returning_id(
                "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (tenant["id"], order_id, "01712345678", "completed", "outbound", 10)
            )
            db.execute(
                "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no) VALUES (%s, %s, %s, 1)",
                (tenant["id"], order_id, call_id)
            )

            update_status_from_call(
                order_id=order_id,
                tenant_id=tenant["id"],
                call_result="completed",
                dtmf_digit="2",
                talk_seconds=10,
                call_id=call_id
            )

            order = db.query_one("SELECT order_status FROM orders WHERE id = %s", (order_id,))
            assert order["order_status"] == "CANCELLED"

def test_no_answer_schedules_retry(app, create_tenant):
    """Test that call timeouts / no-answer results schedule retry executions."""
    tenant, user = create_tenant("G-Store", "g@store.com")

    with app.app_context():
        with patch("app.services.webhook_service.deliver_async"):
            order_id = db.insert_returning_id(
                "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, attempt_count) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (tenant["id"], "ORD-RETRY-1", "01712345678", "Sakib", Decimal("100.00"), "CALLING", 1)
            )
            call_id = db.insert_returning_id(
                "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (tenant["id"], order_id, "01712345678", "no_answer", "outbound", 0)
            )
            db.execute(
                "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no) VALUES (%s, %s, %s, 1)",
                (tenant["id"], order_id, call_id)
            )

            update_status_from_call(
                order_id=order_id,
                tenant_id=tenant["id"],
                call_result="no_answer",
                dtmf_digit=None,
                talk_seconds=0,
                call_id=call_id
            )

            order = db.query_one("SELECT order_status, next_retry_at FROM orders WHERE id = %s", (order_id,))
            assert order["order_status"] == "RETRY_SCHEDULED"
            assert order["next_retry_at"] is not None

def test_max_retries_sets_unreachable(app, create_tenant):
    """Test that exceeding the max attempt limits marks the order UNREACHABLE."""
    tenant, user = create_tenant("H-Store", "h@store.com")

    with app.app_context():
        with patch("app.services.webhook_service.deliver_async"), \
             patch("app.services.email_service.send_order_unreachable_alert"):
            order_id = db.insert_returning_id(
                "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, attempt_count, max_attempts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (tenant["id"], "ORD-MAX-1", "01712345678", "Sakib", Decimal("100.00"), "CALLING", 3, 3)
            )
            call_id = db.insert_returning_id(
                "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (tenant["id"], order_id, "01712345678", "no_answer", "outbound", 0)
            )
            db.execute(
                "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no) VALUES (%s, %s, %s, 3)",
                (tenant["id"], order_id, call_id)
            )

            update_status_from_call(
                order_id=order_id,
                tenant_id=tenant["id"],
                call_result="no_answer",
                dtmf_digit=None,
                talk_seconds=0,
                call_id=call_id
            )

            order = db.query_one("SELECT order_status, next_retry_at FROM orders WHERE id = %s", (order_id,))
            assert order["order_status"] == "UNREACHABLE"
            assert order["next_retry_at"] is None

def test_manual_recall_works_for_eligible_statuses(client, app, create_tenant):
    """Test that manually triggering a recall queues a call for eligible statuses."""
    tenant, user = create_tenant("I-Store", "i@store.com")
    api_key = _create_api_key(tenant["id"], key_str="cc_live_recallflow")

    with app.app_context():
        _setup_providers_and_cli()
        db.execute("UPDATE wallets SET balance = 500 WHERE tenant_id = %s", (tenant["id"],))
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'FAILED') RETURNING id",
            (tenant["id"], "ORD-RC-1", "01712345678", "Sakib", Decimal("100.00"))
        )

    def mock_start(phone, flow_id, name="", *args, **kwargs):
        with app.app_context():
            return db.insert_returning_id(
                "INSERT INTO calls (number, status, direction, talk_seconds) VALUES (%s, 'calling', 'outbound', 0) RETURNING id",
                (phone,)
            )

    with patch("call_engine.call_runner.start_call", side_effect=mock_start) as mock_start_call:
        response = client.post(
            "/api/v1/orders/ORD-RC-1/recall",
            headers={"X-API-Key": api_key}
        )
        assert response.status_code == 200
        assert response.json["success"] is True
        assert response.json["call_id"] > 0

    with app.app_context():
        order = db.query_one("SELECT order_status, attempt_count FROM orders WHERE id = %s", (order_id,))
        assert order["order_status"] == "CALLING"
        assert order["attempt_count"] == 1

def test_cross_tenant_order_access_denied(client, app, create_tenant):
    """Test that a tenant cannot access or trigger actions on another tenant's order."""
    tenant_a, user_a = create_tenant("Tenant A Store", "ta@store.com")
    tenant_b, user_b = create_tenant("Tenant B Store", "tb@store.com")
    api_key_b = _create_api_key(tenant_b["id"], key_str="cc_live_tenantb")

    with app.app_context():
        order_a_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'FAILED') RETURNING id",
            (tenant_a["id"], "ORD-A-1", "01712345678", "Alice", Decimal("100.00"))
        )

    # Tenant B tries to get Tenant A's order details
    response_get = client.get(
        "/api/v1/orders/ORD-A-1",
        headers={"X-API-Key": api_key_b}
    )
    assert response_get.status_code == 404

    # Tenant B tries to recall Tenant A's order
    response_recall = client.post(
        "/api/v1/orders/ORD-A-1/recall",
        headers={"X-API-Key": api_key_b}
    )
    assert response_recall.status_code == 404
