from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.db import db
import db as root_db
from app.models.tenant import create_tenant
from app.models.wallet import credit_balance
from call_engine import active_calls
from call_engine import call_runner

original_start_call = call_runner.start_call
original_start_direct_call = call_runner.start_direct_call


@pytest.fixture(autouse=True)
def patch_root_db(monkeypatch):
    """call_runner.py uses the root db module (SQLite ? placeholders) —
    translate to the shared Postgres test pool, same pattern as
    test_call_runner_ext.py."""
    def pg_tr(sql: str) -> str:
        return sql.replace("?", "%s")

    monkeypatch.setattr(root_db, "query", lambda sql, params=(): db.query(pg_tr(sql), params))
    monkeypatch.setattr(root_db, "query_one", lambda sql, params=(): db.query_one(pg_tr(sql), params))
    monkeypatch.setattr(root_db, "execute", lambda sql, params=(): db.execute(pg_tr(sql), params))
    monkeypatch.setattr(root_db, "insert_returning_id", lambda sql, params=(): db.insert_returning_id(pg_tr(sql), params))


@pytest.fixture(autouse=True)
def clear_registry(monkeypatch):
    """Clear the local fallback registry between tests.
    Redis is unavailable in the test environment so active_calls falls back
    to _local_registry / _local_cancel_handlers automatically."""
    monkeypatch.setenv("LIVE_CALLS_KEEP_COMPLETED_SECONDS", "0")
    active_calls._local_registry.clear()
    active_calls._cancel_handlers.clear()
    yield
    active_calls._local_registry.clear()
    active_calls._cancel_handlers.clear()


@pytest.fixture
def live_tenant(app):
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Live Monitor Store", "email": "live@store.com",
            "plan_name": "free", "status": "active",
        })
        credit_balance(tenant_id, Decimal("100.00"), "Initial Deposit")
        provider_id = db.insert_returning_id(
            "INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password) "
            "VALUES ('Live Provider', 'sip.live.com', 5060, 'user', 'pass') RETURNING id"
        )
        db.execute(
            "INSERT INTO phone_numbers (number, provider_id, number_type, status, monthly_cost) "
            "VALUES ('01788888888', %s, 'shared', 'active', 0.0)",
            (provider_id,)
        )
        flow_id = db.insert_returning_id(
            "INSERT INTO flows (tenant_id, name, is_active) VALUES (%s, 'Live Test Flow', true) RETURNING id",
            (tenant_id,)
        )
        node_id = db.insert_returning_id(
            "INSERT INTO flow_nodes (flow_id, name, node_type, prompt_text, prompt_audio, gather_timeout_seconds, max_retries) "
            "VALUES (%s, 'Root Node', 'menu', 'Hello', 'audio.ulaw', 5, 2) RETURNING id",
            (flow_id,)
        )
        db.execute("UPDATE flows SET root_node_id = %s WHERE id = %s", (node_id, flow_id))
        return tenant_id, flow_id


# ---------------------------------------------------------------------------
# Registry primitives
# ---------------------------------------------------------------------------

def test_registry_register_list_remove():
    active_calls.register_active_call(
        1, tenant_id=5, order_id=None, call_type="direct", phone="+8801700000000", caller_id="01788888888"
    )
    entries = active_calls.list_active_calls()
    assert len(entries) == 1
    assert entries[0]["status"] == "queued"

    active_calls.update_active_call(1, status="ringing")
    assert active_calls.get_active_call(1)["status"] == "ringing"

    active_calls.remove_active_call(1)
    assert active_calls.get_active_call(1) is None


def test_tenant_only_sees_own_call_super_admin_sees_all():
    active_calls.register_active_call(1, tenant_id=10, order_id=None, call_type="direct", phone="x", caller_id=None)
    active_calls.register_active_call(2, tenant_id=20, order_id=None, call_type="direct", phone="y", caller_id=None)

    from app.services.live_call_service import list_live_calls

    tenant_10_view = list_live_calls(tenant_id=10)
    assert len(tenant_10_view) == 1
    assert tenant_10_view[0]["call_id"] == 1

    super_admin_view = list_live_calls(tenant_id=None)
    assert len(super_admin_view) == 2


def test_tenant_cannot_hangup_another_tenants_call():
    active_calls.register_active_call(1, tenant_id=10, order_id=None, call_type="direct", phone="x", caller_id=None)
    active_calls.set_cancel_handler(1, MagicMock())

    from app.services.live_call_service import hangup_call

    assert hangup_call(1, tenant_id=20) is False
    assert hangup_call(1, tenant_id=10) is True


