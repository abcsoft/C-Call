"""Phase 6 tests for C-Call V2 Security Foundation, Calling Reliability, Ecommerce, and SQLite/PostgreSQL."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from call_engine.ivr_engine import Flow, IvrCall, Node


class TestV2SecurityFoundation:
    def test_sip_provider_fernet_fails_closed_in_production(self, monkeypatch):
        from app.models.sip_provider import _get_fernet
        monkeypatch.setenv("FLASK_ENV", "production")
        monkeypatch.delenv("SIP_PASSWORD_ENCRYPTION_KEY", raising=False)
        with pytest.raises(RuntimeError, match="SIP_PASSWORD_ENCRYPTION_KEY"):
            _get_fernet()

    def test_play_recording_blocks_path_traversal(self, client):
        from app.services.recording_service import get_secret
        import hmac, hashlib, base64, time
        with client.application.app_context():
            secret = get_secret()
            bad_path = "../../.env"
            exp = int(time.time()) + 300
            sig = base64.urlsafe_b64encode(
                hmac.new(secret.encode(), f"{bad_path}{exp}".encode(), hashlib.sha256).digest()
            ).decode().strip("=")
            resp = client.get(f"/recordings/play?path={bad_path}&sig={sig}&exp={exp}")
            assert resp.status_code == 403

    def test_serve_sound_requires_authentication(self, client):
        resp = client.get("/sounds/test.ulaw")
        assert resp.status_code in (302, 401, 403)

    def test_debug_live_registry_blocked_for_non_super_admin(self, auth_client, create_tenant):
        pair = create_tenant("V2DbgBiz", "v2dbg@example.com")
        c = auth_client(pair)
        resp = c.get("/api/debug/live-registry")
        assert resp.status_code == 404

    def test_quick_call_blocks_cross_tenant_flow_idor(self, auth_client, create_tenant, db_conn):
        pair1 = create_tenant("V2Idor1", "v2idor1@example.com")
        pair2 = create_tenant("V2Idor2", "v2idor2@example.com")
        t2_id = pair2[0]["id"]
        f2 = db_conn.execute(
            "INSERT INTO flows (tenant_id, name) VALUES (%s, 'Secret Flow') RETURNING id",
            (t2_id,),
        ).fetchone()
        f2_id = f2[0] if not isinstance(f2, dict) else f2["id"]

        c = auth_client(pair1)
        with patch("app.services.number_service.select_caller_id", return_value="+8801700000001"), \
             patch("app.services.wallet_service.check_balance", return_value=True):
            resp = c.post(
                "/dashboard/quick-call",
                data={"phone": "01711223344", "call_type": "flow", "flow_id": str(f2_id)},
            )
            assert b"Flow not found or access denied" in resp.data

    def test_compliance_destination_and_opt_out(self, app, create_tenant):
        from app.services.compliance_service import (
            check_destination_allowed,
            is_opted_out,
            record_opt_out,
            remove_opt_out,
            validate_outbound_call,
        )
        tenant, _ = create_tenant("V2CompBiz", "v2comp@example.com")
        tid = tenant["id"]
        with app.app_context():
            check_destination_allowed(tid, "+8801711223344")
            with pytest.raises(ValueError, match="prefix"):
                check_destination_allowed(tid, "+14155551234")

            record_opt_out(tid, "+8801711223344", reason="dnd", source="test")
            assert is_opted_out(tid, "+8801711223344") is True
            with pytest.raises(ValueError, match="opted out"):
                validate_outbound_call(tid, "+8801711223344", Decimal("1.0"))

            remove_opt_out(tid, "+8801711223344")
            assert is_opted_out(tid, "+8801711223344") is False

    def test_api_key_scope_and_quota_enforcement(self, client, db_conn, create_tenant):
        import hashlib, secrets
        tenant, _ = create_tenant("V2KeyBiz", "v2key@example.com")
        tid = tenant["id"]
        db_conn.execute("UPDATE tenants SET status = 'active' WHERE id = %s", (tid,))
        raw_key = f"cc_live_{secrets.token_urlsafe(16)}"
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        db_conn.execute(
            """
            INSERT INTO api_keys (tenant_id, name, key_hash, key_prefix, is_active, scopes, daily_quota, requests_today)
            VALUES (%s, 'ReadOnly', %s, %s, true, ARRAY['orders:read'], 2, 0)
            """,
            (tid, key_hash, raw_key[:12]),
        )

        # Read allowed
        r1 = client.get("/api/v1/orders", headers={"X-API-Key": raw_key})
        assert r1.status_code == 200

        # Write denied by scope
        r2 = client.post("/api/v1/orders", json={"order_id": "X1", "phone": "01711223344"}, headers={"X-API-Key": raw_key})
        assert r2.status_code == 403
        assert r2.get_json()["code"] == "INSUFFICIENT_SCOPE"

        # 3rd request exceeds daily_quota=2 -> 429
        r3 = client.get("/api/v1/orders", headers={"X-API-Key": raw_key})
        assert r3.status_code == 429
        assert r3.get_json()["code"] == "QUOTA_EXCEEDED"


class TestV2CallingReliabilityAndIVR:
    def _make_ivr(self, flow: Flow) -> tuple[IvrCall, list]:
        events = []
        sip = {"SIP_DOMAIN": "127.0.0.1", "SIP_PORT": "5060", "SIP_USER": "u", "SIP_PASSWORD": "p"}
        ivr = IvrCall(
            target="01711223344",
            flow=flow,
            sip=sip,
            on_event=events.append,
        )
        return ivr, events

    def test_ivr_retry_prompt_captures_barge_in_dtmf(self):
        nodes = {
            1: Node(id=1, name="Menu", node_type="menu", prompt_audio="p1.ulaw", max_retries=1, transitions={"1": 2}),
            2: Node(id=2, name="Done", node_type="hangup"),
        }
        flow = Flow(id=1, root_node_id=1, nodes=nodes)
        ivr, events = self._make_ivr(flow)

        # First _play_prompt -> None, _gather_digit -> None (timeout),
        # Retry _play_prompt -> "1" (barge-in during retry prompt!), then Done hangup _play_prompt -> None
        with patch.object(ivr, "_play_prompt", side_effect=[None, "1", None]), \
             patch.object(ivr, "_gather_digit", return_value=None):
            from call_engine.ivr_engine import CallResult
            res = CallResult()
            ivr._run_flow("127.0.0.1", 10000, res)

        assert res.reached_terminal is True
        assert res.last_node_id == 2
        assert any(e.type == "dtmf" and e.digit == "1" for e in events)

    def test_ivr_missing_root_and_missing_target_not_terminal(self):
        # Missing root
        flow1 = Flow(id=1, root_node_id=999, nodes={})
        ivr1, ev1 = self._make_ivr(flow1)
        from call_engine.ivr_engine import CallResult
        r1 = CallResult()
        ivr1._run_flow("127.0.0.1", 10000, r1)
        assert r1.reached_terminal is False
        assert any(e.type == "error" and e.detail == "missing_root_node" for e in ev1)

        # Missing target node
        nodes2 = {1: Node(id=1, name="Menu", node_type="menu", transitions={"1": 999})}
        flow2 = Flow(id=2, root_node_id=1, nodes=nodes2)
        ivr2, ev2 = self._make_ivr(flow2)
        r2 = CallResult()
        with patch.object(ivr2, "_play_prompt", return_value="1"):
            ivr2._run_flow("127.0.0.1", 10000, r2)
        assert r2.reached_terminal is False
        assert any(e.type == "error" and e.detail == "missing_node:999" for e in ev2)

    def test_ivr_traversal_limit_not_terminal(self):
        nodes = {1: Node(id=1, name="Loop", node_type="menu", transitions={"1": 1})}
        flow = Flow(id=3, root_node_id=1, nodes=nodes)
        ivr, ev = self._make_ivr(flow)
        from call_engine.ivr_engine import CallResult
        res = CallResult()
        with patch.object(ivr, "_play_prompt", return_value="1"):
            ivr._run_flow("127.0.0.1", 10000, res)
        assert res.reached_terminal is False
        assert any(e.type == "error" and e.detail == "traversal_limit_exceeded" for e in ev)

    def test_call_job_states_and_campaign_pause_resume(self, app, db_conn, create_tenant):
        from app.services.call_job_service import (
            VALID_CALL_STATES,
            cancel_job,
            enqueue_order_call,
            pause_tenant_campaigns,
            resume_tenant_campaigns,
            update_call_state,
        )
        assert VALID_CALL_STATES == {
            "queued", "preparing", "dialing", "ringing", "answered",
            "completed", "failed", "canceled", "busy", "no_answer",
        }
        tenant, _ = create_tenant("V2JobBiz", "v2job@example.com")
        tid = tenant["id"]
        with app.app_context():
            ord_row = db_conn.execute(
                "INSERT INTO orders (tenant_id, external_order_id, phone, amount) VALUES (%s, 'V2-ORD-1', '+8801711223344', 500) RETURNING id",
                (tid,),
            ).fetchone()
            oid = ord_row[0] if not isinstance(ord_row, dict) else ord_row["id"]
            job = enqueue_order_call(db_conn, oid, tid, attempt_no=1)
            update_call_state(db_conn, job["id"], "preparing")
            update_call_state(db_conn, job["id"], "dialing")
            assert pause_tenant_campaigns(tid) >= 0
            assert resume_tenant_campaigns(tid) >= 0


class TestV2EcommerceAndSQLite:
    def test_human_agent_dtmf0_webhook_event(self, app, db_conn, create_tenant):
        from app.services.order_service import update_status_from_call
        from app.services.webhook_service import build_payload
        tenant, _ = create_tenant("V2EcomBiz", "v2ecom@example.com")
        tid = tenant["id"]
        with app.app_context():
            ord_row = db_conn.execute(
                "INSERT INTO orders (tenant_id, external_order_id, phone, amount, order_status) "
                "VALUES (%s, 'V2-AGENT-1', '+8801711223344', 1200, 'CALLING') RETURNING id",
                (tid,),
            ).fetchone()
            oid = ord_row[0] if not isinstance(ord_row, dict) else ord_row["id"]
            call_row = db_conn.execute(
                "INSERT INTO calls (tenant_id, number, status) VALUES (%s, '+8801711223344', 'completed') RETURNING id",
                (tid,),
            ).fetchone()
            cid = call_row[0] if not isinstance(call_row, dict) else call_row["id"]
            db_conn.execute(
                "INSERT INTO order_call_attempts (order_id, tenant_id, attempt_no, call_id) VALUES (%s, %s, 1, %s)",
                (oid, tid, cid),
            )
            with patch("app.services.order_service.deliver_async"), \
                 patch("app.services.billing_service.finalize_call_billing", return_value={"charge": Decimal("0"), "billable_seconds": 5, "wallet_transaction_id": None}):
                update_status_from_call(oid, tid, "completed", "0", 5, call_id=cid)

            updated = db_conn.execute("SELECT order_status, metadata FROM orders WHERE id = %s", (oid,)).fetchone()
            meta = updated[1] if not isinstance(updated, dict) else updated["metadata"]
            assert meta.get("human_agent_requested") is True

            payload = build_payload({"id": oid, "external_order_id": "V2-AGENT-1", "order_status": "UNREACHABLE", "phone": "+8801711223344"})
            assert payload["event"] == "order.agent_requested"

    def test_sqlite_legacy_schema_independent(self):
        """Verify SQLite backend initializes and executes queries independently of PostgreSQL."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name
        try:
            conn = sqlite3.connect(db_path)
            with open("schema.sql", "r", encoding="utf-8") as f:
                conn.executescript(f.read())
            conn.execute("INSERT INTO app_settings (key, value) VALUES ('TEST_KEY', 'ok')")
            row = conn.execute("SELECT value FROM app_settings WHERE key = 'TEST_KEY'").fetchone()
            assert row[0] == "ok"
            conn.close()
        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)
