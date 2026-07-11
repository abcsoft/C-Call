from __future__ import annotations

import csv
from io import StringIO
from decimal import Decimal
import pytest
from app.db import db
from app.services import wallet_service

def test_cdr_logs_page_and_summaries(auth_client, create_tenant, app):
    """Test accessing the CDR logs page and fetching metrics summaries."""
    tenant, user = create_tenant("CDR Merchant", "cdr_test@merchant.com")
    client = auth_client((tenant, user))

    # Insert test call data
    with app.app_context():
        # Insert a call attempt that has a charge
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, customer_name, phone, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'NEW') RETURNING id",
            (tenant["id"], "ORD-CDR-1", "Abedin", "01700000001", Decimal("1200.00"))
        )
        # calls.charge/billable_seconds is the source of truth now
        # (billing_service writes it) — set it directly rather than
        # relying on order_call_attempts.charge as a fallback.
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds, charge, billable_seconds) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant["id"], order_id, "01700000001", "completed", "outbound", 125, Decimal("3.75"), 180) # 2m 5s
        )
        db.execute(
            "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no, charge, dtmf_digit, result) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (tenant["id"], order_id, call_id, 1, Decimal("3.75"), "1", "confirmed")
        )

    # 1. GET /cdr logs main panel page
    response = client.get("/cdr")
    assert response.status_code == 200
    assert b"Call Detail Records" in response.data

    # 2. GET /api/cdr summary partial
    summary_resp = client.get("/api/cdr/summary")
    assert summary_resp.status_code == 200
    assert b"3.75" in summary_resp.data
    assert any(m in summary_resp.data for m in (b"3.0m", b"2.1m", b"2.0m"))

    # 3. GET /api/cdr listing (HTML rows partial)
    listing_resp = client.get("/api/cdr")
    assert listing_resp.status_code == 200
    assert b"017****0001" in listing_resp.data
    assert b"2m 5s" in listing_resp.data
    assert b"1 (Confirmed)" in listing_resp.data
    assert b"3.75" in listing_resp.data

    # 4. GET /api/cdr listing (JSON format)
    json_resp = client.get("/api/cdr", headers={"Accept": "application/json"})
    assert json_resp.status_code == 200
    data = json_resp.json
    assert data["success"] is True
    assert len(data["calls"]) == 1
    assert data["calls"][0]["number"] == "01700000001"
    assert data["calls"][0]["charge"] == 3.75


def test_cdr_export_streaming(auth_client, create_tenant, app):
    """Test CDR CSV streaming generation."""
    tenant, user = create_tenant("Export Merchant", "export@merchant.com")
    client = auth_client((tenant, user))

    # Insert mock calls
    with app.app_context():
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, customer_name, phone, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'CONFIRMED') RETURNING id",
            (tenant["id"], "ORD-EXP-9", "Habib", "01700000002", Decimal("150.00"))
        )
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds, recording_path, charge) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant["id"], order_id, "01700000002", "completed", "outbound", 45, "/var/spool/asterisk/monitor/rec_1.ulaw", Decimal("1.35"))
        )
        db.execute(
            "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no, charge, dtmf_digit, result) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (tenant["id"], order_id, call_id, 1, Decimal("1.35"), "2", "cancelled")
        )

    response = client.get("/api/cdr/export")
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith("attachment; filename=cdr-export_merchant-")
    assert response.mimetype == "text/csv"

    # Read streamed content
    csv_content = response.data.decode("utf-8")
    reader = csv.reader(StringIO(csv_content))
    rows = list(reader)

    # Assert headers
    assert rows[0] == ["Date", "Time", "Order ID", "Phone", "Duration (sec)", "DTMF", "Result", "Charge (BDT)", "Recording URL"]
    # Assert data row
    assert rows[1][2] == "ORD-EXP-9"
    assert rows[1][3] == "017****0002"
    assert rows[1][4] == "45"
    assert rows[1][5] == "2"
    assert rows[1][6] == "completed"
    assert rows[1][7] == "1.35"
    assert "rec_1.ulaw" in rows[1][8]


def test_wallet_settings_threshold_update(auth_client, create_tenant, app):
    """Test inline low balance threshold setting update."""
    tenant, user = create_tenant("Settings Merchant", "settings@merchant.com")
    client = auth_client((tenant, user))

    # Send POST to update threshold settings
    response = client.post("/api/wallet/settings", data={"threshold": "250.00"})
    assert response.status_code == 200
    assert b"threshold set to \xe0\xa7\xb3250.00" in response.data or b"250.00" in response.data

    with app.app_context():
        wallet = db.query_one("SELECT * FROM wallets WHERE tenant_id = %s", (tenant["id"],))
        assert wallet["low_balance_threshold"] == Decimal("250.00")


