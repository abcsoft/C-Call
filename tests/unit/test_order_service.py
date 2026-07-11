from __future__ import annotations

import datetime
from decimal import Decimal
import pytest
from unittest.mock import MagicMock
from app.db import db
from app.models.tenant import create_tenant
from app.models.wallet import credit_balance
from app.exceptions import InsufficientBalanceError
from app.services.order_service import (
    create_order,
    queue_confirmation_call,
    update_status_from_call,
    get_order,
    manual_recall,
    parse_template
)
from app.models.order import list_orders
from call_engine import call_runner

@pytest.fixture
def test_setup(app):
    """Setup a tenant, provider, phone number CLI, and credit balance."""
    with app.app_context():
        # Create tenant
        tenant_id = create_tenant({
            "business_name": "Order Service Store",
            "email": "service@store.com",
            "plan_name": "free",
            "status": "active"
        })
        
        # Credit balance
        credit_balance(tenant_id, Decimal("100.00"), "Initial Deposit")
        
        # Create provider
        provider_id = db.insert_returning_id(
            """
            INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password)
            VALUES ('Service Provider', 'sip.service.com', 5060, 'user', 'pass')
            RETURNING id
            """
        )
        
        # Create shared phone CLI
        db.execute(
            """
            INSERT INTO phone_numbers (number, provider_id, number_type, status, monthly_cost)
            VALUES ('01799999999', %s, 'shared', 'active', 0.0)
            """,
            (provider_id,)
        )
        
        return tenant_id

def test_create_order_validation_failures(app, test_setup):
    """Test all input validation paths in create_order."""
    tenant_id = test_setup
    with app.app_context():
        # Missing order_id
        with pytest.raises(ValueError) as exc:
            create_order(tenant_id, {"phone": "01700000000"})
        assert "order_id is required" in str(exc.value)
        
        # Missing phone
        with pytest.raises(ValueError) as exc:
            create_order(tenant_id, {"order_id": "WC-1"})
        assert "phone number is required" in str(exc.value)
        
        # Invalid phone format
        with pytest.raises(ValueError) as exc:
            create_order(tenant_id, {"order_id": "WC-1", "phone": "12345"})
        assert "Invalid Bangladesh mobile number format" in str(exc.value)
        
        # Invalid amount format
        with pytest.raises(ValueError) as exc:
            create_order(tenant_id, {"order_id": "WC-1", "phone": "01700000000", "amount": "abc"})
        assert "Amount must be a valid numeric value" in str(exc.value)
        
        # Negative amount
        with pytest.raises(ValueError) as exc:
            create_order(tenant_id, {"order_id": "WC-1", "phone": "01700000000", "amount": -10.0})
        assert "Amount cannot be negative" in str(exc.value)

def test_create_order_inactive_tenant(app, test_setup):
    """Test create_order fails when the tenant is suspended or inactive."""
    tenant_id = test_setup
    with app.app_context():
        db.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (tenant_id,))
        with pytest.raises(ValueError) as exc:
            create_order(tenant_id, {"order_id": "WC-1", "phone": "01700000000", "amount": 100})
        assert "Tenant is inactive" in str(exc.value)

def test_create_order_insufficient_balance(app, test_setup):
    """Test create_order fails when the tenant has insufficient balance."""
    tenant_id = test_setup
    with app.app_context():
        # Set balance to 0
        db.execute("UPDATE wallets SET balance = 0 WHERE tenant_id = %s", (tenant_id,))
        with pytest.raises(InsufficientBalanceError):
            create_order(tenant_id, {"order_id": "WC-1", "phone": "01700000000", "amount": 100})

