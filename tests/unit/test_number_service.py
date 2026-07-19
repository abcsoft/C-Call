from __future__ import annotations

import pytest
from decimal import Decimal
from app.db import db
from app.models.tenant import create_tenant
from app.models.sip_provider import create_provider
from app.services.number_service import (
    validate_pool_phone,
    select_caller_id,
    assign_number,
    unassign_number,
    add_to_pool,
    get_pool
)

def test_validate_pool_phone():
    # Valid Bangladesh mobile numbers
    assert validate_pool_phone("01712345678") == "+8801712345678"
    assert validate_pool_phone("8801812345678") == "+8801812345678"
    assert validate_pool_phone("+8801912345678") == "+8801912345678"
    
    # Valid E.164 number
    assert validate_pool_phone("+12025550143") == "+12025550143"
    
    # Invalid numbers
    with pytest.raises(ValueError):
        validate_pool_phone("12345")
    with pytest.raises(ValueError):
        validate_pool_phone("abcdefghijk")
    with pytest.raises(ValueError):
        validate_pool_phone("+12345")  # too short for E.164


def test_validate_pool_phone_accepts_bd_mobile():
    assert validate_pool_phone("01712345678") == "+8801712345678"


def test_validate_pool_phone_accepts_bd_virtual_pbx_number():
    # Real-world case that was previously incorrectly rejected — a SIP
    # provider-issued virtual/PBX caller ID, not a mobile-range number.
    result = validate_pool_phone("09602111380")
    assert result.startswith("+88")
    assert result == "+8809602111380"


def test_validate_pool_phone_accepts_e164():
    assert validate_pool_phone("+15551234567") == "+15551234567"


def test_validate_pool_phone_rejects_garbage():
    with pytest.raises(ValueError):
        validate_pool_phone("12345")


def test_select_caller_id(app):
    with app.app_context():
        tenant_id = create_tenant("CallerID Store", "callerid@store.com")
        
        # 1. Initially no numbers are available - returns None
        assert select_caller_id(tenant_id) is None
        
        # Add a mock SIP provider
        prov_id = create_provider({
            "name": "Test Gateway",
            "sip_domain": "sip.testgateway.com",
            "sip_port": 5060,
            "sip_user": "testuser",
            "sip_password": "testpassword"
        })
        
        # 2. Add shared numbers and test round-robin
        num1 = add_to_pool("01700000001", prov_id, "shared", Decimal("100.0"))
        num2 = add_to_pool("01700000002", prov_id, "shared", Decimal("100.0"))
        
        selected_numbers = [select_caller_id(tenant_id) for _ in range(4)]
        assert num1["number"] in selected_numbers
        assert num2["number"] in selected_numbers
        
        # 3. Add dedicated number and assign it (will default to primary since it is the first one)
        num_ded = add_to_pool("01700000003", prov_id, "dedicated", Decimal("200.0"))
        assign_number(tenant_id, num_ded["id"], "dedicated")
        
        assert select_caller_id(tenant_id) == num_ded["number"]
        
        # Demote the first dedicated assignment to non-primary
        db.execute(
            "UPDATE customer_number_assignments SET is_primary = false WHERE tenant_id = %s AND phone_number_id = %s",
            (tenant_id, num_ded["id"])
        )
        
        # 4. Make another dedicated number primary
        num_primary = add_to_pool("01700000004", prov_id, "dedicated", Decimal("250.0"))
        db.execute(
            """
            INSERT INTO customer_number_assignments (tenant_id, phone_number_id, is_primary, assigned_at)
            VALUES (%s, %s, true, now() + interval '1 second')
            """,
            (tenant_id, num_primary["id"])
        )
        
        # Should now prefer the primary dedicated number
        assert select_caller_id(tenant_id) == num_primary["number"]

