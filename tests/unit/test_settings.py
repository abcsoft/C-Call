from __future__ import annotations

import json
import hashlib
import pytest
from unittest.mock import patch, MagicMock
from app.db import db
from app.models.tenant import get_tenant_by_id

def test_integration_page_loads(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Integration Store", "integ@store.com")
        client = auth_client((tenant, user))

        res = client.get("/integration")
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert "API &amp; Integrations" in html or "API & Integrations" in html

def test_api_key_generation_and_revocation(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Key Store", "key@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]

        # 1. Generate key
        res = client.post("/api/integration/keys", data={"name": "WooCommerce Key"})
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert "New API Key Generated!" in html
        assert "cc_live_" in html

        # Verify key is saved hashed in DB
        keys = db.query("SELECT * FROM api_keys WHERE tenant_id = %s", (tenant_id,))
        assert len(keys) == 1
        assert keys[0]["name"] == "WooCommerce Key"
        assert keys[0]["is_active"] is True
        key_id = keys[0]["id"]
        key_prefix = keys[0]["key_prefix"]

        # Check only first 12 chars of the raw key form the prefix
        assert len(key_prefix) == 12
        assert key_prefix.startswith("cc_live_")

        # 2. Revoke key
        res_revoke = client.delete(f"/api/integration/keys/{key_id}")
        assert res_revoke.status_code == 200
        
        # Verify key is deleted
        keys_after = db.query("SELECT * FROM api_keys WHERE tenant_id = %s", (tenant_id,))
        assert len(keys_after) == 0

def test_webhook_url_saving_and_secret_regeneration(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Webhook Store", "wh@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]

        # 1. Save Webhook URL (Invalid scheme)
        res = client.post("/integration", data={"webhook_url": "http://my-insecure-site.com"})
        assert res.status_code == 302 # redirects back
        
        # Verify not updated
        tenant_data = get_tenant_by_id(tenant_id)
        settings = json.loads(tenant_data["settings"]) if isinstance(tenant_data["settings"], str) else tenant_data["settings"]
        assert settings.get("webhook_url") != "http://my-insecure-site.com"

        # 2. Save Webhook URL (Valid)
        res = client.post("/integration", data={"webhook_url": "https://my-secure-site.com/api/webhook"})
        assert res.status_code == 302
        
        # Verify updated
        tenant_data = get_tenant_by_id(tenant_id)
        settings = json.loads(tenant_data["settings"]) if isinstance(tenant_data["settings"], str) else tenant_data["settings"]
        assert settings.get("webhook_url") == "https://my-secure-site.com/api/webhook"

        # 3. Regenerate Webhook Secret
        res_secret = client.post("/api/integration/webhook/secret")
        assert res_secret.status_code == 302

        # Verify updated secret starts with whsec_
        tenant_data = get_tenant_by_id(tenant_id)
        settings = json.loads(tenant_data["settings"]) if isinstance(tenant_data["settings"], str) else tenant_data["settings"]
        new_secret = settings.get("webhook_secret")
        assert new_secret is not None
        assert new_secret.startswith("whsec_")

def test_webhook_delivery_test(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Webhook Test Store", "whtest@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]

        # Test before URL is set
        res_fail = client.get("/api/integration/webhook-test")
        assert res_fail.status_code == 400
        assert "Webhook URL is not configured" in res_fail.get_data(as_text=True)

        # Set URL
        client.post("/integration", data={"webhook_url": "https://my-secure-site.com/api/webhook"})

        # Mock urllib.request.urlopen to simulate successful endpoint response
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = b"Success Body response"
        mock_response.__enter__.return_value = mock_response

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            res_success = client.get("/api/integration/webhook-test")
            assert res_success.status_code == 200
            html = res_success.get_data(as_text=True)
            assert "Webhook Sent Successfully!" in html
            assert "200" in html
            assert "Success Body response" in html
            mock_urlopen.assert_called_once()

def test_script_editor_loading_and_saving(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Script Store", "script@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]

        # 1. Load Editor page
        res = client.get("/script")
        assert res.status_code == 200
        assert "Voice Script Configurator" in res.get_data(as_text=True)

        # 2. Save script settings — use "edge" provider which is in the safe default policy
        post_data = {
            "voice_script": "হ্যালো {{customer_name}}, আপনার অর্ডার {{amount}} টাকা।",
            "tts_provider": "edge",
            "edge_tts_voice": "bn-BD-NabanitaNeural",
            "speaking_rate": "fast",
            "language": "Bangla"
        }
        res_save = client.post("/api/settings/script", data=post_data)
        assert res_save.status_code == 302 # Redirect

        # Verify saved in settings
        tenant_data = get_tenant_by_id(tenant_id)
        settings = json.loads(tenant_data["settings"]) if isinstance(tenant_data["settings"], str) else tenant_data["settings"]
        assert settings.get("voice_script") == "হ্যালো {{customer_name}}, আপনার অর্ডার {{amount}} টাকা।"
        assert settings.get("tts_provider") == "edge"
        assert settings.get("speaking_rate") == "fast"
        assert settings.get("language") == "Bangla"

def test_api_settings_voices_endpoint(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Voice Store", "voice@store.com")
        client = auth_client((tenant, user))

        # Edge voices
        res_edge = client.get("/api/settings/voices?provider=edge")
        assert res_edge.status_code == 200
        assert "bn-BD-NabanitaNeural" in res_edge.get_data(as_text=True)

        # Gemini voices
        res_gemini = client.get("/api/settings/voices?provider=gemini")
        assert res_gemini.status_code == 200
        assert "Puck" in res_gemini.get_data(as_text=True)

        # gTTS options
        res_gtts = client.get("/api/settings/voices?provider=gtts")
        assert res_gtts.status_code == 200
        assert "Bangla" in res_gtts.get_data(as_text=True)

def test_tts_preview_generation(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Preview Store", "prev@store.com")
        client = auth_client((tenant, user))

        post_data = {
            "voice_script": "হ্যালো {{customer_name}}, আপনার অর্ডার {{amount}} টাকা।",
            "tts_provider": "edge",
            "edge_tts_voice": "bn-BD-NabanitaNeural"
        }

        # Mock tts generation to bypass external provider call
        with patch("app.blueprints.customer.settings.generate_order_audio", return_value="ivr-preview-123") as mock_gen:
            res = client.post("/api/settings/tts-preview", data=post_data)
            assert res.status_code == 200
            html = res.get_data(as_text=True)
            assert "Audio Synthesized" in html
            assert "ivr-preview-123.wav" in html
            
            # Check mock called with correct text replacement and overrides
            mock_gen.assert_called_once()
            args = mock_gen.call_args[0]
            # Replace placeholder variables with preview values
            assert "সাকিব হাসান" in args[1] # {{customer_name}}
            assert "970" in args[1] # {{amount}}
            
            kwargs = mock_gen.call_args[1]
            assert kwargs["settings_override"]["tts_provider"] == "edge"
            assert kwargs["settings_override"]["edge_tts_voice"] == "bn-BD-NabanitaNeural"

def test_integration_test_order_trigger(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("Test Order Store", "torder@store.com")
        client = auth_client((tenant, user))

        # Credit wallet to satisfy balance check
        db.execute("UPDATE wallets SET balance = 100.0 WHERE tenant_id = %s", (tenant["id"],))

        post_data = {
            "phone": "01711234567",
            "customer_name": "করিম সাহেব",
            "amount": "2500",
            "product_name": "একটি শাড়ি"
        }

        with patch("app.services.order_service.queue_confirmation_call", return_value="call-999") as mock_queue:
            res = client.post("/api/integration/test-order", data=post_data)
            assert res.status_code == 200
            html = res.get_data(as_text=True)
            assert "Success" in html
            assert "Order created and call queued!" in html
            assert "01711234567" in html
            
            # Check order is created in DB
            orders = db.query("SELECT * FROM orders WHERE tenant_id = %s", (tenant["id"],))
            assert len(orders) == 1
            assert orders[0]["phone"] == "+8801711234567"
            assert orders[0]["customer_name"] == "করিম সাহেব"
            assert orders[0]["amount"] == 2500.00