def test_create_order_success_and_idempotency(app, test_setup, monkeypatch):
    """Test successful order creation, E.164 normalization, webhook snapshotting, and idempotency."""
    tenant_id = test_setup
    
    # Mock call runner to avoid actual SIP processes
    monkeypatch.setattr(call_runner, "start_call", lambda number, flow_id, name="": 42)
    # Prevent async thread from executing to avoid transaction corruption
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    
    with app.app_context():
        # Set tenant webhook setting
        db.execute(
            "UPDATE tenants SET settings = '{\"webhook_url\": \"https://tenant.com/hook\"}' WHERE id = %s",
            (tenant_id,)
        )
        
        data = {
            "order_id": "ORDER-999",
            "phone": "01812345678", # Local format
            "customer_name": "Sakib",
            "amount": "1500.50"
        }
        
        # 1. Success creation
        order = create_order(tenant_id, data)
        assert order is not None
        assert order["external_order_id"] == "ORDER-999"
        assert order["phone"] == "+8801812345678"  # Normalized to E.164
        assert order["webhook_url"] == "https://tenant.com/hook"  # Snapshotted
        assert order["order_status"] == "CALL_QUEUED"
        
        # 2. Idempotency Check (same order ID returns the existing order)
        order2 = create_order(tenant_id, data)
        assert order2["id"] == order["id"]

def test_queue_confirmation_call_failures(app, test_setup, monkeypatch):
    """Test queue_confirmation_call when caller ID or SIP fails."""
    tenant_id = test_setup
    with app.app_context():
        # Save order
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount)
            VALUES (%s, 'WC-ERR', '+8801700000000', 'Customer', 100.0)
            RETURNING id
            """,
            (tenant_id,)
        )
        db.execute("UPDATE orders SET attempt_count = 1 WHERE id = %s", (order_id,))
        
        # Test invalid IDs
        with pytest.raises(ValueError):
            queue_confirmation_call(999999, tenant_id)
        with pytest.raises(ValueError):
            queue_confirmation_call(order_id, 999999)
            
        # Test missing caller ID CLI (delete all CLIs)
        db.execute("DELETE FROM phone_numbers")
        call_id = queue_confirmation_call(order_id, tenant_id)
        assert call_id == 0
        order_db = db.query_one("SELECT order_status FROM orders WHERE id = %s", (order_id,))
        assert order_db["order_status"] == "FAILED"

def test_queue_confirmation_call_exception(app, test_setup, monkeypatch):
    """Test exception recovery when call_runner throws an error."""
    tenant_id = test_setup
    
    # Mock tts generation and call_runner to throw exception
    monkeypatch.setattr("app.services.order_service.generate_order_audio", lambda tid, text: "audio.ulaw")
    def mock_start_call(number, flow_id, name=""):
        raise RuntimeError("SIP Engine Offline")
    monkeypatch.setattr(call_runner, "start_call", mock_start_call)
    
    with app.app_context():
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, attempt_count)
            VALUES (%s, 'WC-FAIL', '+8801700000000', 'Customer', 100.0, 1)
            RETURNING id
            """,
            (tenant_id,)
        )
        
        call_id = queue_confirmation_call(order_id, tenant_id)
        assert call_id == 0
        order_db = db.query_one("SELECT order_status FROM orders WHERE id = %s", (order_id,))
        assert order_db["order_status"] == "FAILED"