def test_assign_and_unassign_number(app):
    with app.app_context():
        tenant_id = create_tenant("Assign Store", "assign@store.com")
        prov_id = create_provider({
            "name": "Test Gateway",
            "sip_domain": "sip.testgateway.com",
            "sip_port": 5060,
            "sip_user": "testuser",
            "sip_password": "testpassword"
        })
        
        num = add_to_pool("01711111111", prov_id, "shared", Decimal("100.0"))
        
        # Assign number
        assigned = assign_number(tenant_id, num["id"], "dedicated")
        assert assigned["tenant_id"] == tenant_id
        assert assigned["number_type"] == "dedicated"
        
        # Check active assignment in DB
        row = db.query_one(
            "SELECT * FROM customer_number_assignments WHERE phone_number_id = %s AND unassigned_at IS NULL",
            (num["id"],)
        )
        assert row is not None
        assert row["tenant_id"] == tenant_id
        
        # Try assigning dedicated number to another tenant (should fail)
        tenant_b_id = create_tenant("Conflict Store", "conflict@store.com")
        with pytest.raises(ValueError) as exc:
            assign_number(tenant_b_id, num["id"], "dedicated")
        assert "already assigned" in str(exc.value)
        
        # Unassign number
        unassign_number(tenant_id, num["id"])
        
        # Check no active assignment in DB
        row_after = db.query_one(
            "SELECT * FROM customer_number_assignments WHERE phone_number_id = %s AND unassigned_at IS NULL",
            (num["id"],)
        )
        assert row_after is None
        
        # Assignment history exists
        history = db.query("SELECT * FROM customer_number_assignments WHERE phone_number_id = %s", (num["id"],))
        assert len(history) == 1
        assert history[0]["unassigned_at"] is not None

def test_get_pool(app):
    with app.app_context():
        tenant_id = create_tenant("Filter Store", "filter@store.com")
        prov_id = create_provider({
            "name": "Test Gateway",
            "sip_domain": "sip.testgateway.com",
            "sip_port": 5060,
            "sip_user": "testuser",
            "sip_password": "testpassword"
        })
        
        # Clear existing phone numbers for precise count in test
        db.execute("DELETE FROM customer_number_assignments")
        db.execute("DELETE FROM phone_numbers")
        
        n1 = add_to_pool("01722222222", prov_id, "shared", Decimal("100.0"))
        n2 = add_to_pool("01733333333", prov_id, "dedicated", Decimal("200.0"))
        
        # 1. Get all
        res = get_pool(page=1, per_page=10)
        assert res["total"] == 2
        assert len(res["data"]) == 2
        
        # 2. Filter by status 'active'
        res_active = get_pool(filters={"status": "active"})
        assert res_active["total"] == 2
        
        # 3. Filter by status 'unassigned'
        res_unassigned = get_pool(filters={"status": "unassigned"})
        assert res_unassigned["total"] == 1  # Only n2 (dedicated, active, unassigned)
        
        # Assign n2
        assign_number(tenant_id, n2["id"], "dedicated")
        
        # Now unassigned should be 0
        res_unassigned_after = get_pool(filters={"status": "unassigned"})
        assert res_unassigned_after["total"] == 0
        
        # 4. Search filter
        res_search = get_pool(filters={"search": "01722"})
        assert res_search["total"] == 1
        assert res_search["data"][0]["number"] == n1["number"]

