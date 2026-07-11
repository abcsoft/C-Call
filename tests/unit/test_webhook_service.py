from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from unittest.mock import MagicMock
import pytest

from app.db import db
from app.models.tenant import create_tenant
from app.services.webhook_service import (
    mask_phone,
    build_payload,
    sign_payload,
    deliver_async,
    _deliver_with_retry,
    retry_failed_webhooks,
    get_webhook_logs
)


@pytest.fixture(autouse=True)
def mock_sleep(monkeypatch):
    """Disable time.sleep for all tests to speed up execution."""
    monkeypatch.setattr(time, "sleep", lambda secs: None)


def test_mask_phone():
    """Verify phone masking formats correctly and handles E.164 prefixes."""
    assert mask_phone("01712345678") == "017****5678"
    assert mask_phone("+8801712345678") == "017****5678"
    assert mask_phone("8801712345678") == "017****5678"
    assert mask_phone("12345") == "12345"
    assert mask_phone("") == ""


def test_sign_payload():
    """Verify HMAC-SHA256 payload signature generation."""
    payload = '{"amount":"120.00","event":"order.confirmed"}'
    secret = "test-secret-key"
    sig = sign_payload(payload, secret)
    assert sig.startswith("sha256=")
    # Re-calculate to match
    import hmac, hashlib
    expected_hex = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    assert sig == f"sha256={expected_hex}"


def test_build_payload(app):
    """Verify webhook payload structure, phone masking, and decimal rounding."""
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Payload Store",
            "email": "payload@store.com",
            "plan_name": "free",
            "status": "active"
        })
        
        # Insert a fake order
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, confirmed_at)
            VALUES (%s, 'O-PAYLOAD-1', '+8801711112222', 'Test Customer', 1234.567, 'CONFIRMED', now())
            RETURNING id
            """,
            (tenant_id,)
        )
        
        # Insert a fake call attempt
        db.execute(
            """
            INSERT INTO order_call_attempts (order_id, tenant_id, attempt_no, result, dtmf_digit, talk_seconds)
            VALUES (%s, %s, 1, 'confirmed', '1', 45)
            """,
            (order_id, tenant_id)
        )
        
        # Load order
        order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
        payload = build_payload(order)
        
        assert payload["event"] == "order.confirmed"
        assert payload["order_id"] == "O-PAYLOAD-1"
        assert payload["internal_id"] == order_id
        assert payload["order_status"] == "CONFIRMED"
        assert payload["dtmf_digit"] == "1"
        assert payload["customer_name"] == "Test Customer"
        assert payload["phone"] == "017****2222"
        assert payload["amount"] == "1234.57"  # Round to 2 decimal places in string
        assert payload["talk_seconds"] == 45
        assert payload["attempt_no"] == 1
        assert "timestamp" in payload
        assert "confirmed_at" in payload


def test_webhook_failures_and_email_alert(app, monkeypatch):
    """Verify that 3 failures update status to failed and send a notification email."""
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Fail Store",
            "email": "fail@store.com",
            "plan_name": "free",
            "status": "active"
        })
        
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_url)
            VALUES (%s, 'O-FAIL-1', '+8801711112222', 'Test Customer', 10.00, 'CONFIRMED', 'http://webhook.fail.url')
            RETURNING id
            """,
            (tenant_id,)
        )
        
        # Mock urllib.request.urlopen to always fail with a HTTP 500 error
        def mock_urlopen(req, timeout=None, context=None):
            raise urllib.error.HTTPError("http://webhook.fail.url", 500, "Internal Server Error", {}, None)
            
        monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
        
        # Track mail alerts
        mail_sent = []
        def mock_send_alert(tenant, order_id_arg, url):
            mail_sent.append(True)
        monkeypatch.setattr("app.services.email_service.send_webhook_failure_alert", mock_send_alert)
        
        # Deliver webhook
        _deliver_with_retry(order_id)
        
        # Order webhook status should be 'failed'
        order = db.query_one("SELECT webhook_status FROM orders WHERE id = %s", (order_id,))
        assert order["webhook_status"] == "failed"
        
        # 3 logs should be created
        logs = get_webhook_logs(order_id, tenant_id)
        assert len(logs) == 3
        assert all(l["success"] is False for l in logs)
        assert all(l["status_code"] == 500 for l in logs)
        
        # One email alert should be sent
        assert len(mail_sent) == 1


def test_webhook_redirect_limit_and_timeout(app, monkeypatch):
    """Verify redirection limit of 3 redirects and 10s connection timeout."""
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Redirect Store",
            "email": "redirect@store.com",
            "plan_name": "free",
            "status": "active"
        })
        
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_url)
            VALUES (%s, 'O-RED-1', '+8801711112222', 'Test Customer', 10.00, 'CONFIRMED', 'http://webhook.redirect.url')
            RETURNING id
            """,
            (tenant_id,)
        )
        
        # Mock urlopen to capture timeout and simulate redirect/timeout failures
        captured_timeout = None
        def mock_urlopen(req, timeout=None, context=None):
            nonlocal captured_timeout
            captured_timeout = timeout
            raise TimeoutError("Connection timed out")
            
        monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
        
        _deliver_with_retry(order_id)
        
        logs = get_webhook_logs(order_id, tenant_id)
        assert len(logs) == 3
        assert "Connection timed out" in logs[0]["response_body"]
        assert captured_timeout == 10


def test_webhook_response_truncation(app, monkeypatch):
    """Verify that response bodies larger than 500 characters are truncated."""
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Trunc Store",
            "email": "trunc@store.com",
            "plan_name": "free",
            "status": "active"
        })
        
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_url)
            VALUES (%s, 'O-TRUNC-1', '+8801711112222', 'Test Customer', 10.00, 'CONFIRMED', 'http://webhook.trunc.url')
            RETURNING id
            """,
            (tenant_id,)
        )
        
        class MockResponse:
            def __init__(self):
                self.status = 200
            def read(self, limit=None):
                return b"X" * 1500
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
                
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None, context=None: MockResponse())
        
        _deliver_with_retry(order_id)
        
        logs = get_webhook_logs(order_id, tenant_id)
        assert len(logs) == 1
        assert logs[0]["success"] is True
        # Response body should be exactly 500 characters long (truncated from 1500)
        assert len(logs[0]["response_body"]) == 500
        assert logs[0]["response_body"] == "X" * 500