def test_update_status_from_call_outcomes(app, test_setup, monkeypatch):
    """Test all outcome mappings, billing calculations, and retries in update_status_from_call."""
    tenant_id = test_setup
    
    # Mock retry_service.schedule_retry and webhook_service.deliver_async
    schedule_mock = MagicMock()
    deliver_mock = MagicMock()
    monkeypatch.setattr("app.services.retry_service.schedule_retry", schedule_mock)
    monkeypatch.setattr("app.services.webhook_service.deliver_async", deliver_mock)
    
    with app.app_context():
        # Helper to create a test order and call attempt
        def setup_order_call(ext_id):
            order_id = db.insert_returning_id(
                "INSERT INTO orders (tenant_id, external_order_id, phone, attempt_count) VALUES (%s, %s, '01700000000', 1) RETURNING id",
                (tenant_id, ext_id)
            )
            call_id = db.insert_returning_id(
                "INSERT INTO calls (number, status, direction, talk_seconds, tenant_id, order_id) VALUES ('01700000000', 'calling', 'outbound', 0, %s, %s) RETURNING id",
                (tenant_id, order_id)
            )
            attempt_id = db.insert_returning_id(
                "INSERT INTO order_call_attempts (order_id, tenant_id, attempt_no, call_id) VALUES (%s, %s, 1, %s) RETURNING id",
                (order_id, tenant_id, call_id)
            )
            return order_id, call_id, attempt_id
            
        # 1. completed call + DTMF 1 -> CONFIRMED + Billing check
        order_id, call_id, attempt_id = setup_order_call("O-1")
        # Set talk time to 90 seconds (2 minutes billing: 2 * 0.80 = 1.60 BDT)
        db.execute("UPDATE calls SET talk_seconds = 90 WHERE id = %s", (call_id,))
        
        update_status_from_call(order_id, tenant_id, "completed", "1", 90, call_id)
        
        # Check order status is CONFIRMED and confirmed_at set
        order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
        assert order["order_status"] == "CONFIRMED"
        assert order["confirmed_at"] is not None
        
        # Check wallet deducted (100.00 - 1.60 = 98.40)
        wallet = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tenant_id,))
        assert Decimal(wallet["balance"]) == Decimal("98.40")
        
        # Check attempt updated
        att = db.query_one("SELECT * FROM order_call_attempts WHERE id = %s", (attempt_id,))
        assert att["result"] == "completed"
        assert att["dtmf_digit"] == "1"
        assert Decimal(att["charge"]) == Decimal("1.60")
        
        # 2. completed call + DTMF 2 -> CANCELLED
        order_id, call_id, attempt_id = setup_order_call("O-2")
        update_status_from_call(order_id, tenant_id, "completed", "2", 30, call_id)
        order = db.query_one("SELECT order_status FROM orders WHERE id = %s", (order_id,))
        assert order["order_status"] == "CANCELLED"
        
        # 3. completed call + invalid/no DTMF -> FAILED
        order_id, call_id, attempt_id = setup_order_call("O-3")
        update_status_from_call(order_id, tenant_id, "completed", None, 10, call_id)
        order = db.query_one("SELECT order_status FROM orders WHERE id = %s", (order_id,))
        assert order["order_status"] == "FAILED"
        
        # 4. busy call -> NO_ANSWER + retry schedule
        order_id, call_id, attempt_id = setup_order_call("O-4")
        update_status_from_call(order_id, tenant_id, "busy", None, 0, call_id)
        order = db.query_one("SELECT order_status FROM orders WHERE id = %s", (order_id,))
        assert order["order_status"] == "NO_ANSWER"
        schedule_mock.assert_called_with(order_id, tenant_id, call_result="busy")
        
        # 5. failed call -> FAILED
        order_id, call_id, attempt_id = setup_order_call("O-5")
        update_status_from_call(order_id, tenant_id, "failed", None, 0, call_id)
        order = db.query_one("SELECT order_status FROM orders WHERE id = %s", (order_id,))
        assert order["order_status"] == "FAILED"

def test_get_order_and_list_orders(app, test_setup):
    """Test get_order (retrieval with attempts) and list_orders (filtering, pagination)."""
    tenant_id = test_setup
    with app.app_context():
        # Setup mock orders
        order_id1 = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, order_status, created_at) VALUES (%s, 'O-GET1', '01712345678', 'CONFIRMED', now() - interval '2 days') RETURNING id",
            (tenant_id,)
        )
        order_id2 = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, order_status, created_at) VALUES (%s, 'O-GET2', '01787654321', 'FAILED', now()) RETURNING id",
            (tenant_id,)
        )
        
        # 1. get_order by internal ID
        o1 = get_order(order_id1, tenant_id)
        assert o1 is not None
        assert o1["external_order_id"] == "O-GET1"
        
        # 2. get_order by external ID
        o2 = get_order("O-GET2", tenant_id)
        assert o2 is not None
        assert o2["id"] == order_id2
        
        # 3. get_order missing
        assert get_order("MISSING-ID", tenant_id) is None
        
        # 4. list_orders with filters
        res = list_orders(tenant_id, filters={"status": "CONFIRMED"}, page=1, per_page=10)
        assert res["total"] == 1
        assert res["data"][0]["external_order_id"] == "O-GET1"
        
        # Date range filtering
        date_from = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        res_date = list_orders(tenant_id, filters={"date_from": date_from}, page=1, per_page=10)
        assert res_date["total"] == 1
        assert res_date["data"][0]["external_order_id"] == "O-GET2"

