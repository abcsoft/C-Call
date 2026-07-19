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

def test_health_exposes_call_queue_breakdown(client, app):
    """Health endpoint must include call_queue with queued/leased/retry_wait/expired_leases."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    checks = response.json["checks"]
    assert "call_queue" in checks, (
        f"Expected 'call_queue' in checks; got keys: {list(checks.keys())}"
    )
    q = checks["call_queue"]
    for key in ("queued", "leased", "retry_wait", "expired_leases"):
        assert key in q, f"Missing key '{key}' in call_queue: {q}"
        assert isinstance(q[key], int), f"call_queue['{key}'] must be int, got {type(q[key])}"


def test_health_exposes_heartbeats_section(client, app, db_conn):
    """Health endpoint must include a heartbeats section."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    checks = response.json["checks"]
    assert "heartbeats" in checks, (
        f"Expected 'heartbeats' in checks; got keys: {list(checks.keys())}"
    )
    # Empty dict is valid (no workers running) — but must not be the string "unavailable".
    hb = checks["heartbeats"]
    assert isinstance(hb, dict), f"heartbeats must be a dict, got {type(hb)}: {hb}"


def test_health_heartbeat_fresh_not_stale(client, app, db_conn):
    """A freshly-written heartbeat row must appear as stale=False."""
    import datetime
    with app.app_context():
        from app.db import db
        db.execute(
            """INSERT INTO service_heartbeats (service_name, instance_id, last_seen_at, status)
               VALUES ('worker', 'test-worker-fresh', now(), 'ok')
               ON CONFLICT (service_name, instance_id) DO UPDATE
                   SET last_seen_at = now(), status = 'ok'""",
        )

    try:
        response = client.get("/api/v1/health")
        checks = response.json["checks"]
        workers = checks.get("heartbeats", {}).get("worker", [])
        fresh = [w for w in workers if w["instance_id"] == "test-worker-fresh"]
        assert fresh, "Fresh worker heartbeat not found in health response"
        assert not fresh[0]["stale"], (
            f"Fresh heartbeat must not be stale: {fresh[0]}"
        )
    finally:
        with app.app_context():
            from app.db import db
            db.execute(
                "DELETE FROM service_heartbeats WHERE service_name='worker' "
                "AND instance_id='test-worker-fresh'"
            )


def test_health_heartbeat_stale_flagged(client, app, db_conn):
    """A heartbeat older than 2 minutes must appear as stale=True."""
    with app.app_context():
        from app.db import db
        db.execute(
            """INSERT INTO service_heartbeats (service_name, instance_id, last_seen_at, status)
               VALUES ('worker', 'test-worker-stale', now() - interval '5 minutes', 'ok')
               ON CONFLICT (service_name, instance_id) DO UPDATE
                   SET last_seen_at = now() - interval '5 minutes', status = 'ok'""",
        )

    try:
        response = client.get("/api/v1/health")
        checks = response.json["checks"]
        workers = checks.get("heartbeats", {}).get("worker", [])
        stale = [w for w in workers if w["instance_id"] == "test-worker-stale"]
        assert stale, "Stale worker heartbeat not found in health response"
        assert stale[0]["stale"], (
            f"5-minute-old heartbeat must be stale=True: {stale[0]}"
        )
    finally:
        with app.app_context():
            from app.db import db
            db.execute(
                "DELETE FROM service_heartbeats WHERE service_name='worker' "
                "AND instance_id='test-worker-stale'"
            )


def test_health_expired_lease_count(client, app, db_conn):
    """Health endpoint must count expired leases (leased jobs past their lease_expires_at)."""
    import uuid
    import datetime

    uid = uuid.uuid4().hex[:8]
    with app.app_context():
        from app.db import db
        # Insert a tenant so we can create a leased call_job.
        tenant_row = db.query_one(
            "INSERT INTO tenants (business_name, email, status, settings) "
            "VALUES (%s, %s, 'active', '{}') RETURNING id",
            (f"Health Test {uid}", f"health-{uid}@test.internal"),
        )
        tenant_id = int(tenant_row["id"])
        order_row = db.query_one(
            """INSERT INTO orders (tenant_id, external_order_id, phone, customer_name,
                                   amount, order_status, attempt_count, max_attempts)
               VALUES (%s, %s, %s, %s, %s, 'CALL_QUEUED', 0, 1) RETURNING id""",
            (tenant_id, f"H-{uid}", "01700000001", "Health", "10.00"),
        )
        order_id = int(order_row["id"])
        attempt_row = db.query_one(
            """INSERT INTO order_call_attempts (order_id, tenant_id, attempt_no, created_at)
               VALUES (%s, %s, 1, now()) RETURNING id""",
            (order_id, tenant_id),
        )
        attempt_id = int(attempt_row["id"] if isinstance(attempt_row, dict) else attempt_row[0])
        db.execute(
            """INSERT INTO call_jobs
                   (order_id, tenant_id, attempt_no, job_status, job_type,
                    available_at, lease_expires_at, leased_by, max_attempts,
                    order_call_attempt_id, updated_at)
               VALUES (%s, %s, 1, 'leased', 'order_call',
                       now(), now() - interval '1 minute', 'expired-worker',
                       1, %s, now())""",
            (order_id, tenant_id, attempt_id),
        )

    try:
        response = client.get("/api/v1/health")
        q = response.json["checks"].get("call_queue", {})
        assert isinstance(q.get("expired_leases"), int), (
            f"expired_leases must be int, got: {q.get('expired_leases')}"
        )
        assert q["expired_leases"] >= 1, (
            f"Expected >= 1 expired lease after inserting one, got {q['expired_leases']}"
        )
    finally:
        with app.app_context():
            from app.db import db
            db.execute("DELETE FROM call_jobs WHERE tenant_id = %s", (tenant_id,))
            db.execute("DELETE FROM order_call_attempts WHERE tenant_id = %s", (tenant_id,))
            db.execute("DELETE FROM orders WHERE tenant_id = %s", (tenant_id,))
            db.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))


def test_health_empty_queue(client, app):
    """call_queue counts must be integers (≥ 0) even when no jobs exist."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    q = response.json["checks"].get("call_queue", {})
    assert isinstance(q, dict), f"call_queue must be a dict, got: {q}"
    # All counts are non-negative integers.
    for key in ("queued", "leased", "retry_wait", "expired_leases"):
        assert isinstance(q.get(key), int) and q.get(key) >= 0, (
            f"call_queue['{key}'] must be a non-negative int, got {q.get(key)!r}"
        )


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
