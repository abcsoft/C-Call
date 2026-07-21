from decimal import Decimal
from app.models.tenant import create_tenant
from app.services.rate_service import calculate_charge, estimate_charge, get_tenant_rate
from app.db import db

def test_rate_calculations(app):
    with app.app_context():
        tenant_id = create_tenant("Rate Store", "rates@store.com")
        
        # 1. Default platform rates (no overrides in database)
        rates = get_tenant_rate(tenant_id)
        assert rates["rate_per_min"] == Decimal("0.80")
        assert rates["rate_per_call"] == Decimal("0.00")
        assert rates["min_charge"] == Decimal("0.80")
        
        # Estimate charge (should be min_charge = 0.80)
        assert estimate_charge(tenant_id) == Decimal("0.80")
        
        # calculate_charge for different talk times
        # 0s talk time -> 0 (call was not answered; billing based on connection evidence)
        assert calculate_charge(0, tenant_id) == Decimal("0.00")
        # 45s talk time -> 1 min -> 0.80
        assert calculate_charge(45, tenant_id) == Decimal("0.80")
        # 60s talk time -> 1 min -> 0.80
        assert calculate_charge(60, tenant_id) == Decimal("0.80")
        # 61s talk time -> 2 mins -> 1.60
        assert calculate_charge(61, tenant_id) == Decimal("1.60")
        # 120s talk time -> 2 mins -> 1.60
        assert calculate_charge(120, tenant_id) == Decimal("1.60")
        # 121s talk time -> 3 mins -> 2.40
        assert calculate_charge(121, tenant_id) == Decimal("2.40")

        # 2. Add custom rate overrides for tenant
        db.execute(
            """
            INSERT INTO customer_rates (tenant_id, rate_per_min, rate_per_call, min_charge)
            VALUES (%s, 1.20, 0.00, 1.00)
            """,
            (tenant_id,)
        )
        
        # Invalidate rate cache for the tenant
        from app.models.tenant import invalidate_tenant_caches
        invalidate_tenant_caches(tenant_id)
        
        # Verify custom rates are picked up
        rates = get_tenant_rate(tenant_id)
        assert rates["rate_per_min"] == Decimal("1.20")
        assert rates["min_charge"] == Decimal("1.00")
        
        # Check calculation with custom rates
        # 30s talk time -> 1 min * 1.20 = 1.20 (max of 1.20 and min_charge 1.00)
        assert calculate_charge(30, tenant_id) == Decimal("1.20")
        # 10s talk time -> 1 min * 1.20 = 1.20
        assert calculate_charge(10, tenant_id) == Decimal("1.20")
        # 61s talk time -> 2 mins * 1.20 = 2.40
        assert calculate_charge(61, tenant_id) == Decimal("2.40")

        # 3. Flat charge override
        db.execute(
            """
            UPDATE customer_rates 
            SET rate_per_call = 2.50, rate_per_min = 0.00, min_charge = 2.50
            WHERE tenant_id = %s
            """,
            (tenant_id,)
        )
        
        # Invalidate rate cache for the tenant
        from app.models.tenant import invalidate_tenant_caches
        invalidate_tenant_caches(tenant_id)
        
        # 30s, 60s, 180s talk times should all cost exactly 2.50
        # 30s, 60s, 180s talk times should all cost exactly 2.50
        assert calculate_charge(30, tenant_id) == Decimal("2.50")
        assert calculate_charge(120, tenant_id) == Decimal("2.50")
        assert calculate_charge(600, tenant_id) == Decimal("2.50")


