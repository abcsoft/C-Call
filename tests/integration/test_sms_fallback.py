import json
from decimal import Decimal
from unittest.mock import patch, MagicMock
from app.db import db
from app.models.tenant import create_tenant, update_tenant_settings
from app.services.sms_service import send_sms, send_order_fallback_sms
from app.services.retry_service import schedule_retry
from app.models.wallet import get_wallet, credit_balance


def test_send_sms_tenant_key(app):
    with app.app_context():
        # Setup tenant with custom key
        tenant_id = create_tenant("SMS Tenant A", "sms_a@store.com")
        update_tenant_settings(tenant_id, {
            "smslayer_api_key": "tenant-custom-api-key",
            "enable_sms_fallback": True,
            "sms_template": "Order #{order_id} - amount {amount}"
        })
        
        # Mock requests.post
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message_id": "msg_12345", "cost": 0.05}
        
        with patch("requests.post") as mock_post:
            mock_post.return_value = mock_response
            
            res = send_sms("01712345678", "Test custom SMS", tenant_id, order_id=None)
            
            assert res["success"] is True
            assert res["message_id"] == "msg_12345"
            
            # Verify HTTP headers & body
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert kwargs["headers"]["Authorization"] == "Bearer tenant-custom-api-key"
            assert kwargs["json"]["to"] == "01712345678"
            assert kwargs["json"]["message"] == "Test custom SMS"
            
            # Verify log entry in DB
            log = db.query_one("SELECT * FROM sms_logs WHERE tenant_id = %s", (tenant_id,))
            assert log is not None
            assert log["phone"] == "01712345678"
            assert log["gateway"] == "smslayer"
            assert log["message_id"] == "msg_12345"
            assert log["status"] == "sent"


def test_send_sms_platform_fallback_and_wallet_deduction(app):
    with app.app_context():
        # Setup tenant without key, with some balance
        tenant_id = create_tenant("SMS Tenant B", "sms_b@store.com")
        credit_balance(tenant_id, Decimal("10.00"), "Initial Credit")
        
        # Setup platform settings
        db.set_setting("PLATFORM_SMSLAYER_API_KEY", "platform-api-key")
        db.set_setting("PLATFORM_SMS_RATE", "0.35") # 0.35 BDT per SMS
        
        # Mock requests.post
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message_id": "platform_msg_999", "cost": 0.02} # SMSLayer cost, but platform charges 0.35
        
        with patch("requests.post") as mock_post:
            mock_post.return_value = mock_response
            
            res = send_sms("01812345678", "Test platform SMS", tenant_id, order_id=None)
            
            assert res["success"] is True
            assert res["message_id"] == "platform_msg_999"
            
            # Verify authorization header uses platform key
            args, kwargs = mock_post.call_args
            assert kwargs["headers"]["Authorization"] == "Bearer platform-api-key"
            
            # Verify wallet deduction
            wallet = get_wallet(tenant_id)
            assert wallet["balance"] == Decimal("9.65") # 10.00 - 0.35
            
            # Verify log entry in DB
            log = db.query_one("SELECT * FROM sms_logs WHERE tenant_id = %s", (tenant_id,))
            assert log is not None
            assert log["gateway"] == "smslayer_platform"
            assert float(log["cost"]) == 0.35


def test_send_order_fallback_sms_success(app):
    with app.app_context():
        tenant_id = create_tenant("SMS Tenant C", "sms_c@store.com")
        update_tenant_settings(tenant_id, {
            "smslayer_api_key": "tenant-key",
            "sms_template": "Order {order_id} has amount {amount} callback {callback_number}"
        })
        db.set_setting("GLOBAL_CALLBACK_NUMBER", "09612345678")
        
        # Insert mock order
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status)
            VALUES (%s, 'WC-1002', '01912345678', 'Meraj', 1500.00, 'UNREACHABLE')
            RETURNING id
            """,
            (tenant_id,)
        )
        
        # Mock requests.post
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message_id": "msg_success"}
        
        with patch("requests.post") as mock_post:
            mock_post.return_value = mock_response
            
            success = send_order_fallback_sms(order_id, tenant_id)
            assert success is True
            
            # Verify template replacement
            args, kwargs = mock_post.call_args
            assert kwargs["json"]["message"] == "Order WC-1002 has amount 1500.00 callback 09612345678"
            
            # Verify order table updated
            order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
            assert order["sms_sent"] is True
            assert order["sms_sent_at"] is not None


def test_unreachable_status_triggers_sms_fallback_thread(app):
    with app.app_context():
        tenant_id = create_tenant("SMS Tenant D", "sms_d@store.com")
        update_tenant_settings(tenant_id, {
            "enable_sms_fallback": True
        })
        
        # Create an order with attempt_count = 3, max_attempts = 3
        # Next retry attempt is 4, which exceeds max_attempts, triggering UNREACHABLE status.
        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, attempt_count, max_attempts)
            VALUES (%s, 'WC-1003', '01512345678', 'Anis', 500.00, 'CALL_QUEUED', 3, 3)
            RETURNING id
            """,
            (tenant_id,)
        )
        
        with patch("threading.Thread") as mock_thread_class:
            mock_thread_instance = MagicMock()
            mock_thread_class.return_value = mock_thread_instance
            
            # When schedule_retry is called, it sees attempt_count=3 and attempt_no=4, exhausts retries,
            # and marks the order as UNREACHABLE, triggering _mark_unreachable.
            res = schedule_retry(order_id, tenant_id, current_attempt=3)
            
            assert res is False # Retries exhausted
            
            # Verify order status in DB
            order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
            assert order["order_status"] == "UNREACHABLE"
            
            # Verify Thread was started to send fallback SMS
            found = False
            for call in mock_thread_class.call_args_list:
                called_kwargs = call[1]
                if called_kwargs.get("target") == send_order_fallback_sms:
                    assert called_kwargs["args"] == (order_id, tenant_id)
                    assert called_kwargs["daemon"] is True
                    found = True
                    break
            assert found, "send_order_fallback_sms thread was not started"
            mock_thread_instance.start.assert_called()
