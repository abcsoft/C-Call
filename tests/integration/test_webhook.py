from __future__ import annotations

import hmac
import hashlib
import json
import urllib.request
from unittest.mock import patch, MagicMock
from decimal import Decimal
import pytest
from app.db import db
from app.services import webhook_service

def test_webhook_delivered_after_order_confirmed(app, create_tenant):
    """Test webhook is delivered and logged when an order moves to a terminal status (CONFIRMED)."""
    tenant, user = create_tenant("WH Store 1", "wh1@store.com")
    tenant_uuid = str(tenant["uuid"])

    with app.app_context():
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_url)
            VALUES (%s, 'ORD-WH-1', '01712345678', 'Sujon', 1000.00, 'CONFIRMED', 'http://store.com/wh')
            RETURNING id
            """,
            (tenant["id"],)
        )
        
        # Mock urllib.request.urlopen to return successful 200 response
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"success": true}'
        
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            # Trigger delivery
            success = webhook_service.deliver_webhook(order_id, attempt_no=1)
            assert success is True
            assert mock_urlopen.call_count == 1

        # Check webhook status is updated to sent in DB
        order = db.query_one("SELECT webhook_status FROM orders WHERE id = %s", (order_id,))
        assert order["webhook_status"] == "sent"

        # Check logs
        logs = webhook_service.get_webhook_logs(order_id, tenant["id"])
        assert len(logs) == 1
        assert logs[0]["status_code"] == 200
        assert logs[0]["success"] is True

def test_webhook_payload_contains_correct_fields(app, create_tenant):
    """Test that the webhook payload matches the PRD fields structure."""
    tenant, user = create_tenant("WH Store 2", "wh2@store.com")

    with app.app_context():
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_url)
            VALUES (%s, 'ORD-WH-2', '01712345678', 'Sujon', 100.00, 'CONFIRMED', 'http://store.com/wh')
            RETURNING id
            """,
            (tenant["id"],)
        )
        # Add call attempt details
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, order_id, number, status, direction, talk_seconds) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant["id"], order_id, "01712345678", "completed", "outbound", 25)
        )
        db.execute(
            "INSERT INTO order_call_attempts (tenant_id, order_id, call_id, attempt_no, dtmf_digit, talk_seconds, result) VALUES (%s, %s, %s, 1, '1', 25, 'confirmed')",
            (tenant["id"], order_id, call_id)
        )

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{}'
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            webhook_service.deliver_webhook(order_id, attempt_no=1)
            
            # Verify the post request payload
            called_req = mock_urlopen.call_args[0][0]
            payload = json.loads(called_req.data.decode("utf-8"))
            
            assert payload["event"] == "order.confirmed"
            assert payload["order_id"] == "ORD-WH-2"
            assert payload["internal_id"] == order_id
            assert payload["order_status"] == "CONFIRMED"
            assert payload["dtmf_digit"] == "1"
            assert payload["customer_name"] == "Sujon"
            assert payload["phone"] == "017****5678"
            assert payload["amount"] == "100.00"
            assert payload["talk_seconds"] == 25
            assert payload["attempt_no"] == 1
            assert "timestamp" in payload
            assert "signature" in payload