def test_pulse_billing_and_failed_calls(app):
    from decimal import Decimal
    from app.services.rate_service import calculate_charge_and_duration, get_tenant_rate
    from app.models.tenant import create_tenant, invalidate_tenant_caches
    from app.db import db
    from app.services.order_service import update_status_from_call, create_order
    from app.services.wallet_service import get_balance, credit
    
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Billing Test Store",
            "email": "billing@test.com",
            "plan_name": "free",
            "status": "active"
        })
        
        # 1. duration 6s, no pulse -> billable 60s, charge = 1 minute rate (0.80 BDT)
        # Default Platform Rate = 0.80 / min, pulse = 60s, min_charge = 0.80 BDT
        charge, billable = calculate_charge_and_duration(6, tenant_id, "completed")
        assert billable == 60
        assert charge == Decimal("0.8000")
        
        # 2. duration 29s, 30s pulse -> billable 30s
        # Update billing rules: pulse = 30, min_charge = 0.00, minimum_billable_seconds = 30
        db.execute(
            """
            INSERT INTO customer_rates (tenant_id, rate_per_min, rate_per_call, min_charge, billing_pulse_seconds, minimum_billable_seconds, charge_failed_calls)
            VALUES (%s, 1.20, 0.00, 0.00, 30, 30, false)
            ON CONFLICT (tenant_id) DO UPDATE SET
                rate_per_min = EXCLUDED.rate_per_min,
                rate_per_call = EXCLUDED.rate_per_call,
                min_charge = EXCLUDED.min_charge,
                billing_pulse_seconds = EXCLUDED.billing_pulse_seconds,
                minimum_billable_seconds = EXCLUDED.minimum_billable_seconds,
                charge_failed_calls = EXCLUDED.charge_failed_calls
            """,
            (tenant_id,)
        )
        invalidate_tenant_caches(tenant_id)
        
        # rate_per_min = 1.20 -> rate per second = 1.20 / 60 = 0.02
        # duration 29s -> rounded to 30s -> charge = 30 * 0.02 = 0.60
        charge, billable = calculate_charge_and_duration(29, tenant_id, "completed")
        assert billable == 30
        assert charge == Decimal("0.6000")
        
        # 3. duration 31s, 30s pulse -> billable 60s
        # duration 31s -> rounded to 60s -> charge = 60 * 0.02 = 1.20
        charge, billable = calculate_charge_and_duration(31, tenant_id, "completed")
        assert billable == 60
        assert charge == Decimal("1.2000")
        
        # 4. duration 61s, no pulse -> billable 120s
        # Update billing rules: pulse = 60 (or 0), min_charge = 0.00, minimum_billable_seconds = 60
        db.execute(
            """
            UPDATE customer_rates 
            SET billing_pulse_seconds = 60, minimum_billable_seconds = 60
            WHERE tenant_id = %s
            """,
            (tenant_id,)
        )
        invalidate_tenant_caches(tenant_id)
        
        # rate_per_min = 1.20. duration 61s -> rounded to 120s -> charge = 120 * 0.02 = 2.40
        charge, billable = calculate_charge_and_duration(61, tenant_id, "completed")
        assert billable == 120
        assert charge == Decimal("2.4000")
        
        # 5. failed call 0s -> charge 0 by default
        charge, billable = calculate_charge_and_duration(0, tenant_id, "failed")
        assert billable == 0
        assert charge == Decimal("0.0000")
        
        # Now check if charge_failed_calls is True
        db.execute("UPDATE customer_rates SET charge_failed_calls = true WHERE tenant_id = %s", (tenant_id,))
        invalidate_tenant_caches(tenant_id)
        # failed call 0s -> should apply minimum billable seconds (60) -> charge = 1.20
        charge, billable = calculate_charge_and_duration(0, tenant_id, "failed")
        assert billable == 60
        assert charge == Decimal("1.2000")
        
        # 6. completed call deducts wallet & double billing prevention
        # Reset charge_failed_calls = false
        db.execute("UPDATE customer_rates SET charge_failed_calls = false WHERE tenant_id = %s", (tenant_id,))
        invalidate_tenant_caches(tenant_id)
        
        # Setup wallet balance: 10 BDT
        credit(tenant_id, Decimal("10.00"), "Seed")
        assert get_balance(tenant_id) == Decimal("10.00")
        
        # Create a test call and order
        order = create_order(tenant_id, {
            "order_id": "BILLING-TEST-ORD",
            "phone": "+8801711111111",
            "customer_name": "Test Billing Customer",
            "amount": 150.00
        })
        
        # Insert a dummy call — use explicit pre-cutoff started_at so finalize_call_billing
        # uses live-rate fallback (not fail-closed) for this legacy-path test.
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, order_id, number, status, started_at)"
            " VALUES (%s, %s, %s, 'initiated', '2026-07-20 00:00:00+00') RETURNING id",
            (tenant_id, order["id"], order["phone"])
        )
        
        # Link call_id to the pre-created attempt row (attempt_no=1 was created in create_order).
        db.execute(
            """
            INSERT INTO order_call_attempts (order_id, tenant_id, call_id, attempt_no, result)
            VALUES (%s, %s, %s, 1, 'initiated')
            ON CONFLICT (order_id, attempt_no) DO UPDATE
                SET call_id = EXCLUDED.call_id, result = EXCLUDED.result
            """,
            (order["id"], tenant_id, call_id)
        )
        
        # Call completes with talk_seconds = 30.
        # Custom rate = 1.20/min. Pulse = 60s, min_billable = 60s.
        # So billable_seconds = 60s. Charge = 1.20 BDT.
        update_status_from_call(order["id"], tenant_id, "completed", "1", 30, call_id)
        
        # Wallet balance must decrease from 10.00 to 8.80
        assert get_balance(tenant_id) == Decimal("8.8000")
        
        # Check call record in DB
        call_row = db.query_one("SELECT * FROM calls WHERE id = %s", (call_id,))
        assert call_row["billable_seconds"] == 60
        assert call_row["charge"] == Decimal("1.2000")
        assert call_row["wallet_transaction_id"] is not None
        
        txn_id = call_row["wallet_transaction_id"]
        
        # Check attempt record in DB
        attempt_row = db.query_one("SELECT * FROM order_call_attempts WHERE call_id = %s", (call_id,))
        assert attempt_row["billable_seconds"] == 60
        assert attempt_row["charge"] == Decimal("1.2000")
        assert attempt_row["wallet_transaction_id"] == txn_id
        
        # 7. refreshing CDR/calling update_status_from_call again does NOT deduct again
        balance_before = get_balance(tenant_id)
        update_status_from_call(order["id"], tenant_id, "completed", "1", 30, call_id)
        assert get_balance(tenant_id) == balance_before  # balance remains 8.80

