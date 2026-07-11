# tests/unit/test_flows.py
from __future__ import annotations

import pytest
from app.db import db


@pytest.fixture(autouse=True)
def mock_generate_order_audio(monkeypatch):
    monkeypatch.setattr(
        "app.blueprints.customer.flows.generate_order_audio",
        lambda tenant_id, text, *args, **kwargs: "audio_basename"
    )



def test_create_and_manage_flows(app, create_tenant, auth_client):
    tenant_pair = create_tenant("Flow Store", "flow_admin@store.com")
    tenant, user = tenant_pair
    client = auth_client(tenant_pair)
    
    # 1. Access flows page
    resp = client.get("/flows")
    assert resp.status_code == 200
    assert b"Interactive IVR Voice Flows" in resp.data
    
    # 2. Create flow
    resp = client.post("/flows/new", data={
        "name": "Sales Flow",
        "description": "My sales department flow"
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Flow created" in resp.data
    
    # Verify flow in DB
    flow = db.query_one("SELECT * FROM flows WHERE tenant_id = %s AND name = 'Sales Flow'", (tenant["id"],))
    assert flow is not None
    assert flow["description"] == "My sales department flow"
    
    # 3. Access flow edit page
    resp = client.get(f"/flows/{flow['id']}")
    assert resp.status_code == 200
    assert b"Sales Flow" in resp.data
    
    # 4. Add node
    resp = client.post(f"/flows/{flow['id']}/nodes", data={
        "name": "Welcome Menu",
        "node_type": "menu",
        "prompt_text": "Welcome to our shop! For sales press 1, for support press 2.",
        "prompt_lang": "bn",
        "gather_timeout_seconds": "8",
        "max_retries": "2"
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Node added" in resp.data
    
    # Check node in DB
    node = db.query_one("SELECT * FROM flow_nodes WHERE flow_id = %s AND name = 'Welcome Menu'", (flow["id"],))
    assert node is not None
    assert node["node_type"] == "menu"
    
    # Verify flows table root_node_id got automatically set to this first node
    flow_updated = db.query_one("SELECT * FROM flows WHERE id = %s", (flow["id"],))
    assert flow_updated["root_node_id"] == node["id"]

    # 5. Add second node
    resp = client.post(f"/flows/{flow['id']}/nodes", data={
        "name": "Sales Announcement",
        "node_type": "message",
        "prompt_text": "All items are 20% off today.",
        "prompt_lang": "bn"
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Node added" in resp.data
    
    node2 = db.query_one("SELECT * FROM flow_nodes WHERE flow_id = %s AND name = 'Sales Announcement'", (flow["id"],))
    assert node2 is not None

    # 6. Map Keypress Transition
    resp = client.post(f"/flows/{flow['id']}/transitions", data={
        "node_id": str(node["id"]),
        "digit": "1",
        "next_node_id": str(node2["id"]),
        "label": "Press 1 for Sales"
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"mapped successfully" in resp.data

    # Check transition in DB
    trans = db.query_one("SELECT * FROM flow_transitions WHERE node_id = %s AND digit = '1'", (node["id"],))
    assert trans is not None
    assert trans["next_node_id"] == node2["id"]

    # 7. Update node
    resp = client.post(f"/flows/{flow['id']}/nodes/{node['id']}/update", data={
        "name": "Welcome Menu v2",
        "node_type": "menu",
        "prompt_text": "Updated text",
        "prompt_lang": "bn",
        "gather_timeout_seconds": "10",
        "max_retries": "3"
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Node updated" in resp.data

    node_updated = db.query_one("SELECT * FROM flow_nodes WHERE id = %s", (node["id"],))
    assert node_updated["name"] == "Welcome Menu v2"
    assert node_updated["gather_timeout_seconds"] == 10

    # 8. Delete Transition
    resp = client.post(f"/flows/{flow['id']}/transitions/{trans['id']}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Transition removed" in resp.data

    trans_deleted = db.query_one("SELECT * FROM flow_transitions WHERE id = %s", (trans["id"],))
    assert trans_deleted is None

    # 9. Delete Node
    resp = client.post(f"/flows/{flow['id']}/nodes/{node2['id']}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Node deleted" in resp.data

    node2_deleted = db.query_one("SELECT * FROM flow_nodes WHERE id = %s", (node2["id"],))
    assert node2_deleted is None

    # 10. Delete Flow
    resp = client.post(f"/flows/{flow['id']}/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Flow deleted" in resp.data

    flow_deleted = db.query_one("SELECT * FROM flows WHERE id = %s", (flow["id"],))
    assert flow_deleted is None


def test_cross_tenant_flow_restrictions(app, create_tenant, auth_client):
    # Setup Tenant A
    tenant_pair_a = create_tenant("Store A", "a@store.com")
    tenant_a, user_a = tenant_pair_a
    
    # Setup Tenant B
    tenant_pair_b = create_tenant("Store B", "b@store.com")
    tenant_b, user_b = tenant_pair_b
    
    # Create flow for Tenant B
    flow_b_id = db.insert_returning_id(
        "INSERT INTO flows (name, description, tenant_id) VALUES (%s, %s, %s) RETURNING id",
        ("Secret Flow", "Private B flow", tenant_b["id"])
    )
    
    # Log in as Tenant A
    client = auth_client(tenant_pair_a)
    
    # Try to access Tenant B's flow edit page
    resp = client.get(f"/flows/{flow_b_id}")
    assert resp.status_code == 302  # Redirects to flows list page
    
    # Try to post a new node to Tenant B's flow
    resp = client.post(f"/flows/{flow_b_id}/nodes", data={
        "name": "Hacked Node",
        "node_type": "hangup"
    })
    assert resp.status_code == 302  # Redirects
    
    # Try to delete Tenant B's flow
    resp = client.post(f"/flows/{flow_b_id}/delete")
    assert resp.status_code == 302  # Redirects
    
    # Flow B should still exist in DB
    flow_b = db.query_one("SELECT * FROM flows WHERE id = %s", (flow_b_id,))
    assert flow_b is not None


def test_ivr_engine_tenant_validation(app):
    from call_engine.ivr_engine import Flow, IvrCall, Node
    
    # Create flow A belonging to tenant 1
    flow = Flow(id=1, root_node_id=1, nodes={1: Node(id=1, name="Start")}, tenant_id=1)
    
    # Call from tenant 2 should fail on IvrCall initialization
    with pytest.raises(RuntimeError) as exc:
        IvrCall(
            target="01711111111",
            flow=flow,
            sip={"SIP_DOMAIN": "localhost", "SIP_PORT": "5060", "SIP_USER": "caller", "SIP_PASSWORD": "password"},
            on_event=lambda ev: None,
            tenant_id=2
        )
    assert "does not match" in str(exc.value)
