import hashlib
import json
from decimal import Decimal
from app.db import db
from app.models.tenant import create_tenant, update_tenant_settings
from app.models.wallet import credit_balance
from app.services import order_service
from app.services.tts_service import render_order_voice_script, generate_order_audio

def _create_api_key(tenant_id, name="Test Key", key_str="cc_live_testkey12345"):
    key_hash = hashlib.sha256(key_str.encode("utf-8")).hexdigest()
    key_prefix = key_str[:12]
    db.execute(
        """
        INSERT INTO api_keys (tenant_id, name, key_hash, key_prefix, is_active)
        VALUES (%s, %s, %s, %s, true)
        """,
        (tenant_id, name, key_hash, key_prefix)
    )
    return key_str

def _setup_tenant_and_cli(app, name="E-Comm Tenant", email="ecomm@tenant.com"):
    tenant_id = create_tenant(name, email)
    credit_balance(tenant_id, Decimal("100.0"), "Deposit")
    
    # Add mock provider and phone number CLI so order dispatch does not fail
    provider_id = db.insert_returning_id(
        """
        INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password)
        VALUES ('EComm Mock Provider', 'sip.mock.com', 5060, 'user', 'pass')
        RETURNING id
        """
    )
    db.execute(
        """
        INSERT INTO phone_numbers (number, provider_id, number_type, status, monthly_cost)
        VALUES ('01799999999', %s, 'shared', 'active', 0.0)
        """,
        (provider_id,)
    )
    return tenant_id

def test_order_with_ecommerce_metadata(client, app):
    with app.app_context():
        tenant_id = _setup_tenant_and_cli(app)
        api_key = _create_api_key(tenant_id)
        
    payload = {
        "order_id": "WC-METADATA-1",
        "phone": "01712345678",
        "customer_name": "Sakib Hasan",
        "amount": "1500.00",
        "site_name": "My Store",
        "product_name": "Item A",
        "products": [{"name": "Item A", "qty": 1, "total": "1400.00"}],
        "price": "1400.00",
        "delivery_charge": "100.00",
        "grand_total": "1500.00",
        "payment_method": "Cash on Delivery",
        "callback_number": "09602111380",
        "confirm_text": "confirmed custom",
        "cancel_text": "cancelled custom",
        "agent_extension": "102"
    }
    
    res = client.post(
        "/api/v1/orders",
        headers={"X-API-Key": api_key},
        json=payload
    )
    assert res.status_code == 200
    data = json.loads(res.data)
    assert data["success"] is True
    
    # Fetch details and verify metadata is returned correctly
    res_get = client.get(
        "/api/v1/orders/WC-METADATA-1",
        headers={"X-API-Key": api_key}
    )
    assert res_get.status_code == 200
    details = json.loads(res_get.data)["data"]
    
    assert details["customer_name"] == "Sakib Hasan"
    assert details["amount"] == 1500.00
    
    meta = details["metadata"]
    assert meta["store_name"] == "My Store"
    assert meta["product_name"] == "Item A"
    assert meta["price"] == "1400.00"
    assert meta["delivery_charge"] == "100.00"
    assert meta["grand_total"] == "1500.00"
    assert meta["payment_method"] == "Cash on Delivery"
    assert meta["callback_number"] == "09602111380"
    assert meta["confirm_text"] == "confirmed custom"
    assert meta["cancel_text"] == "cancelled custom"
    assert meta["agent_extension"] == "102"

def test_rendered_tts_contains_dynamic_values(app):
    with app.app_context():
        tenant_id = _setup_tenant_and_cli(app)
        tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
        
        order_data = {
            "order_id": "WC-RENDER-1",
            "phone": "01712345678",
            "customer_name": "Rashed",
            "amount": "970.00",
            "site_name": "BD Shop",
            "product_name": "Panjabi",
            "products": [{"name": "Panjabi", "qty": 1, "total": "850.00"}],
            "price": "850.00",
            "delivery_charge": "120.00",
            "grand_total": "970.00",
            "payment_method": "COD",
            "callback_number": "09602111380"
        }
        
        order = order_service.create_order(tenant_id, order_data)
        
        template = (
            "Store: {site_name}, Name: {customer_name}, Order: {order_id}, Product: {product_name}, "
            "Products: {products}, Price: {price}, Delivery: {delivery_charge}, Grand: {grand_total}, "
            "Subtotal: {subtotal}, Method: {payment_method}, Callback: {callback_number}"
        )
        
        rendered = render_order_voice_script(template, order, tenant)
        
        assert "Store: BD Shop" in rendered
        assert "Name: Rashed" in rendered
        assert "Order: WC-RENDER-1" in rendered
        assert "Product: Panjabi" in rendered
        assert "Products: Panjabi" in rendered
        assert "Price: 850" in rendered
        assert "Delivery: 120" in rendered
        assert "Grand: 970" in rendered
        assert "Subtotal: 850" in rendered
        assert "Method: COD" in rendered
        assert "Callback: 09602111380" in rendered

