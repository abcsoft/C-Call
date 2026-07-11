import os
import time
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from app.db import db
from app.services.recording_service import get_signed_url, verify_signed_url
from app.services.transcript_service import get_call_transcript_timeline


def test_recording_service_signing_and_verification(app):
    with app.app_context():
        # Setup using database directly
        tenant_id = db.insert_returning_id(
            "INSERT INTO tenants (business_name, email) VALUES ('Signing Tenant', 'sig@store.com') RETURNING id"
        )
        
        # Create a call with recording path
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, number, status, recording_path) VALUES (%s, '01712345678', 'completed', 'instance/recordings/test_rec.wav') RETURNING id",
            (tenant_id,)
        )
        
        # 1. Sign URL
        signed_url = get_signed_url("instance/recordings/test_rec.wav", tenant_id)
        assert "/recordings/play?path=" in signed_url
        assert "&sig=" in signed_url
        assert "&exp=" in signed_url
        
        # Parse parameters
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(signed_url)
        params = parse_qs(parsed.query)
        path_param = params["path"][0]
        sig_param = params["sig"][0]
        exp_param = params["exp"][0]
        
        assert path_param == "instance/recordings/test_rec.wav"
        
        # 2. Verify Valid Signature
        assert verify_signed_url(path_param, sig_param, exp_param) is True
        
        # 3. Verify Expired Signature
        assert verify_signed_url(path_param, sig_param, int(time.time()) - 10) is False
        
        # 4. Verify Invalid Signature
        assert verify_signed_url(path_param, "invalidsig", exp_param) is False
        assert verify_signed_url("another_path.wav", sig_param, exp_param) is False


def test_recording_service_tenant_isolation(app):
    with app.app_context():
        tenant_a = db.insert_returning_id(
            "INSERT INTO tenants (business_name, email) VALUES ('Tenant A', 'a@store.com') RETURNING id"
        )
        tenant_b = db.insert_returning_id(
            "INSERT INTO tenants (business_name, email) VALUES ('Tenant B', 'b@store.com') RETURNING id"
        )
        
        # Create call for Tenant A
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, number, status, recording_path) VALUES (%s, '01712345678', 'completed', 'instance/recordings/tenant_a_rec.wav') RETURNING id",
            (tenant_a,)
        )
        
        # Attempting to sign Tenant A's recording with Tenant B's credentials must raise PermissionError
        with pytest.raises(PermissionError) as exc_info:
            get_signed_url("instance/recordings/tenant_a_rec.wav", tenant_b)
        assert "Access Denied" in str(exc_info.value)


def test_recording_play_endpoint(app, client, create_tenant, auth_client):
    tenant, user = create_tenant("Play Store", "play@store.com")
    auth_client((tenant, user))
    
    with app.app_context():
        tenant_id = tenant["id"]
        
        # Ensure target recordings directory exists and write dummy WAV content
        os.makedirs("instance/recordings", exist_ok=True)
        dummy_filepath = "instance/recordings/dummy_test_play.wav"
        with open(dummy_filepath, "wb") as f:
            f.write(b"RIFFdummywaveheaderandcontent")
            
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, number, status, recording_path) VALUES (%s, '01712345678', 'completed', %s) RETURNING id",
            (tenant_id, dummy_filepath)
        )
        
        # Generate signed URL
        signed_url = get_signed_url(dummy_filepath, tenant_id)
        
    try:
        # GET signed URL via client
        response = client.get(signed_url)
        assert response.status_code == 200
        assert response.data == b"RIFFdummywaveheaderandcontent"
        assert response.mimetype == "audio/wav"
        response.close()
        
        # Test download parameter forces attachment disposition
        download_response = client.get(signed_url + "&download=1")
        assert download_response.status_code == 200
        assert "attachment" in download_response.headers["Content-Disposition"]
        assert "dummy_test_play.wav" in download_response.headers["Content-Disposition"]
        download_response.close()
        
        # Test request without valid signature is denied (403)
        bad_url = signed_url.replace("&sig=", "&sig=bad")
        bad_response = client.get(bad_url)
        assert bad_response.status_code == 403
        bad_response.close()
    finally:
        # Cleanup
        if os.path.exists(dummy_filepath):
            try:
                os.remove(dummy_filepath)
            except Exception:
                pass


def test_recording_signed_url_htmx_endpoint(app, client, create_tenant, auth_client):
    tenant, user = create_tenant("HTMX Play Store", "htmx_play@store.com")
    auth_client((tenant, user))
    
    with app.app_context():
        tenant_id = tenant["id"]
        
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount) VALUES (%s, 'HTMX-101', '01999999999', 'Rony', 120.00) RETURNING id",
            (tenant_id,)
        )
        
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, order_id, number, status, recording_path, talk_seconds) VALUES (%s, %s, '01999999999', 'completed', 'instance/recordings/test.wav', 12) RETURNING id",
            (tenant_id, order_id)
        )
        
        attempt_id = db.insert_returning_id(
            "INSERT INTO order_call_attempts (order_id, tenant_id, call_id, attempt_no, result) VALUES (%s, %s, %s, 1, 'confirmed') RETURNING id",
            (order_id, tenant_id, call_id)
        )
        
    # Request dynamic signed URL endpoint
    response = client.get(f"/api/recordings/{attempt_id}/url")
    assert response.status_code == 200
    html_content = response.data.decode("utf-8")
    
    # Verify HTML fragment contains audio element and download link pointing to signed route
    assert "<audio" in html_content
    assert "/recordings/play?path=" in html_content
    assert "&sig=" in html_content
    assert "Download" in html_content


def test_transcript_timeline_generation(app):
    with app.app_context():
        tenant_id = db.insert_returning_id(
            "INSERT INTO tenants (business_name, email) VALUES ('Transcript Store', 'trans@store.com') RETURNING id"
        )
        
        call_id = db.insert_returning_id(
            "INSERT INTO calls (tenant_id, number, status) VALUES (%s, '01712345678', 'completed') RETURNING id",
            (tenant_id,)
        )
        
        # Insert timeline events
        db.execute(
            """
            INSERT INTO call_events (call_id, seq, event_type, digit, at_offset_ms, detail) VALUES 
            (%s, 1, 'answered', '', 5000, 'Answered'),
            (%s, 2, 'dtmf', '1', 12300, 'Confirmation key'),
            (%s, 3, 'hangup', '', 15100, 'Ended')
            """,
            (call_id, call_id, call_id)
        )
        
        timeline = get_call_transcript_timeline(call_id)
        
        assert len(timeline) == 3
        assert timeline[0] == "00:05 — Call answered"
        assert timeline[1] == "00:12 — Customer pressed: 1 (Confirm)"
        assert timeline[2] == "00:15 — Call ended"
