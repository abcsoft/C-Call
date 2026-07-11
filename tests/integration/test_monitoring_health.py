# tests/integration/test_monitoring_health.py
from __future__ import annotations

import json
from app import scrub_sensitive_data
from app.__init__ import phone_masking_processor

def test_liveness_probe(client):
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}

def test_readiness_probe(client):
    # Readiness probe checks DB and Redis (both are configured in test suite)
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}

def test_detailed_health_check(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json
    assert data["status"] == "ok"
    assert "timestamp" in data
    assert "checks" in data
    assert "database" in data["checks"]
    assert "redis" in data["checks"]
    assert "scheduler" in data["checks"]
    assert "sip_providers" in data["checks"]
    assert "version" in data
    assert "uptime_seconds" in data

def test_metrics_endpoint(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    text = response.data.decode("utf-8")
    assert "ccall_calls_total" in text
    assert "ccall_active_calls" in text
    assert "ccall_call_duration_seconds" in text
    assert "ccall_tts_generation_seconds" in text
    assert "ccall_wallet_balance_bdt" in text
    assert "ccall_api_requests_total" in text
    assert "ccall_retry_queue_size" in text

def test_scrub_sensitive_data():
    event = {
        "extra": {"some_key": "some_value"},
        "request": {
            "url": "http://localhost/api/v1/orders",
            "headers": {
                "Authorization": "Bearer confidential_token",
                "X-API-Key": "my-secret-key-12345",
                "Accept": "application/json"
            }
        },
        "logentry": {
            "message": "Calling 01712345678 or +8801812345678"
        }
    }
    scrubbed = scrub_sensitive_data(event, None)
    
    # Headers should be removed
    headers = scrubbed["request"]["headers"]
    assert "Authorization" not in headers
    assert "X-API-Key" not in headers
    assert headers["Accept"] == "application/json"
    
    # Phone numbers should be scrubbed
    message = scrubbed["logentry"]["message"]
    assert "01712345678" not in message
    assert "01812345678" not in message
    assert "[SCRUBBED_PHONE]" in message

def test_phone_masking_in_logs():
    event_dict = {
        "event": "Dispatching call to 01712345678 and +8801987654321, check 8801512345678",
        "phone": "01712345678",
        "nested": {
            "phone": "01512345678"
        }
    }
    
    processed = phone_masking_processor(None, None, event_dict)
    assert processed["event"] == "Dispatching call to 017****5678 and +88019****4321, check 88015****5678"
    assert processed["phone"] == "017****5678"
    assert processed["nested"]["phone"] == "015****5678"