def test_cli_pool_assignment_scenarios(app):
    with app.app_context():
        # Clean DB for numbers
        db.execute("DELETE FROM customer_number_assignments")
        db.execute("DELETE FROM phone_numbers")
        
        # Create tenants & provider
        tenant_live = create_tenant("Live Test Store", "live@test.com")
        tenant_solar = create_tenant("Solar Bangla Ltd", "solar@bangla.com")
        prov_id = create_provider({
            "name": "Test Gateway",
            "sip_domain": "sip.testgateway.com",
            "sip_port": 5060,
            "sip_user": "testuser",
            "sip_password": "testpassword"
        })
        
        # 1. Add shared number -> no tenant assignment
        num_shared = add_to_pool("01700000100", prov_id, "shared", Decimal("100.0"))
        assignment = db.query_one("SELECT * FROM customer_number_assignments WHERE phone_number_id = %s", (num_shared["id"],))
        assert assignment is None
        
        # 2. Add dedicated number for Live Test Store -> creates active assignment
        num_ded_live = add_to_pool("01700000200", prov_id, "dedicated", Decimal("200.0"))
        assign_number(tenant_live, num_ded_live["id"], "dedicated")
        
        # Check active assignment in DB
        assignment_live = db.query_one(
            "SELECT * FROM customer_number_assignments WHERE phone_number_id = %s AND unassigned_at IS NULL", 
            (num_ded_live["id"],)
        )
        assert assignment_live is not None
        assert assignment_live["tenant_id"] == tenant_live
        
        # 3. Add dedicated number for Solar Bangla Ltd -> creates active assignment
        num_ded_solar = add_to_pool("01700000300", prov_id, "dedicated", Decimal("200.0"))
        assign_number(tenant_solar, num_ded_solar["id"], "dedicated")
        
        # Check active assignment
        assignment_solar = db.query_one(
            "SELECT * FROM customer_number_assignments WHERE phone_number_id = %s AND unassigned_at IS NULL", 
            (num_ded_solar["id"],)
        )
        assert assignment_solar is not None
        assert assignment_solar["tenant_id"] == tenant_solar
        
        # 4. Release dedicated number -> Dedicated but unassigned (no active assignment, but type remains dedicated)
        unassign_number(tenant_live, num_ded_live["id"])
        
        # Check no active assignment
        active_assignment_after = db.query_one(
            "SELECT * FROM customer_number_assignments WHERE phone_number_id = %s AND unassigned_at IS NULL",
            (num_ded_live["id"],)
        )
        assert active_assignment_after is None
        
        # Fetch status/type of number
        number_rec = db.query_one("SELECT * FROM phone_numbers WHERE id = %s", (num_ded_live["id"],))
        assert number_rec["number_type"] == "dedicated"
        
        # 5. Assign released number again -> works, no UNIQUE error
        assign_number(tenant_live, num_ded_live["id"], "dedicated")
        
        # Release and assign it to another tenant too (e.g. Solar Bangla)
        unassign_number(tenant_live, num_ded_live["id"])
        assign_number(tenant_solar, num_ded_live["id"], "dedicated")
        
        # Verify it works
        active_assignment_solar = db.query_one(
            "SELECT * FROM customer_number_assignments WHERE phone_number_id = %s AND unassigned_at IS NULL", 
            (num_ded_live["id"],)
        )
        assert active_assignment_solar is not None
        assert active_assignment_solar["tenant_id"] == tenant_solar
        
        # 6. select_caller_id(tenant_id) returns dedicated number first
        db.execute("DELETE FROM customer_number_assignments")
        db.execute("DELETE FROM phone_numbers")
        
        n_shared = add_to_pool("01700000999", prov_id, "shared", Decimal("100.0"))
        n_ded = add_to_pool("01700000888", prov_id, "dedicated", Decimal("200.0"))
        assign_number(tenant_live, n_ded["id"], "dedicated")
        
        assert select_caller_id(tenant_live) == n_ded["number"]
        
        # 7. If tenant has no dedicated number, select_caller_id uses shared pool
        assert select_caller_id(tenant_solar) == n_shared["number"]
        
        # 8. If no shared/dedicated number exists, order call fails with clear "no active phone CLI available"
        db.execute("DELETE FROM customer_number_assignments")
        db.execute("DELETE FROM phone_numbers")
        
        assert select_caller_id(tenant_live) is None
        
        from app.services.order_service import queue_confirmation_call
        from app.models.order import create_order as db_create_order
        from app.exceptions import DispatchNoCallerIdError
        import pytest

        order_id = db_create_order(
            tenant_id=tenant_live,
            external_order_id="TEST-101",
            phone="+8801700000000",
            customer_name="John Doe",
            amount=Decimal("1500.0"),
            webhook_url="https://example.com/webhook"
        )

        with pytest.raises(DispatchNoCallerIdError):
            queue_confirmation_call(order_id, tenant_live, attempt_no=1)

