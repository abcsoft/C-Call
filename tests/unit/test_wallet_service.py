from __future__ import annotations

import threading
from decimal import Decimal
import pytest
import psycopg

from app.db import db
from app.exceptions import InsufficientBalanceError
from app.models.tenant import create_tenant
from app.services.wallet_service import deduct, credit, refund, get_balance, get_transactions
from app.services.rate_service import calculate_charge, estimate_charge, get_tenant_rate


def test_deduct_is_atomic_under_concurrent_calls(app):
    """Verify that multiple threads performing concurrent deductions run atomically and respect row-level locks."""
    # Restore original Thread.start and pool.connection
    from psycopg_pool import ConnectionPool
    threading.Thread.start = threading.Thread._original_start
    pool = db.get_pool()
    original_pool_connection = pool.connection
    pool.connection = ConnectionPool.connection.__get__(pool, ConnectionPool)

    tenant_id = None
    try:
        with app.app_context():
            # Create unique tenant for this concurrency test
            tenant_id = create_tenant({
                "business_name": "Concurrent Store",
                "email": "concurrent@store.com",
                "plan_name": "free",
                "status": "active"
            })
            # Credit 10 BDT
            credit(tenant_id, Decimal("10.00"), "Seed deposit")
            assert get_balance(tenant_id) == Decimal("10.00")

        # Spin up two threads trying to deduct 6 BDT each concurrently
        results = []
        errors = []

        def run_deduct():
            with app.app_context():
                try:
                    deduct(tenant_id, Decimal("6.00"), "Deduction attempt")
                    results.append(True)
                except InsufficientBalanceError as exc:
                    errors.append(exc)
                except Exception as exc:
                    errors.append(exc)

        t1 = threading.Thread(target=run_deduct)
        t2 = threading.Thread(target=run_deduct)

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        # One transaction must succeed, the other must fail with InsufficientBalanceError
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], InsufficientBalanceError)

        with app.app_context():
            assert get_balance(tenant_id) == Decimal("4.00")
            txns = get_transactions(tenant_id, page=1, per_page=20)
            # Seed credit (1) + Successful debit (1) = 2 transactions total
            assert txns["total"] == 2

    finally:
        # Clean up database records manually (since we bypassed db_conn transaction wrappers)
        if tenant_id:
            with psycopg.connect(app.config["DATABASE_URL"]) as conn:
                conn.execute("DELETE FROM wallet_transactions WHERE tenant_id = %s", (tenant_id,))
                conn.execute("DELETE FROM customer_rates WHERE tenant_id = %s", (tenant_id,))
                conn.execute("DELETE FROM retry_rules WHERE tenant_id = %s", (tenant_id,))
                conn.execute("DELETE FROM wallets WHERE tenant_id = %s", (tenant_id,))
                conn.execute("DELETE FROM users WHERE tenant_id = %s", (tenant_id,))
                conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
                conn.commit()

        # Restore conftest mocks
        pool.connection = original_pool_connection
        threading.Thread.start = lambda self: None


def test_deduct_raises_on_insufficient_balance(app):
    """Verify that deducting more than the wallet balance raises an InsufficientBalanceError."""
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Limit Store",
            "email": "limit@store.com",
            "plan_name": "free",
            "status": "active"
        })
        credit(tenant_id, Decimal("5.00"), "Initial Deposit")
        
        with pytest.raises(InsufficientBalanceError) as exc_info:
            deduct(tenant_id, Decimal("6.00"), "Call Charge")
            
        assert exc_info.value.current_balance == Decimal("5.00")
        assert exc_info.value.required_amount == Decimal("6.00")
        assert get_balance(tenant_id) == Decimal("5.00")


def test_balance_never_goes_negative(app):
    """Verify application-level check and database CHECK constraint prevent negative balances."""
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Negative Store",
            "email": "negative@store.com",
            "plan_name": "free",
            "status": "active"
        })
        
        # 1. Application-level check
        with pytest.raises(InsufficientBalanceError):
            deduct(tenant_id, Decimal("0.01"), "Overdraft")
            
        # 2. Database check constraint (balance >= 0)
        # We try to bypass the app-level guard by updating the balance directly in SQL
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute("UPDATE wallets SET balance = -1.00 WHERE tenant_id = %s", (tenant_id,))


def test_calculate_charge_rounds_up_to_minute(app):
    """Verify that talk seconds are rounded up to the nearest minute before billing."""
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Round Store",
            "email": "round@store.com",
            "plan_name": "free",
            "status": "active"
        })
        # Platform rate is 0.80 BDT/min, min_charge = 0.80 BDT
        
        # 60s -> 1 minute -> 0.80 BDT
        assert calculate_charge(60, tenant_id) == Decimal("0.8000")
        
        # 61s -> 2 minutes -> 1.60 BDT
        assert calculate_charge(61, tenant_id) == Decimal("1.6000")
        
        # 119s -> 2 minutes -> 1.60 BDT
        assert calculate_charge(119, tenant_id) == Decimal("1.6000")
        
        # 120s -> 2 minutes -> 1.60 BDT
        assert calculate_charge(120, tenant_id) == Decimal("1.6000")
        
        # 121s -> 3 minutes -> 2.40 BDT
        assert calculate_charge(121, tenant_id) == Decimal("2.4000")


def test_min_charge_applied_for_short_calls(app):
    """Verify that a minimum charge is applied for calls shorter than 1 minute."""
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Short Store",
            "email": "short@store.com",
            "plan_name": "free",
            "status": "active"
        })
        
        # 0s talk time -> min_charge (0.80 BDT)
        assert calculate_charge(0, tenant_id) == Decimal("0.8000")
        
        # 10s talk time -> min_charge (0.80 BDT)
        assert calculate_charge(10, tenant_id) == Decimal("0.8000")
        
        # 45s talk time -> min_charge (0.80 BDT)
        assert calculate_charge(45, tenant_id) == Decimal("0.8000")


def test_per_call_rate_overrides_per_minute(app):
    """Verify that a non-zero rate_per_call overrides per-minute billing."""
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Flat Store",
            "email": "flat@store.com",
            "plan_name": "free",
            "status": "active"
        })
        
        # Insert flat rate override (2.50 BDT flat charge)
        db.execute(
            """
            INSERT INTO customer_rates (tenant_id, rate_per_min, rate_per_call, min_charge)
            VALUES (%s, 0.0000, 2.5000, 2.5000)
            """,
            (tenant_id,)
        )
        
        # 10s, 60s, and 600s should all cost exactly 2.50 BDT
        assert calculate_charge(10, tenant_id) == Decimal("2.5000")
        assert calculate_charge(60, tenant_id) == Decimal("2.5000")
        assert calculate_charge(600, tenant_id) == Decimal("2.5000")
        assert estimate_charge(tenant_id) == Decimal("2.5000")
