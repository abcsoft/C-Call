from __future__ import annotations

import csv
import datetime
from io import StringIO
from decimal import Decimal
import pytest
from app.db import db

def test_csv_export_contains_correct_columns(client, auth_client, create_tenant, app):
    """Test that CSV export contains all standard CDR columns specified in PRD."""
    tenant, user = create_tenant("CSV Store", "csv@store.com")
    client = auth_client((tenant, user))

    # Insert a dummy record
    with app.app_context():
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'CONFIRMED') RETURNING id",
            (tenant["id"], "ORD-CSV-1", "01712345678", "Customer A", Decimal("500.00"))
        )
        # calls.charge is the source of truth now (billing_service writes
        # it) — CDR export no longer falls back to order_call_attempts.charge
        # or recalculates a number on the fly.
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds, charge) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant["id"], order_id, "01712345678", "completed", "outbound", 65, Decimal("1.30"))
        )
        db.execute(
            "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no, charge, dtmf_digit, result) VALUES (%s, %s, %s, 1, 1.30, '1', 'confirmed')",
            (tenant["id"], order_id, call_id)
        )

    response = client.get("/api/cdr/export")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    
    csv_data = response.data.decode("utf-8")
    reader = csv.reader(StringIO(csv_data))
    rows = list(reader)
    
    # Assert header columns
    headers = rows[0]
    expected_headers = ["Date", "Time", "Order ID", "Phone", "Duration (sec)", "DTMF", "Result", "Charge (BDT)", "Recording URL"]
    assert headers == expected_headers
    
    # Assert data exists
    assert len(rows) == 2
    assert rows[1][2] == "ORD-CSV-1"
    assert rows[1][4] == "65"
    assert rows[1][5] == "1"
    assert rows[1][6] == "completed"
    assert rows[1][7] == "1.30"

def test_csv_export_masks_phone_numbers(client, auth_client, create_tenant, app):
    """Test that customer phone numbers are properly masked in CSV exports."""
    tenant, user = create_tenant("CSV Mask Store", "csv_mask@store.com")
    client = auth_client((tenant, user))

    with app.app_context():
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'CONFIRMED') RETURNING id",
            (tenant["id"], "ORD-CSV-2", "01712345678", "Customer B", Decimal("100.00"))
        )
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant["id"], order_id, "01712345678", "completed", "outbound", 12)
        )
        db.execute(
            "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no, charge) VALUES (%s, %s, %s, 1, 0.80)",
            (tenant["id"], order_id, call_id)
        )

    response = client.get("/api/cdr/export")
    assert response.status_code == 200
    
    csv_data = response.data.decode("utf-8")
    reader = csv.reader(StringIO(csv_data))
    rows = list(reader)
    
    # Phone number is column index 3 (4th column)
    # Original phone: 01712345678 (clean length: 11) -> masked format: clean[:3] + "****" + clean[7:] -> "017****5678"
    assert rows[1][3] == "017****5678"

def test_csv_export_only_returns_own_tenant_records(client, auth_client, create_tenant, app):
    """Test that CSV exports only contain records belonging to the authenticated tenant."""
    tenant_a, user_a = create_tenant("CSV Tenant A", "tenanta@store.com")
    tenant_b, user_b = create_tenant("CSV Tenant B", "tenantb@store.com")

    with app.app_context():
        # Tenant A call
        order_a = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'CONFIRMED') RETURNING id",
            (tenant_a["id"], "ORD-A", "01711111111", "Customer A", Decimal("10.00"))
        )
        call_a = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant_a["id"], order_a, "01711111111", "completed", "outbound", 30)
        )
        db.execute(
            "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no, charge) VALUES (%s, %s, %s, 1, 0.80)",
            (tenant_a["id"], order_a, call_a)
        )

        # Tenant B call
        order_b = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, %s, %s, %s, %s, 'CONFIRMED') RETURNING id",
            (tenant_b["id"], "ORD-B", "01722222222", "Customer B", Decimal("20.00"))
        )
        call_b = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant_b["id"], order_b, "01722222222", "completed", "outbound", 40)
        )
        db.execute(
            "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no, charge) VALUES (%s, %s, %s, 1, 0.80)",
            (tenant_b["id"], order_b, call_b)
        )

    # Login as Tenant A and export
    client_a = auth_client((tenant_a, user_a))
    response_a = client_a.get("/api/cdr/export")
    assert response_a.status_code == 200
    
    reader_a = csv.reader(StringIO(response_a.data.decode("utf-8")))
    rows_a = list(reader_a)
    assert len(rows_a) == 2 # 1 header + 1 data row
    assert rows_a[1][2] == "ORD-A"

    # Login as Tenant B and export
    client_b = auth_client((tenant_b, user_b))
    response_b = client_b.get("/api/cdr/export")
    assert response_b.status_code == 200
    
    reader_b = csv.reader(StringIO(response_b.data.decode("utf-8")))
    rows_b = list(reader_b)
    assert len(rows_b) == 2 # 1 header + 1 data row
    assert rows_b[1][2] == "ORD-B"