def test_manual_recall_controls(app, test_setup, monkeypatch):
    """Test manual_recall restriction checks, billing validation, and attempt counter resets."""
    tenant_id = test_setup
    
    # Mock queue_confirmation_call to return 88
    monkeypatch.setattr("app.services.order_service.queue_confirmation_call", lambda oid, tid: 88)
    
    with app.app_context():
        # Setup an order in CALLING state (recall forbidden)
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, order_status, attempt_count) VALUES (%s, 'REC-1', '01700000000', 'CALLING', 3) RETURNING id",
            (tenant_id,)
        )
        
        # Recall should fail (wrong state)
        with pytest.raises(ValueError) as exc:
            manual_recall(order_id, tenant_id)
        assert "Recall is only allowed" in str(exc.value)
        
        # Set to FAILED (recall allowed)
        db.execute("UPDATE orders SET order_status = 'FAILED' WHERE id = %s", (order_id,))
        
        # Recall execution
        call_id = manual_recall("REC-1", tenant_id)
        assert call_id == 88
        
        # Check attempt count reset to 1 and status set to CALL_QUEUED
        order = db.query_one("SELECT attempt_count, order_status FROM orders WHERE id = %s", (order_id,))
        assert order["attempt_count"] == 1
        assert order["order_status"] == "CALL_QUEUED"
        
        # Test insufficient balance recall failure
        db.execute("UPDATE wallets SET balance = 0 WHERE tenant_id = %s", (tenant_id,))
        db.execute("UPDATE orders SET order_status = 'FAILED' WHERE id = %s", (order_id,))
        with pytest.raises(InsufficientBalanceError):
            manual_recall(order_id, tenant_id)

def test_parse_template():
    """Verify parse_template handles substitutions."""
    order = {"external_order_id": "WC-123", "amount": 250.00, "customer_name": "Karim", "items": "Book"}
    tenant = {"business_name": "Atoz Shop"}
    
    template = "Hello {{customer_name}}, store {{store_name}} ID {{order_id}} price {{amount}}."
    res = parse_template(template, order, tenant)
    assert res == "Hello Karim, store Atoz Shop ID WC-123 price 250."

