from __future__ import annotations

import csv
from io import StringIO
import datetime
from decimal import Decimal
import pytest
from app.db import db

def test_orders_list_and_filters(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Filter Store", "filter@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]

        # Insert some orders with different statuses and dates
        db.execute(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, created_at)
            VALUES 
                (%s, 'ORD-X1', '+8801700000001', 'Alice', 100.0, 'CONFIRMED', now()),
                (%s, 'ORD-X2', '+8801700000002', 'Bob', 200.0, 'CANCELLED', now() - interval '1 day'),
                (%s, 'ORD-X3', '+8801700000003', 'Charlie', 300.0, 'RETRY_SCHEDULED', now() - interval '2 days')
            """,
            (tenant_id, tenant_id, tenant_id)
        )

        # 1. Test all orders
        res = client.get("/orders")
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert "ORD-X1" in html
        assert "ORD-X2" in html
        assert "ORD-X3" in html

        # 2. Test status filter
        res_status = client.get("/orders?status=Confirmed")
        assert res_status.status_code == 200
        html_status = res_status.get_data(as_text=True)
        assert "ORD-X1" in html_status
        assert "ORD-X2" not in html_status

        # 3. Test search filter
        res_search = client.get("/orders?search=Bob")
        assert res_search.status_code == 200
        html_search = res_search.get_data(as_text=True)
        assert "ORD-X2" in html_search
        assert "ORD-X1" not in html_search

        # 4. Test date filter
        today_str = datetime.date.today().isoformat()
        res_date = client.get(f"/orders?date_from={today_str}&date_to={today_str}")
        assert res_date.status_code == 200
        html_date = res_date.get_data(as_text=True)
        assert "ORD-X1" in html_date
        assert "ORD-X2" not in html_date


def test_cursor_based_pagination(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Paging Store", "paging@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]

        # Insert 5 orders to test paging limit=2
        # Cursors must order correctly by created_at DESC, id DESC
        for i in range(1, 6):
            db.execute(
                """
                INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, created_at)
                VALUES (%s, %s, %s, %s, %s, 'CONFIRMED', now() - interval '1 hour' * %s)
                """,
                (tenant_id, f"ORD-{i}", f"+880170000000{i}", f"User {i}", 100.0 * i, i)
            )

        # First page (limit=2)
        res_first = client.get("/orders?limit=2")
        assert res_first.status_code == 200
        html_first = res_first.get_data(as_text=True)
        assert "ORD-1" in html_first
        assert "ORD-2" in html_first
        assert "ORD-3" not in html_first

        # Extract last order ID on first page
        first_page_orders = db.query(
            "SELECT id FROM orders WHERE tenant_id = %s ORDER BY created_at DESC, id DESC LIMIT 2",
            (tenant_id,)
        )
        assert len(first_page_orders) == 2
        last_id = first_page_orders[-1]["id"]
        first_id = first_page_orders[0]["id"]

        # Next page (after last_id)
        res_next = client.get(f"/orders?limit=2&after={last_id}&offset=2")
        assert res_next.status_code == 200
        html_next = res_next.get_data(as_text=True)
        assert "ORD-3" in html_next
        assert "ORD-4" in html_next
        assert "ORD-1" not in html_next

        # Prev page (before first_id of second page)
        second_page_orders = db.query(
            "SELECT id FROM orders WHERE tenant_id = %s AND (created_at, id) < (SELECT created_at, id FROM orders WHERE id = %s) ORDER BY created_at DESC, id DESC LIMIT 2",
            (tenant_id, last_id)
        )
        assert len(second_page_orders) == 2
        prev_target_id = second_page_orders[0]["id"]

        res_prev = client.get(f"/orders?limit=2&before={prev_target_id}&offset=0")
        assert res_prev.status_code == 200
        html_prev = res_prev.get_data(as_text=True)
        assert "ORD-1" in html_prev
        assert "ORD-2" in html_prev


def test_order_detail_view_and_polling(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Detail Store", "detail@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]

        # Insert order
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_status, webhook_url)
            VALUES (%s, 'ORD-DET', '+8801700000999', 'Robi', 1500.0, 'CALLING', 'failed', 'https://webhook.site/test') RETURNING id
            """,
            (tenant_id,)
        )

        # Insert call record
        call_id = db.insert_returning_id(
            """
            INSERT INTO calls (tenant_id, order_id, number, status, recording_path, talk_seconds, started_at, ended_at)
            VALUES (%s, %s, '+8801700000999', 'completed', '/var/spool/asterisk/monitor/rec-xyz.wav', 45, now() - interval '10 minutes', now() - interval '9 minutes') RETURNING id
            """,
            (tenant_id, order_id)
        )

        # Insert attempt
        db.execute(
            """
            INSERT INTO order_call_attempts (order_id, tenant_id, attempt_no, call_id, result, dtmf_digit, talk_seconds, charge, started_at, ended_at)
            VALUES (%s, %s, 1, %s, 'confirmed', '1', 45, 0.93, now() - interval '10 minutes', now() - interval '9 minutes')
            """,
            (order_id, tenant_id, call_id)
        )

        # 1. GET detail view
        res = client.get("/orders/ORD-DET")
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert "ORD-DET" in html
        assert "Robi" in html
        assert "Attempt #1" in html
        assert "☎ 1" in html
        assert "rec-xyz.wav" in html
        assert "Failed ✗" in html
        assert "hx-trigger=\"every 5s\"" in html  # Verify it is polling since status=CALLING

        # 2. GET polling status endpoint
        res_status = client.get(f"/api/orders/{order_id}/status")
        assert res_status.status_code == 200
        json_data = res_status.get_json()
        assert json_data["success"] is True
        assert json_data["status"] == "CALLING"
        assert json_data["webhook_status"] == "failed"


