import hmac
import hashlib
import json
from unittest.mock import patch, MagicMock
import urllib.request
from app.db import db
from app.models.tenant import create_tenant
from app.services.webhook_service import deliver_webhook

def test_webhook_signature_and_delivery_logging(app):
    with app.app_context():
        # Setup tenant
        tenant_id = create_tenant("Webhook Store", "wh@store.com")
        tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
        tenant_uuid = str(tenant["uuid"])
        
        # Insert mock order
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, webhook_url)
            VALUES (%s, 'WC-999', '01712345678', 'Sakib', 1200.0, 'CONFIRMED', 'http://merchant.com/webhook')
            RETURNING id
            """,
            (tenant_id,)
        )
        
        # Mock urllib.request.urlopen
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"status": "received"}'
        
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value = mock_response
            
            # Deliver webhook
            success = deliver_webhook(order_id, attempt_no=1)
            
            assert success is True
            assert mock_urlopen.called
            
            # Inspect the request sent
            called_req = mock_urlopen.call_args[0][0]
            assert isinstance(called_req, urllib.request.Request)
            assert called_req.full_url == "http://merchant.com/webhook"
            assert called_req.get_header("Content-type") == "application/json"
            
            # Verify body payload and signature
            req_body = called_req.data
            payload = json.loads(req_body.decode("utf-8"))
            
            # Verify the signature field in the request body
            assert "signature" in payload
            sig_value = payload["signature"]
            assert sig_value.startswith("sha256=")
            
            # Clean the signature key from payload to verify HMAC matching
            actual_sig = sig_value.replace("sha256=", "")
            del payload["signature"]
            
            # Re-generate HMAC and verify
            recomputed_json = json.dumps(payload, sort_keys=True)
            expected_sig = hmac.new(
                tenant_uuid.encode("utf-8"),
                recomputed_json.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            
            assert actual_sig == expected_sig

            # Verify log entry in DB
            log = db.query_one("SELECT * FROM webhook_logs WHERE order_id = %s", (order_id,))
            assert log is not None
            assert log["status_code"] == 200
            assert log["success"] is True
            assert log["attempt_no"] == 1
            assert "received" in log["response_body"]