def test_hangup_works_for_ivr_call():
    handler = MagicMock()
    active_calls.register_active_call(1, tenant_id=10, order_id=None, call_type="ivr", phone="x", caller_id=None)
    active_calls.set_cancel_handler(1, handler)

    from app.services.live_call_service import hangup_call

    assert hangup_call(1, tenant_id=10) is True
    handler.assert_called_once()


def test_hangup_works_for_direct_subprocess(monkeypatch):
    """cancel_call's direct-call branch terminates the stored Popen handle."""
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # still running
    cancel_event = MagicMock()
    call_runner._active_direct_procs[42] = (fake_proc, cancel_event)

    try:
        result = call_runner.cancel_call(42)
        assert result is True
        cancel_event.set.assert_called_once()
        fake_proc.terminate.assert_called_once()
    finally:
        call_runner._active_direct_procs.pop(42, None)


# ---------------------------------------------------------------------------
# Real dispatch paths
# ---------------------------------------------------------------------------

def test_start_direct_call_appears_in_live_calls(app, live_tenant, monkeypatch):
    tenant_id, _ = live_tenant
    monkeypatch.setattr(call_runner, "start_direct_call", original_start_direct_call)
    monkeypatch.setattr(call_runner, "sip_ready", lambda tid: True)
    monkeypatch.setattr(call_runner, "audio_duration_seconds", lambda name: 5)

    captured = {}
    def mock_submit(fn, *args):
        captured["args"] = args
    monkeypatch.setattr(call_runner, "_executor", MagicMock(submit=mock_submit))

    with app.app_context():
        audio_path = call_runner.INSTANCE_DIR / "sounds"
        audio_path.mkdir(parents=True, exist_ok=True)
        (audio_path / "test_audio.ulaw").touch()

        call_id = call_runner.start_direct_call(
            "01712345678", "test_audio", 1, message_text="hi", tenant_id=tenant_id, caller_id="01788888888"
        )

        entry = active_calls.get_active_call(call_id)
        assert entry is not None
        assert entry["call_type"] == "direct"
        assert entry["tenant_id"] == tenant_id
        assert entry["status"] == "queued"


def test_order_call_appears_in_live_calls_then_removed_on_completion(app, live_tenant, monkeypatch):
    tenant_id, flow_id = live_tenant
    monkeypatch.setattr("app.services.order_service.update_status_from_call", lambda **kw: None)

    with app.app_context():
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount) "
            "VALUES (%s, 'O-LIVE-1', '01712345678', 'Customer', 100.0) RETURNING id",
            (tenant_id,)
        )
        call_id = db.insert_returning_id(
            "INSERT INTO calls (number, status, direction, talk_seconds, tenant_id, order_id) "
            "VALUES ('01712345678', 'calling', 'outbound', 0, %s, %s) RETURNING id",
            (tenant_id, order_id)
        )

        # Simulate what start_call() does before dispatching to the thread.
        active_calls.register_active_call(
            call_id, tenant_id=tenant_id, order_id=order_id, call_type="order_confirmation",
            phone="01712345678", caller_id="01788888888",
        )
        assert active_calls.get_active_call(call_id) is not None

        monkeypatch.setattr(call_runner, "_extract_dtmf_digit", lambda cid: "1")
        flow = call_runner.load_flow(flow_id)

        from call_engine.ivr_engine import CallResult
        mock_result = CallResult(
            status="completed", sip_final_status="200 OK", ring_seconds=2, talk_seconds=12,
            total_seconds=14, digits_pressed=1, drop_node_id=None, last_node_id=flow.root_node_id,
            reached_terminal=True, recording_path=None, error=None,
        )
        monkeypatch.setattr(call_runner.IvrCall, "run", lambda self: mock_result)

        call_runner._run_ivr_thread(
            call_id, "sip:01712345678@127.0.0.1", flow,
            {"SIP_DOMAIN": "127.0.0.1", "SIP_USER": "testuser", "SIP_PASSWORD": "testpassword"},
            tenant_id=tenant_id, order_id=order_id,
        )

        # Completed -> dropped from the live registry...
        assert active_calls.get_active_call(call_id) is None
        # ...but the durable DB record still has it (CDR).
        row = db.query_one("SELECT status, dtmf_digit FROM calls WHERE id = %s", (call_id,))
        assert row["status"] == "completed"
        assert row["dtmf_digit"] == "1"
