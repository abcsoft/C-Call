from __future__ import annotations

import json
from decimal import Decimal
import pytest
from unittest.mock import MagicMock

from app.db import db
import db as root_db
from app.models.tenant import create_tenant, update_tenant
from app.models.wallet import credit_balance
from app.services.tts_service import generate_order_audio
from call_engine import call_runner

# Save original start_call to un-mock it in tests
original_start_call = call_runner.start_call

@pytest.fixture(autouse=True)
def patch_root_db(monkeypatch):
    """Patch root db module to delegate to app.db.db to share the same transaction pool,
    translating SQLite placeholder syntax (?) to PostgreSQL syntax (%s)."""
    def pg_tr(sql: str) -> str:
        return sql.replace("?", "%s")

    monkeypatch.setattr(root_db, "query", lambda sql, params=(): db.query(pg_tr(sql), params))
    monkeypatch.setattr(root_db, "query_one", lambda sql, params=(): db.query_one(pg_tr(sql), params))
    monkeypatch.setattr(root_db, "execute", lambda sql, params=(): db.execute(pg_tr(sql), params))
    monkeypatch.setattr(root_db, "insert_returning_id", lambda sql, params=(): db.insert_returning_id(pg_tr(sql), params))


@pytest.fixture
def test_setup_ext(app):
    """Setup a tenant, provider, phone CLI, and credit balance."""
    with app.app_context():
        # Create tenant
        tenant_id = create_tenant({
            "business_name": "Ext Store",
            "email": "ext@store.com",
            "plan_name": "free",
            "status": "active"
        })
        
        # Credit balance
        credit_balance(tenant_id, Decimal("100.00"), "Initial Deposit")
        
        # Create provider
        provider_id = db.insert_returning_id(
            """
            INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password)
            VALUES ('Ext Provider', 'sip.ext.com', 5060, 'user', 'pass')
            RETURNING id
            """
        )
        
        # Create shared phone CLI
        db.execute(
            """
            INSERT INTO phone_numbers (number, provider_id, number_type, status, monthly_cost)
            VALUES ('01788888888', %s, 'shared', 'active', 0.0)
            """,
            (provider_id,)
        )
        
        # Create a basic flow
        flow_id = db.insert_returning_id(
            "INSERT INTO flows (tenant_id, name, description, is_active) VALUES (%s, 'Test Flow', 'Desc', true) RETURNING id",
            (tenant_id,)
        )
        node_id = db.insert_returning_id(
            "INSERT INTO flow_nodes (flow_id, name, node_type, prompt_text, prompt_audio, gather_timeout_seconds, max_retries) VALUES (%s, 'Root Node', 'menu', 'Hello', 'audio.ulaw', 5, 2) RETURNING id",
            (flow_id,)
        )
        db.execute("UPDATE flows SET root_node_id = %s WHERE id = %s", (node_id, flow_id))
        
        return tenant_id, flow_id


def test_sip_config_tenant_fallback(app, test_setup_ext):
    """Verify that sip_config loads settings from tenant settings and falls back to global settings."""
    tenant_id, _ = test_setup_ext
    with app.app_context():
        # 1. Without tenant settings, fallback to global setting or default
        # Clear/mock global settings
        root_db.set_setting("SIP_DOMAIN", "global.domain.com")
        root_db.set_setting("SIP_PORT", "5060")
        root_db.set_setting("SIP_USER", "globaluser")
        root_db.set_setting("SIP_PASSWORD", "globalpass")

        cfg = call_runner.sip_config(tenant_id)
        assert cfg["SIP_DOMAIN"] == "global.domain.com"
        assert cfg["SIP_PORT"] == "5060"
        assert cfg["SIP_USER"] == "globaluser"
        assert cfg["SIP_PASSWORD"] == "globalpass"
        assert call_runner.sip_ready(tenant_id) is True

        # 2. With tenant settings, override global settings
        tenant_settings = {
            "SIP_DOMAIN": "tenant.domain.com",
            "sip_user": "tenantuser",
            "sip_password": "tenantpass"
        }
        update_tenant(tenant_id, {"settings": tenant_settings})

        cfg = call_runner.sip_config(tenant_id)
        assert cfg["SIP_DOMAIN"] == "tenant.domain.com"
        assert cfg["SIP_PORT"] == "5060" # Fallback
        assert cfg["SIP_USER"] == "tenantuser" # lowercase resolution
        assert cfg["SIP_PASSWORD"] == "tenantpass"
        assert call_runner.sip_ready(tenant_id) is True