def test_additional_service_methods(app, test_setup, monkeypatch):
    """Test wrappers, flow exist routes, lookup fallbacks, missing orders and bad settings parsing in order_service."""
    tenant_id = test_setup
    
    # Mock tts generation
    monkeypatch.setattr("app.services.order_service.generate_order_audio", lambda tid, text, *args, **kwargs: "audio.ulaw")
    # Prevent async thread from executing to avoid transaction corruption
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    
    with app.app_context():
        # Insert a dummy call to satisfy the foreign key constraint in attempts table
        call_id = db.insert_returning_id(
            "INSERT INTO calls (number, status, direction, talk_seconds, tenant_id) VALUES ('01712345678', 'calling', 'outbound', 0, %s) RETURNING id",
            (tenant_id,)
        )
        monkeypatch.setattr(call_runner, "start_call", lambda num, flow, name="", *args, **kwargs: call_id)

        # Test create_order_and_queue wrapper
        data = {
            "order_id": "WRAPPER-1",
            "phone": "01712345678",
            "customer_name": "Wrapper Cust",
            "amount": "20.0"
        }
        res_wrap = create_order(tenant_id, data)
        assert res_wrap["external_order_id"] == "WRAPPER-1"
        
        # Test get_order_by_external_id_service
        from app.services.order_service import get_order_by_external_id_service
        found_order = get_order_by_external_id_service(tenant_id, "WRAPPER-1")
        assert found_order is not None
        assert found_order["id"] == res_wrap["id"]
        
        # Test queue_confirmation_call when flow already exists
        # 1st call creates the flow
        call_id_1 = queue_confirmation_call(res_wrap["id"], tenant_id)
        assert call_id_1 == call_id
        # 2nd call uses the existing flow
        call_id_2 = queue_confirmation_call(res_wrap["id"], tenant_id)
        assert call_id_2 == call_id
        
        # Test update_status_from_call with missing order
        from app.services.order_service import update_status_from_call
        # Should not raise exception
        update_status_from_call(999999, tenant_id, "completed", "1", 10, None)
        
        # Test update_status_from_call with call_id as None (will lookup from attempts)
        order_id = res_wrap["id"]
        # Clear specific call_id in attempt to check fallback lookup
        db.execute("UPDATE order_call_attempts SET result = NULL WHERE order_id = %s", (order_id,))
        update_status_from_call(order_id, tenant_id, "completed", "1", 30, None)
        # Check it successfully set order status to CONFIRMED
        order_db = db.query_one("SELECT order_status FROM orders WHERE id = %s", (order_id,))
        assert order_db["order_status"] == "CONFIRMED"
        
        # Test update_status_from_call when wallet deduction fails
        # Reset order and attempt
        db.execute("UPDATE orders SET order_status = 'CALLING' WHERE id = %s", (order_id,))
        db.execute("UPDATE order_call_attempts SET result = NULL, call_id = %s WHERE order_id = %s", (call_id, order_id))
        db.execute("UPDATE calls SET wallet_transaction_id = NULL WHERE id = %s", (call_id,))
        # Mock wallet deduct to raise exception
        def mock_deduct_fail(tenant_id, amount, description, ref_id=None):
            raise RuntimeError("DB Pool Timeout")
        monkeypatch.setattr("app.services.billing_service.deduct", mock_deduct_fail)
        
        # This call should catch the deduction error internally and still proceed to update order status
        update_status_from_call(order_id, tenant_id, "completed", "2", 40, call_id)
        order_db = db.query_one("SELECT order_status FROM orders WHERE id = %s", (order_id,))
        assert order_db["order_status"] == "CANCELLED"
        
        # Test manual_recall when order is missing
        with pytest.raises(ValueError):
            manual_recall("NON-EXISTENT-ORDER-ID", tenant_id)
            
        # Test settings JSON parsing failure coverage in create_order and queue_confirmation_call
        # We temporarily mock get_tenant_by_id to return a string settings value
        import app.services.order_service as os_mod
        original_get_tenant = os_mod.get_tenant_by_id
        def mock_get_tenant(tid):
            t = original_get_tenant(tid)
            if t:
                t = dict(t)
                t["settings"] = "INVALID-JSON-STRING"
            return t
        monkeypatch.setattr("app.services.order_service.get_tenant_by_id", mock_get_tenant)
        
        # Create a new order with invalid JSON settings on tenant
        data_invalid_settings = {
            "order_id": "WRAPPER-INVALID",
            "phone": "01712345678",
            "amount": "10.0"
        }
        order_invalid = create_order(tenant_id, data_invalid_settings)
        assert order_invalid is not None
        
        # Test queue_confirmation_call settings JSON parsing failure coverage
        queue_confirmation_call(order_invalid["id"], tenant_id)
        
        # Restore original get_tenant_by_id
        monkeypatch.setattr("app.services.order_service.get_tenant_by_id", original_get_tenant)

def test_call_monitor(app, monkeypatch):
    """Test background call monitor loop, start and stop monitor methods."""
    import app.services.order_service as os_mod
    
    calls_processed = []
    def mock_query(q, params=None):
        if "order_call_attempts" in q:
            return [{"id": 1, "order_id": 100, "tenant_id": 1, "call_id": 200, "attempt_no": 1}]
        return []
        
    def mock_query_one(q, params=None):
        if "FROM calls" in q:
            return {"status": "completed", "talk_seconds": 30}
        if "FROM call_events" in q:
            return {"digit": "1"}
        return None
        
    def mock_update_status(order_id, tenant_id, call_result, dtmf_digit, talk_seconds, call_id):
        calls_processed.append((order_id, call_result, dtmf_digit))
        # Turn off monitor to stop the loop
        os_mod._monitor_active = False
        
    monkeypatch.setattr(os_mod.db, "query", mock_query)
    monkeypatch.setattr(os_mod.db, "query_one", mock_query_one)
    monkeypatch.setattr(os_mod, "update_status_from_call", mock_update_status)
    
    # Test start and stop monitor functions (starts thread but it is mocked by conftest fixture)
    os_mod.start_call_monitor(app)
    os_mod.start_call_monitor(app)
    os_mod.stop_call_monitor()
    
    # Run loop synchronously
    os_mod._monitor_active = True
    os_mod._monitor_loop(app)
    
    assert len(calls_processed) > 0

