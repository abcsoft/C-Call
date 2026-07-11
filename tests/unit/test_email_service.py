from unittest.mock import patch, MagicMock
from app.services.email_service import (
    send_verification_email,
    send_reset_email,
    send_low_balance_alert,
    send_webhook_failure_alert,
    send_order_unreachable_alert,
    _get_val
)

def test_get_val():
    assert _get_val(None, "key", "def") == "def"
    assert _get_val({"key": "val"}, "key") == "val"
    class Obj:
        pass
    o = Obj()
    o.key = "val2"
    assert _get_val(o, "key") == "val2"

def test_send_emails(app):
    with app.app_context():
        user = {"email": "test@user.com", "username": "testuser"}
        tenant = {
            "email": "merchant@store.com",
            "business_name": "Test Merchant",
            "settings": '{"low_balance_threshold": 100.0}'
        }
        order = {
            "external_order_id": "ORD-123",
            "customer_name": "Customer A",
            "phone": "01712345678"
        }
        
        with patch("app.extensions.mail.send") as mock_send:
            # 1. send_verification_email
            send_verification_email(user, "token123")
            assert mock_send.call_count == 1
            
            # 2. send_reset_email
            send_reset_email(user, "token456")
            assert mock_send.call_count == 2
            
            # 3. send_low_balance_alert
            send_low_balance_alert(tenant, 45.0)
            assert mock_send.call_count == 3
            
            # 4. send_webhook_failure_alert
            send_webhook_failure_alert(tenant, "ORD-123", "http://test.com/wh")
            assert mock_send.call_count == 4
            
            # 5. send_order_unreachable_alert
            send_order_unreachable_alert(tenant, order)
            assert mock_send.call_count == 5
            
        # Test error scenarios
        with patch("app.extensions.mail.send", side_effect=Exception("SMTP error")) as mock_send_err:
            send_verification_email(user, "token123")
            send_reset_email(user, "token456")
            send_low_balance_alert(tenant, 45.0)
            send_webhook_failure_alert(tenant, "ORD-123", "http://test.com/wh")
            send_order_unreachable_alert(tenant, order)
            assert mock_send_err.call_count == 5
