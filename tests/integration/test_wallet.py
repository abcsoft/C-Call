from __future__ import annotations

import time
from unittest.mock import patch
import pytest
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from app.db import db
from app.services import wallet_service, order_service
from app.services.order_service import update_status_from_call
from app.exceptions import InsufficientBalanceError

def _create_api_key(tenant_id, name="Test Key", key_str="cc_live_testkey12345"):
    import hashlib
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

def test_wallet_deduction_after_call(app, create_tenant):
    """Test that a completed call with talk duration deducts the correct rate from tenant wallet balance."""
    tenant, user = create_tenant("Deduct Store", "deduct@store.com")

    with app.app_context():
        with patch("app.services.webhook_service.deliver_async"):
            # Set balance to 50 BDT and rate min_charge=0.80, rate_per_min=0.80
            db.execute("UPDATE wallets SET balance = 50.00 WHERE tenant_id = %s", (tenant["id"],))
            db.execute("UPDATE customer_rates SET rate_per_min = 1.20, min_charge = 1.00 WHERE tenant_id = %s", (tenant["id"],))

            order_id = db.insert_returning_id(
                "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'CALLING') RETURNING id",
                (tenant["id"], "ORD-DED-1", "01712345678", "Sakib", Decimal("100.00"))
            )
            call_id = db.insert_returning_id(
                "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds, started_at) VALUES (%s, %s, %s, %s, %s, %s, '2026-07-20 00:00:00+00') RETURNING id",
                (tenant["id"], order_id, "01712345678", "completed", "outbound", 90) # 1m 30s
            )
            db.execute(
                "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no) VALUES (%s, %s, %s, 1)",
                (tenant["id"], order_id, call_id)
            )

            # 90s = 1.5 min. Rate = 1.20/min. Total charge = 1.5 * 1.20 = 1.80 BDT
            update_status_from_call(
                order_id=order_id,
                tenant_id=tenant["id"],
                call_result="completed",
                dtmf_digit="1",
                talk_seconds=90,
                call_id=call_id
            )

            # Verify wallet balance
            wallet = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tenant["id"],))
            assert wallet["balance"] == Decimal("47.60") # 50.00 - 2.40 = 47.60 BDT

            # Verify transaction log
            txn = db.query_one("SELECT * FROM wallet_transactions WHERE tenant_id = %s ORDER BY id DESC LIMIT 1", (tenant["id"],))
            assert txn["type"] == "debit"
            assert txn["amount"] == Decimal("2.40")
            assert txn["balance_after"] == Decimal("47.60")


@pytest.mark.real_db
def test_concurrent_deductions_dont_go_negative(app, create_tenant):
    """Test that concurrent balance deductions serialize and do not push the balance below zero."""
    # We bypass the single connection transaction wrapper using real_db marker.
    # Setup tenant using create_tenant fixture
    tenant, user = create_tenant("Concurrent Store", "concur@store.com")
    tenant_id = tenant["id"]
    
    with app.app_context():
        db.execute(
            "UPDATE wallets SET balance = 5.00, low_balance_threshold = 1.00 WHERE tenant_id = %s",
            (tenant_id,)
        )

    # We will trigger 4 concurrent deductions of 2.0 BDT.
    # Since initial balance is 5.00, only 2 should succeed, leaving 1.00 BDT.
    # 2 must raise InsufficientBalanceError.
    
    success_count = 0
    failure_count = 0
    
    def run_deduction(idx):
        nonlocal success_count, failure_count
        # We must execute database operations within app context
        with app.app_context():
            try:
                wallet_service.deduct(tenant_id, Decimal("2.00"), description=f"Concurrent test {idx}")
                success_count += 1
            except InsufficientBalanceError:
                failure_count += 1
            except Exception as e:
                # Catch other exceptions to debug
                print("Error:", e)

    # Run in ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(run_deduction, i) for i in range(4)]
        for f in futures:
            f.result()

    assert success_count == 2
    assert failure_count == 2
    
    with app.app_context():
        # Check final balance in DB
        wallet = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tenant_id,))
        assert wallet["balance"] == Decimal("1.00")
        
        # Cleanup tenant since this is real_db
        db.execute("DELETE FROM wallet_transactions WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM wallets WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM customer_rates WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM retry_rules WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM users WHERE tenant_id = %s", (tenant_id,))
        db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))