def test_wallet_recharge_mock_checkout_flow(auth_client, create_tenant, app):
    """Test payment recharge intent and mock sandbox checkout redirect workflow."""
    tenant, user = create_tenant("Recharge Merchant", "recharge@merchant.com")
    client = auth_client((tenant, user))

    # 1. POST to recharge endpoint to initiate deposit intent
    response = client.post("/api/wallet/recharge", data={
        "amount": "800.00",
        "payment_method": "sslcommerz"
    })
    # Since live credentials are empty, it redirects to the mock sandbox portal
    assert response.status_code == 302
    redirect_url = response.headers["Location"]
    assert "/wallet/recharge/mock/" in redirect_url

    # Extract intent ID
    intent_id = int(redirect_url.split("/")[-1])
    
    with app.app_context():
        intent = db.query_one("SELECT * FROM payment_intents WHERE id = %s", (intent_id,))
        assert intent["tenant_id"] == tenant["id"]
        assert intent["amount"] == Decimal("800.00")
        assert intent["status"] == "pending"

    # 2. View mock gateway landing page
    gateway_resp = client.get(f"/wallet/recharge/mock/{intent_id}")
    assert gateway_resp.status_code == 200
    assert b"SSLCommerz Simulation" in gateway_resp.data
    assert b"800.00" in gateway_resp.data


def test_payment_webhook_sslcommerz_callback(auth_client, create_tenant, app):
    """Test payment callback and verification of SSLCommerz signatures."""
    tenant, user = create_tenant("Webhook Merchant", "webhook_test@merchant.com")
    client = auth_client((tenant, user))

    # Initialize a payment intent
    with app.app_context():
        intent_id = db.insert_returning_id(
            "INSERT INTO payment_intents (tenant_id, gateway, amount, status) VALUES (%s, %s, %s, 'pending') RETURNING id",
            (tenant["id"], "sslcommerz", Decimal("1500.00"))
        )

    # 1. Successful payment validation callback simulation (Store Password not set, signature bypasses)
    success_callback_data = {
        "status": "VALID",
        "tran_id": str(intent_id),
        "amount": "1500.00",
        "val_id": "val_mock12345",
        "verify_sign": "mocksignature",
        "verify_key": "tran_id,amount,status,val_id"
    }
    
    # Send callback POST request (public callback endpoint)
    response = client.post("/webhooks/payment/sslcommerz", data=success_callback_data, follow_redirects=True)
    assert response.status_code == 200
    assert b"Recharge successful" in response.data

    # Verify wallet balance was updated atomically and transaction was created
    with app.app_context():
        # Wallet balance check
        wallet = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tenant["id"],))
        assert wallet["balance"] == Decimal("1500.00")

        # Transaction check
        txn = db.query_one("SELECT * FROM wallet_transactions WHERE tenant_id = %s ORDER BY id DESC LIMIT 1", (tenant["id"],))
        assert txn["type"] == "credit"
        assert txn["amount"] == Decimal("1500.00")
        assert txn["balance_after"] == Decimal("1500.00")
        assert txn["ref_id"] == "val_mock12345"

        # Intent status check
        intent = db.query_one("SELECT * FROM payment_intents WHERE id = %s", (intent_id,))
        assert intent["status"] == "completed"
        assert intent["gateway_ref"] == "val_mock12345"

    # 2. Failed payment validation callback simulation
    with app.app_context():
        failed_intent_id = db.insert_returning_id(
            "INSERT INTO payment_intents (tenant_id, gateway, amount, status) VALUES (%s, %s, %s, 'pending') RETURNING id",
            (tenant["id"], "sslcommerz", Decimal("300.00"))
        )

    fail_callback_data = {
        "status": "FAILED",
        "tran_id": str(failed_intent_id),
        "amount": "300.00"
    }
    fail_response = client.post("/webhooks/payment/sslcommerz", data=fail_callback_data, follow_redirects=True)
    assert fail_response.status_code == 200
    assert b"Recharge failed" in fail_response.data

    with app.app_context():
        intent = db.query_one("SELECT * FROM payment_intents WHERE id = %s", (failed_intent_id,))
        assert intent["status"] == "failed"
        
        # Balance should remain unchanged (still 1500)
        wallet = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tenant["id"],))
        assert wallet["balance"] == Decimal("1500.00")