def test_wc_shipping_total_as_delivery_charge(app):
    with app.app_context():
        tenant_id = _setup_tenant_and_cli(app)
        order_data = {
            "order_id": "WC-SHIPPING-1",
            "phone": "01712345678",
            "customer_name": "Rashed",
            "amount": "950.00",
            "shipping_total": "150.00"
        }
        
        order = order_service.create_order(tenant_id, order_data)
        meta = order["metadata"]
        assert meta["delivery_charge"] == "150.00"

def test_custom_text_overrides_template(app, monkeypatch):
    # Mock create_tts_audio to catch the text that is passed in
    received_text = []
    def mock_create_tts_audio(text, **kwargs):
        received_text.append(text)
        return "mock_audio_filename"
        
    monkeypatch.setattr("app.services.tts_service.create_tts_audio", mock_create_tts_audio)
    
    with app.app_context():
        tenant_id = _setup_tenant_and_cli(app)
        
        order_data = {
            "order_id": "WC-CUSTOM-TXT-1",
            "phone": "01712345678",
            "customer_name": "Tasin",
            "amount": "1000.00",
            "custom_text": "This is a completely custom script override for {customer_name}"
        }
        
        order = order_service.create_order(tenant_id, order_data)
        # Verify custom_text column is set
        assert order["custom_text"] == "This is a completely custom script override for {customer_name}"
        
        # When generating audio, it should render custom_text instead of tenant template
        generate_order_audio(tenant_id, order)
        
        assert len(received_text) > 0
        # Text gets appdata spaces (tenant_id) appended for cache key, so check startswith
        assert received_text[0].startswith("This is a completely custom script override for Tasin")

def test_single_and_double_brace_variables(app):
    with app.app_context():
        tenant_id = _setup_tenant_and_cli(app)
        tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
        
        order_data = {
            "order_id": "WC-BRACES-1",
            "phone": "01712345678",
            "customer_name": "Misu",
            "amount": "500.00",
            "site_name": "Aura"
        }
        order = order_service.create_order(tenant_id, order_data)
        
        template = "Single: {site_name}, Double: {{site_name}}"
        rendered = render_order_voice_script(template, order, tenant)
        assert rendered == "Single: Aura, Double: Aura"

def test_missing_delivery_charge_defaults_zero(app):
    with app.app_context():
        tenant_id = _setup_tenant_and_cli(app)
        tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
        
        order_data = {
            "order_id": "WC-MISSING-DELIV-1",
            "phone": "01712345678",
            "customer_name": "Karim",
            "amount": "500.00"
        }
        order = order_service.create_order(tenant_id, order_data)
        
        template = "Charge: {delivery_charge}"
        rendered = render_order_voice_script(template, order, tenant)
        assert rendered == "Charge: 0"

def test_fallback_store_name_to_tenant(app):
    with app.app_context():
        tenant_id = _setup_tenant_and_cli(app)
        db.execute("UPDATE tenants SET business_name = 'Tenant Business Name' WHERE id = %s", (tenant_id,))
        tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
        
        order_data = {
            "order_id": "WC-FALLBACK-STORE-1",
            "phone": "01712345678",
            "customer_name": "Karim",
            "amount": "500.00"
        }
        order = order_service.create_order(tenant_id, order_data)
        
        template = "Store: {site_name}"
        rendered = render_order_voice_script(template, order, tenant)
        assert rendered == "Store: Tenant Business Name"

def test_fallback_grand_total_to_amount(app):
    with app.app_context():
        tenant_id = _setup_tenant_and_cli(app)
        tenant = db.query_one("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
        
        order_data = {
            "order_id": "WC-FALLBACK-GT-1",
            "phone": "01712345678",
            "customer_name": "Karim",
            "amount": "650.00"
        }
        order = order_service.create_order(tenant_id, order_data)
        
        template = "Grand: {grand_total}"
        rendered = render_order_voice_script(template, order, tenant)
        assert rendered == "Grand: 650"
