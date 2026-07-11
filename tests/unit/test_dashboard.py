from __future__ import annotations

import datetime
from decimal import Decimal
import pytest
from app.db import db
from app.models.tenant import create_tenant
from app.models.sip_provider import create_provider
from app.models.wallet import get_wallet
from app.services.dashboard_service import get_dashboard_stats, get_chart_data

def test_get_dashboard_stats_and_chart(app):
    with app.app_context():
        tenant_id = create_tenant("Dash Store", "dash@store.com")
        
        # 1. Test empty stats
        stats = get_dashboard_stats(tenant_id)
        assert stats["today_orders"] == 0
        assert stats["today_confirmed"] == 0
        assert stats["wallet_balance"] == Decimal("0.0")
        assert stats["success_rate_7d"] == 0.0
        assert stats["calls_today"] == 0
        assert stats["minutes_today"] == 0
        assert stats["cost_today"] == Decimal("0.0")
        assert len(stats["recent_orders"]) == 0
        
        # 2. Add some test data
        prov_id = create_provider({
            "name": "Test Gateway",
            "sip_domain": "sip.testgateway.com",
            "sip_port": 5060,
            "sip_user": "testuser",
            "sip_password": "testpassword"
        })
        
        # Insert today's order
        db.execute(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, created_at)
            VALUES (%s, 'ORD-001', '+8801711111111', 'Sakib', 1200.0, 'CONFIRMED', now())
            """,
            (tenant_id,)
        )
        # Insert yesterday's order
        db.execute(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status, created_at)
            VALUES (%s, 'ORD-002', '+8801722222222', 'Tamim', 800.0, 'CANCELLED', now() - interval '1 day')
            """,
            (tenant_id,)
        )
        
        # Credit wallet
        db.execute(
            "UPDATE wallets SET balance = 500.0 WHERE tenant_id = %s",
            (tenant_id,)
        )
        wallet = get_wallet(tenant_id)
        
        # Insert a call today
        db.execute(
            """
            INSERT INTO calls (tenant_id, number, status, started_at, talk_seconds)
            VALUES (%s, '+8801711111111', 'completed', now(), 90)
            """,
            (tenant_id,)
        )
        
        # Insert a debit transaction today
        db.execute(
            """
            INSERT INTO wallet_transactions (wallet_id, tenant_id, amount, balance_after, type, description, ref_id)
            VALUES (%s, %s, 5.0, 495.0, 'debit', 'Call charge', 'ref_1')
            """,
            (wallet["id"], tenant_id)
        )
        
        # Invalidate cache for the tenant
        from app.models.tenant import invalidate_tenant_caches
        invalidate_tenant_caches(tenant_id)
        
        # Verify stats
        stats_updated = get_dashboard_stats(tenant_id)
        assert stats_updated["today_orders"] == 1
        assert stats_updated["today_confirmed"] == 1
        assert stats_updated["wallet_balance"] == Decimal("500.0")
        assert stats_updated["calls_today"] == 1
        assert stats_updated["minutes_today"] == 2  # ceil(90/60) = 2
        assert stats_updated["cost_today"] == Decimal("5.0")
        assert stats_updated["success_rate_7d"] == 50.0  # 1 confirmed out of 2 total
        assert len(stats_updated["recent_orders"]) == 2
        
        # Verify chart data
        chart = get_chart_data(tenant_id, days=7)
        assert len(chart["labels"]) == 7
        assert len(chart["confirmed"]) == 7
        assert len(chart["cancelled"]) == 7
        
        # Today is last index in 7 days
        assert chart["confirmed"][-1] == 1
        # Yesterday is 2nd to last index
        assert chart["cancelled"][-2] == 1

def test_dashboard_api_endpoints(auth_client, create_tenant, app):
    with app.app_context():
        tenant, user = create_tenant("API Dash Store", "apidash@store.com")
        client = auth_client((tenant, user))
        
        # 1. Test GET /api/dashboard/stats (JSON)
        res = client.get("/api/dashboard/stats", headers={"Accept": "application/json"})
        assert res.status_code == 200
        json_data = res.get_json()
        assert "today_orders" in json_data
        assert "wallet_balance" in json_data
        
        # 2. Test GET /api/dashboard/stats (HTMX fragment)
        res_htmx = client.get("/api/dashboard/stats", headers={"HX-Request": "true"})
        assert res_htmx.status_code == 200
        html_content = res_htmx.get_data(as_text=True)
        assert "Today's Orders" in html_content
        
        # 3. Test GET /api/dashboard/chart
        res_chart = client.get("/api/dashboard/chart")
        assert res_chart.status_code == 200
        chart_json = res_chart.get_json()
        assert "labels" in chart_json
        assert "confirmed" in chart_json
        
        # 4. Test GET /api/orders
        res_orders = client.get("/api/orders?limit=5")
        if res_orders.status_code != 200:
            print("ERROR ON /api/orders?limit=5:")
            print("Status:", res_orders.status_code)
            print("Headers:", res_orders.headers)
            print("Body:", res_orders.get_data(as_text=True))
        assert res_orders.status_code == 200
        assert "tr" in res_orders.get_data(as_text=True)

def test_quick_call_composer(auth_client, create_tenant, monkeypatch, app):
    with app.app_context():
        tenant, user = create_tenant("Composer Store", "composer@store.com")
        client = auth_client((tenant, user))
        tenant_id = tenant["id"]
        
        # Mock call_runner.start_call and start_direct_call
        start_call_called = False
        start_direct_called = False
        
        def mock_start_call(number, flow_id, *, name='', tenant_id=None, order_id=None, caller_id=None, **kwargs):
            nonlocal start_call_called
            start_call_called = True
            return 123
            
        def mock_start_direct_call(number, audio_name, repeat_count, message_text='', audio_mode='tts', *, name='', tenant_id=None, order_id=None, caller_id=None, **kwargs):
            nonlocal start_direct_called
            start_direct_called = True
            return 456

        from call_engine import call_runner
        monkeypatch.setattr(call_runner, "start_call", mock_start_call)
        monkeypatch.setattr(call_runner, "start_direct_call", mock_start_direct_call)
        monkeypatch.setattr("app.services.tts_service.generate_order_audio", lambda tenant_id, text, *args, **kwargs: "audio.ulaw")
        
        # Credit the balance first to bypass wallet checks
        db.execute("UPDATE wallets SET balance = 100.0 WHERE tenant_id = %s", (tenant_id,))
        
        # Insert a flow
        flow_id = db.insert_returning_id(
            "INSERT INTO flows (tenant_id, name) VALUES (%s, 'Test Flow') RETURNING id",
            (tenant_id,)
        )
        
        # 1. Launch flow call
        res_flow = client.post("/dashboard/quick-call", data={
            "phone": "01712345678",
            "call_type": "flow",
            "flow_id": str(flow_id)
        })
        assert res_flow.status_code == 200
        assert "Quick call dispatched" in res_flow.get_data(as_text=True)
        assert start_call_called is True
        
        # 2. Launch direct TTS call
        res_direct = client.post("/dashboard/quick-call", data={
            "phone": "01712345678",
            "call_type": "direct",
            "message_text": "হ্যালো বিশ্ব"
        })
        assert res_direct.status_code == 200
        assert "Quick call dispatched" in res_direct.get_data(as_text=True)
        assert start_direct_called is True