def test_webhook_hmac_signature_correct(app, create_tenant):
    """Test that the webhook HMAC-SHA256 signature is correct."""
    tenant, user = create_tenant("WH Store 3", "wh3@store.com")
    tenant_uuid = str(tenant["uuid"])

    with app.app_context():
        # Retrieve settings or set custom secret
        db.execute(
            "UPDATE tenants SET settings = '{\"webhook_secret\": \"my_super_secret\"}' WHERE id = %s",
            (tenant["id"],)
        )
        from app.models.tenant import invalidate_tenant_caches
        invalidate_tenant_caches(tenant["id"])

        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_url)
            VALUES (%s, 'ORD-WH-3', '01712345678', 'Sujon', 100.00, 'CONFIRMED', 'http://store.com/wh')
            RETURNING id
            """,
            (tenant["id"],)
        )

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{}'
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            webhook_service.deliver_webhook(order_id, attempt_no=1)
            
            called_req = mock_urlopen.call_args[0][0]
            header_sig = called_req.get_header("X-ccall-signature")
            payload = json.loads(called_req.data.decode("utf-8"))
            
            assert header_sig is not None
            assert header_sig.startswith("sha256=")
            
            # Verify hmac matches
            actual_sig = header_sig.replace("sha256=", "")
            
            # Remove signature key from payload to calculate raw HMAC
            del payload["signature"]
            payload_json = json.dumps(payload, sort_keys=True)
            
            expected_sig = hmac.new(
                b"my_super_secret",
                payload_json.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            
            assert actual_sig == expected_sig

def test_webhook_retried_on_failure(app, create_tenant):
    """Test webhook is retried upon failure using backoff delays."""
    tenant, user = create_tenant("WH Store 4", "wh4@store.com")
    
    with patch("app.services.email_service.send_webhook_failure_alert"), \
         patch("time.sleep") as mock_sleep:

        with app.app_context():
            order_id = db.insert_returning_id(
                """
                INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_url)
                VALUES (%s, 'ORD-WH-4', '01712345678', 'Sujon', 100.00, 'CONFIRMED', 'http://store.com/wh')
                RETURNING id
                """,
                (tenant["id"],)
            )

            # First request fails (500), second succeeds (200)
            mock_resp_fail = MagicMock()
            mock_resp_fail.status = 500
            mock_resp_fail.read.return_value = b'Internal Server Error'

            mock_resp_ok = MagicMock()
            mock_resp_ok.status = 200
            mock_resp_ok.read.return_value = b'{}'
            mock_resp_ok.__enter__.return_value = mock_resp_ok

            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = [
                    urllib.error.HTTPError("http://store.com/wh", 500, "Internal Server Error", {}, None),
                    mock_resp_ok
                ]

                # Trigger delivery async wrapper or direct retry scheduler loop
                webhook_service._deliver_with_retry(order_id)

                # Urlopen called twice, sleep called once (for 60s)
                assert mock_urlopen.call_count == 2
                assert mock_sleep.call_count == 1
                mock_sleep.assert_called_with(60)

            # Order status should be sent in DB
            order = db.query_one("SELECT webhook_status FROM orders WHERE id = %s", (order_id,))
            assert order["webhook_status"] == "sent"

def test_webhook_marked_failed_after_3_attempts(app, create_tenant):
    """Test webhook is marked failed in the DB after maximum attempts are exhausted."""
    tenant, user = create_tenant("WH Store 5", "wh5@store.com")
    
    with patch("app.services.email_service.send_webhook_failure_alert") as mock_alert, \
         patch("time.sleep") as mock_sleep:

        with app.app_context():
            order_id = db.insert_returning_id(
                """
                INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_url)
                VALUES (%s, 'ORD-WH-5', '01712345678', 'Sujon', 100.00, 'CONFIRMED', 'http://store.com/wh')
                RETURNING id
                """,
                (tenant["id"],)
            )

            # Mock all 3 requests failing (500)
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = urllib.error.HTTPError("http://store.com/wh", 500, "Internal Server Error", {}, None)

                webhook_service._deliver_with_retry(order_id)

                assert mock_urlopen.call_count == 3
                assert mock_sleep.call_count == 2 # 60s, then 300s
                assert mock_alert.call_count == 1 # Alert email sent

            # Order webhook status should be failed
            order = db.query_one("SELECT webhook_status FROM orders WHERE id = %s", (order_id,))
            assert order["webhook_status"] == "failed"

def test_webhook_not_delivered_to_wrong_tenant(app, create_tenant):
    """Test webhook log query tenant isolation bounds."""
    tenant_a, user_a = create_tenant("WH Tenant A", "wh_ta@store.com")
    tenant_b, user_b = create_tenant("WH Tenant B", "wh_tb@store.com")

    with app.app_context():
        # Setup Tenant A order
        order_a = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_url)
            VALUES (%s, 'ORD-A', '01711111111', 'Sakib', 100.00, 'CONFIRMED', 'http://store.com/wh')
            RETURNING id
            """,
            (tenant_a["id"],)
        )
        db.execute(
            """
            INSERT INTO webhook_logs (tenant_id, order_id, url, payload, status_code, response_body, success, attempt_no)
            VALUES (%s, %s, %s, '{}', 200, 'ok', true, 1)
            """,
            (tenant_a["id"], order_a, "http://store.com/wh")
        )

    with app.app_context():
        # Fetching logs using Tenant B context should return empty list
        logs_b = webhook_service.get_webhook_logs(order_a, tenant_b["id"])
        assert len(logs_b) == 0

        # Fetching logs using Tenant A context returns list
        logs_a = webhook_service.get_webhook_logs(order_a, tenant_a["id"])
        assert len(logs_a) == 1