def test_recharge_credits_wallet(app, create_tenant):
    """Test that recharging / crediting the wallet updates the balance correctly."""
    tenant, user = create_tenant("Credit Store", "credit@store.com")

    with app.app_context():
        db.execute("UPDATE wallets SET balance = 10.00 WHERE tenant_id = %s", (tenant["id"],))

        # Credit 500 BDT
        wallet_service.credit(
            tenant_id=tenant["id"],
            amount=Decimal("500.00"),
            description="Recharge credits test",
            ref_id="REF-1234"
        )

        wallet = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tenant["id"],))
        assert wallet["balance"] == Decimal("510.00")

        # Verify transaction log
        txn = db.query_one("SELECT * FROM wallet_transactions WHERE tenant_id = %s ORDER BY id DESC LIMIT 1", (tenant["id"],))
        assert txn["type"] == "credit"
        assert txn["amount"] == Decimal("500.00")
        assert txn["balance_after"] == Decimal("510.00")
        assert txn["ref_id"] == "REF-1234"


def test_low_balance_blocks_call(client, app, create_tenant):
    """Test that creating an order fails with 402 if the balance is lower than min_charge."""
    tenant, user = create_tenant("Low Bal Store", "lowbal@store.com")
    api_key = _create_api_key(tenant["id"], key_str="cc_live_lowbaltest")

    with app.app_context():
        # Set balance below min_charge (min_charge is 0.80)
        db.execute("UPDATE wallets SET balance = 0.50 WHERE tenant_id = %s", (tenant["id"],))
        db.execute("UPDATE customer_rates SET min_charge = 1.00 WHERE tenant_id = %s", (tenant["id"],))

    payload = {
        "order_id": "ORD-LOWBAL-1",
        "phone": "01712345678",
        "customer_name": "Rahim",
        "amount": 100.0
    }

    response = client.post(
        "/api/v1/orders",
        headers={"X-API-Key": api_key},
        json=payload
    )
    assert response.status_code == 402
    assert b"INSUFFICIENT_BALANCE" in response.data


def test_transaction_history_paginated(client, auth_client, create_tenant, app):
    """Test that transaction history endpoint correctly paginates and returns JSON."""
    tenant, user = create_tenant("Txn Store", "txn@store.com")
    client = auth_client((tenant, user))

    # Insert mock transactions
    with app.app_context():
        wallet_row = db.query_one("SELECT id FROM wallets WHERE tenant_id = %s", (tenant["id"],))
        wallet_id = wallet_row["id"]
        for i in range(1, 6):
            db.execute(
                """
                INSERT INTO wallet_transactions (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
                VALUES (%s, %s, 'credit', %s, %s, %s, %s)
                """,
                (wallet_id, tenant["id"], Decimal("100.00"), Decimal(f"{i * 100.00}"), f"Deposit {i}", f"REF-{i}")
            )

    # Query first page via JSON API
    response = client.get(
        "/api/wallet/transactions?limit=2",
        headers={"Accept": "application/json"}
    )
    assert response.status_code == 200
    data = response.json
    assert data["success"] is True
    assert len(data["transactions"]) == 2
    assert data["total"] == 5
    assert data["has_next"] is True
    assert data["has_prev"] is False
    
    # Query next page using next_cursor
    next_cursor = data["next_cursor"]
    response2 = client.get(
        f"/api/wallet/transactions?limit=2&after={next_cursor}",
        headers={"Accept": "application/json"}
    )
    assert response2.status_code == 200
    data2 = response2.json
    assert len(data2["transactions"]) == 2
    assert data2["has_next"] is True
    assert data2["has_prev"] is True