def test_start_call_insert_and_override(app, test_setup_ext, monkeypatch):
    """Verify start_call overrides SIP_USER when caller_id is provided and inserts it."""
    tenant_id, flow_id = test_setup_ext
    
    # Restore actual start_call from conftest global mock
    monkeypatch.setattr(call_runner, "start_call", original_start_call)

    # Mock executor and IvrCall run to run synchronously and capture arguments
    captured_sip = {}
    def mock_submit(fn, call_id, dial_target, flow, sip, tenant_id_arg, order_id_arg):
        nonlocal captured_sip
        captured_sip = sip
        
    monkeypatch.setattr(call_runner, "_executor", MagicMock(submit=mock_submit))
    monkeypatch.setattr(call_runner, "sip_ready", lambda tid: True)

    with app.app_context():
        # Setup mock order row in database to satisfy foreign key constraints
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount) VALUES (%s, 'O-STAR-CALL', '01712345678', 'Customer', 100.0) RETURNING id",
            (tenant_id,)
        )

        call_id = call_runner.start_call(
            "01712345678",
            flow_id,
            name="Test Caller",
            tenant_id=tenant_id,
            order_id=order_id,
            caller_id="01788888888"
        )
        
        # Verify call DB row has the new columns
        row = db.query_one("SELECT * FROM calls WHERE id = %s", (call_id,))
        assert row["tenant_id"] == tenant_id
        assert row["order_id"] == order_id
        assert row["caller_id"] == "01788888888"
        
        # Verify SIP_USER override in execution sip config
        assert captured_sip["SIP_USER"] == "01788888888"


def test_run_ivr_thread_sync_order_update(app, test_setup_ext, monkeypatch):
    """Verify that _run_ivr_thread calls update_status_from_call synchronously when the call terminates."""
    tenant_id, flow_id = test_setup_ext
    
    # Mock update_status_from_call to verify it was called
    called_args = {}
    def mock_update_status(order_id, tenant_id, call_result, dtmf_digit, talk_seconds, call_id):
        nonlocal called_args
        called_args = {
            "order_id": order_id,
            "tenant_id": tenant_id,
            "call_result": call_result,
            "dtmf_digit": dtmf_digit,
            "talk_seconds": talk_seconds,
            "call_id": call_id
        }
    monkeypatch.setattr("app.services.order_service.update_status_from_call", mock_update_status)

    with app.app_context():
        # Setup mock order row in database to satisfy foreign key constraints
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount) VALUES (%s, 'O-RUN-IVR', '01712345678', 'Customer', 100.0) RETURNING id",
            (tenant_id,)
        )

        # Setup mock call row in database
        call_id = db.insert_returning_id(
            "INSERT INTO calls (number, status, direction, talk_seconds, tenant_id, order_id) VALUES ('01712345678', 'calling', 'outbound', 0, %s, %s) RETURNING id",
            (tenant_id, order_id)
        )
        
        # Mock _extract_dtmf_digit to return "1"
        monkeypatch.setattr(call_runner, "_extract_dtmf_digit", lambda cid: "1")
        
        # Load flow first
        flow = call_runner.load_flow(flow_id)

        # Mock IvrCall.run to return completed result using flow.root_node_id
        from call_engine.ivr_engine import CallResult
        mock_result = CallResult(
            status="completed",
            sip_final_status="200 OK",
            ring_seconds=2,
            talk_seconds=12,
            total_seconds=14,
            digits_pressed=1,
            drop_node_id=None,
            last_node_id=flow.root_node_id,
            reached_terminal=True,
            recording_path=None,
            error=None
        )
        monkeypatch.setattr(call_runner.IvrCall, "run", lambda self: mock_result)

        # Run thread synchronously
        call_runner._run_ivr_thread(call_id, "sip:01712345678@127.0.0.1", flow, {"SIP_DOMAIN": "127.0.0.1", "SIP_USER": "testuser", "SIP_PASSWORD": "testpassword"}, tenant_id=tenant_id, order_id=order_id)
        
        # Assert update_status_from_call called with correct inputs
        assert called_args["order_id"] == order_id
        assert called_args["tenant_id"] == tenant_id
        assert called_args["call_result"] == "completed"
        assert called_args["dtmf_digit"] == "1"
        assert called_args["talk_seconds"] == 12
        assert called_args["call_id"] == call_id


def test_tts_service_zero_width_spaces(app, test_setup_ext, monkeypatch):
    """Verify that generate_order_audio appends zero-width spaces matching tenant_id to the text to prevent cross-tenant cache collisions."""
    tenant_id, _ = test_setup_ext
    
    captured_text = ""
    def mock_create_tts_audio(text, *args, **kwargs):
        nonlocal captured_text
        captured_text = text
        return "audio_filename"
    monkeypatch.setattr("app.services.tts_service.create_tts_audio", mock_create_tts_audio)

    with app.app_context():
        # 1. Dictionaries parsing template
        order_data = {
            "customer_name": "Rahim",
            "order_id": "WC-100",
            "amount": "500.00",
            "store_name": "My Shop"
        }
        
        # Update settings template
        update_tenant(tenant_id, {"settings": {"voice_script": "Hello {{customer_name}}, pay {{amount}} BDT at {{store_name}}."}})
        
        res = generate_order_audio(tenant_id, order_data)
        assert res == "audio_filename"
        # Zero width spaces count should match tenant_id
        assert captured_text.endswith("\u200b" * tenant_id)
        assert "Hello Rahim, pay 500 BDT at My Shop." in captured_text

        # 2. String preview backward compatibility
        res2 = generate_order_audio(tenant_id, "Preview Text")
        assert res2 == "audio_filename"
        assert captured_text.endswith("\u200b" * tenant_id)
        assert "Preview Text" in captured_text