def test_order_recall_action(auth_client, create_tenant, monkeypatch, app):
    with app.app_context():
        tenant, user = create_tenant("Recall Store", "recall@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]

        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status)
            VALUES (%s, 'ORD-REC', '+8801700000888', 'Farhan', 500.0, 'NO_ANSWER') RETURNING id
            """,
            (tenant_id,)
        )

        recall_triggered = False
        def mock_manual_recall(oid, tid):
            nonlocal recall_triggered
            assert oid == order_id
            assert tid == tenant_id
            recall_triggered = True
            return 999

        monkeypatch.setattr("app.blueprints.customer.orders.manual_recall", mock_manual_recall)

        res = client.post("/orders/ORD-REC/recall")
        assert res.status_code == 302
        assert recall_triggered is True


def test_csv_export(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Export Store", "export@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]

        db.execute(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_status)
            VALUES 
                (%s, 'ORD-E1', '+8801700000555', 'Hasan', 100.0, 'CONFIRMED', 'sent'),
                (%s, 'ORD-E2', '+8801700000666', 'Mitu', 200.0, 'CANCELLED', 'failed')
            """,
            (tenant_id, tenant_id)
        )

        # Export all
        res = client.get("/api/orders/export")
        assert res.status_code == 200
        assert res.headers["Content-Type"] == "text/csv"
        assert "attachment; filename=ccall_orders.csv" in res.headers["Content-Disposition"]
        
        csv_data = res.get_data(as_text=True)
        # Parse CSV
        f = StringIO(csv_data)
        reader = csv.reader(f)
        rows = list(reader)
        
        assert rows[0] == ["Order ID", "Customer Name", "Phone", "COD Amount", "Status", "Attempt Count", "Created At", "Confirmed At", "Webhook Status"]
        assert len(rows) == 3  # Header + 2 data rows
        
        # Verify phone number masking in CSV
        phone_col_idx = 2
        assert rows[1][phone_col_idx] == "+88017****0555" or rows[1][phone_col_idx] == "+88****0555" or "****" in rows[1][phone_col_idx]


def test_cancel_retry_and_webhook_retry_apis(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("API Actions Store", "apiact@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]

        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_status)
            VALUES (%s, 'ORD-API', '+8801700000444', 'Nabil', 450.00, 'RETRY_SCHEDULED', 'failed') RETURNING id
            """,
            (tenant_id,)
        )

        # 1. Test POST /api/orders/<id>/retry/cancel
        res_cancel = client.post(f"/api/orders/{order_id}/retry/cancel")
        assert res_cancel.status_code == 200
        assert res_cancel.get_json()["success"] is True
        
        # Verify DB status updated to CANCELLED
        updated_order = db.query_one("SELECT order_status, next_retry_at FROM orders WHERE id = %s", (order_id,))
        assert updated_order["order_status"] == "CANCELLED"
        assert updated_order["next_retry_at"] is None

        # 2. Test POST /api/orders/<id>/webhook/retry
        # We can trigger it and check if it runs successfully (runs async in background thread, so returns immediately)
        res_webhook = client.post(f"/api/orders/{order_id}/webhook/retry")
        assert res_webhook.status_code == 200
        assert res_webhook.get_json()["success"] is True


def test_serve_recording_security_isolation(auth_client, create_tenant, app):
    with app.app_context():
        # Tenant 1: owner of recording
        tenant1, user1 = create_tenant("Owner Store", "owner@store.com")
        client = auth_client((tenant1, user1))
        tenant1_id = tenant1["id"]

        # Insert call record for Tenant 1
        call_id = db.insert_returning_id(
            """
            INSERT INTO calls (tenant_id, number, status, recording_path)
            VALUES (%s, '+8801700000123', 'completed', '/var/spool/asterisk/monitor/tenant1-rec.wav') RETURNING id
            """,
            (tenant1_id,)
        )

        # Log out tenant 1 first
        client.get("/auth/logout")

        # Tenant 2: attacker trying to download Tenant 1's recording
        tenant2, user2 = create_tenant("Attacker Store", "attacker@store.com")
        client = auth_client((tenant2, user2))

        # 1. Attacker (Tenant 2) tries to access Tenant 1's recording
        res_attacker = client.get("/recordings/tenant1-rec.wav")
        assert res_attacker.status_code == 403

        # Log out tenant 2 and log back in as tenant 1 (owner)
        client.get("/auth/logout")
        client = auth_client((tenant1, user1))

        # 2. Owner (Tenant 1) tries to access (should pass lookup but fail with 404 since physical file doesn't exist)
        res_owner = client.get("/recordings/tenant1-rec.wav")
        assert res_owner.status_code == 404
