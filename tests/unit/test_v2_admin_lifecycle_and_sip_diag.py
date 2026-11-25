"""Regression tests for V2 Admin Merchant Lifecycle, Plan/TTS Cache, and SIP Trunk Diagnostics."""
from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
from decimal import Decimal

import bcrypt
import pytest

from app.db import db
from app.extensions import redis_ext
from app.models.sip_provider import (
    add_sip_provider,
    get_sip_provider_by_id,
    reconcile_stale_calls,
    test_connection as run_sip_diagnostic,
    update_sip_provider,
)
from app.models.tenant import create_tenant, get_all_tenants, get_tenant_by_id
from app.services.admin_tenant_lifecycle import (
    archive_tenant,
    build_cleanup_audit_preview,
    bulk_archive_tenants,
    bulk_cleanup_synthetic_tenants,
    delete_tenant_safely,
    edit_tenant_profile,
    inspect_tenant_dependencies,
    list_synthetic_cleanup_candidates,
)
from app.services.wallet_service import credit as credit_wallet


def _make_super_admin(email: str, password: str = "SuperAdminPass123!") -> int:
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    return db.insert_returning_id(
        "INSERT INTO users (tenant_id, email, password_hash, role, email_verified) "
        "VALUES (NULL, %s, %s, 'super_admin', true) RETURNING id",
        (email, pw_hash),
    )


def _login(client, email: str, password: str):
    return client.post("/auth/login", data={"email": email, "password": password}, follow_redirects=True)


def _header(msg: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}:\s*([^\r\n]+)", msg, re.I | re.M)
    return m.group(1).strip() if m else ""


def _start_mock_sip_server(mode: str, expected_user: str = "trunkuser", expected_secret: str = "trunkpass"):
    """Spawn a localhost UDP SIP responder in a background thread."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    port = sock.getsockname()[1]
    stop = threading.Event()
    received_methods: list[str] = []
    received_raw: list[str] = []

    def _reply(addr, req: str, status_line: str, extra: str = ""):
        via = _header(req, "Via")
        frm = _header(req, "From")
        to = _header(req, "To")
        if ";tag=" not in to.lower():
            to = f"{to};tag=mocktag1"
        cid = _header(req, "Call-ID")
        cseq = _header(req, "CSeq")
        resp = (
            f"SIP/2.0 {status_line}\r\n"
            f"Via: {via}\r\n"
            f"From: {frm}\r\n"
            f"To: {to}\r\n"
            f"Call-ID: {cid}\r\n"
            f"CSeq: {cseq}\r\n"
            f"{extra}"
            "Content-Length: 0\r\n\r\n"
        )
        sock.sendto(resp.encode("utf-8"), addr)

    def _loop():
        nonce = "v2diagnonce1"
        realm = "ccall.mock"
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            req = data.decode("utf-8", errors="ignore")
            received_raw.append(req)
            first = req.split("\r\n", 1)[0]
            method = first.split(" ", 1)[0].upper()
            received_methods.append(method)

            if mode == "always_404":
                _reply(addr, req, "404 Not Found")
            elif mode == "digest_200":
                if method == "OPTIONS":
                    _reply(addr, req, "405 Method Not Allowed")
                elif method == "REGISTER":
                    auth_hdr = _header(req, "Authorization")
                    if not auth_hdr:
                        _reply(
                            addr,
                            req,
                            "401 Unauthorized",
                            f'WWW-Authenticate: Digest realm="{realm}", nonce="{nonce}", algorithm=MD5\r\n',
                        )
                    else:
                        uri_m = re.search(r'uri="([^"]+)"', auth_hdr)
                        resp_m = re.search(r'response="([0-9a-f]+)"', auth_hdr)
                        uri = uri_m.group(1) if uri_m else ""
                        ha1 = hashlib.md5(f"{expected_user}:{realm}:{expected_secret}".encode()).hexdigest()
                        ha2 = hashlib.md5(f"REGISTER:{uri}".encode()).hexdigest()
                        expected_dig = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
                        if resp_m and resp_m.group(1) == expected_dig:
                            _reply(addr, req, "200 OK")
                        else:
                            _reply(addr, req, "403 Forbidden")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return port, stop, received_methods, sock, received_raw


def test_sip_404_not_reported_as_success(app, monkeypatch):
    monkeypatch.setenv("SIP_DIAGNOSTIC_TIMEOUT_SECONDS", "0.6")
    port, stop, methods, srv_sock, _ = _start_mock_sip_server("always_404")
    try:
        with app.app_context():
            pid = add_sip_provider("Bad 404 Trunk", "127.0.0.1", port, "u1", "VerySecretPw1!", max_channels=12)
            res = run_sip_diagnostic(pid)
        assert res["success"] is False
        assert res["udp_reachable"] is True
        assert res["options_status_code"] == 404
        assert res["register_status_code"] == 404
        assert res["provider_readiness"] == "sip_not_found"
        assert res["outbound_call_verified"] is False
        assert "No outbound call" in res["caveat"]
        assert "VerySecretPw1!" not in json.dumps(res)
        assert "OPTIONS" in methods and "REGISTER" in methods
    finally:
        stop.set()
        srv_sock.close()


def test_sip_digest_401_to_200_register_passes_and_never_places_call(app, monkeypatch):
    monkeypatch.setenv("SIP_DIAGNOSTIC_TIMEOUT_SECONDS", "0.6")
    port, stop, methods, srv_sock, raw_packets = _start_mock_sip_server("digest_200", "u_ok", "VerySecretPw2!")
    try:
        with app.app_context():
            pid = add_sip_provider("Digest OK Trunk", "127.0.0.1", port, "u_ok", "VerySecretPw2!", max_channels=25)
            res = run_sip_diagnostic(pid)
        assert res["success"] is True
        assert res["udp_reachable"] is True
        assert res["options_status_code"] == 405
        assert res["auth_challenge"] is True
        assert res["auth_realm"] == "ccall.mock"
        assert res["register_status_code"] == 200
        assert res["provider_readiness"] == "registered"
        assert res["outbound_call_verified"] is False
        assert res["registration_non_disruptive"] is True
        assert "INVITE" not in methods
        # Ensure RFC 3261 binding-query REGISTER omits Contact/Expires so active trunk registrations are never modified
        reg_packets = [pkt for pkt in raw_packets if pkt.startswith("REGISTER ")]
        assert len(reg_packets) == 2
        for pkt in reg_packets:
            assert "\r\nContact:" not in pkt and "\r\nExpires:" not in pkt
        assert "VerySecretPw2!" not in json.dumps(res)
    finally:
        stop.set()
        srv_sock.close()


def test_sip_wrong_password_rejected_403(app, monkeypatch):
    monkeypatch.setenv("SIP_DIAGNOSTIC_TIMEOUT_SECONDS", "0.6")
    port, stop, _, srv_sock, _ = _start_mock_sip_server("digest_200", "u_ok", "real_secret")
    try:
        with app.app_context():
            pid = add_sip_provider("Bad Auth Trunk", "127.0.0.1", port, "u_ok", "wrong_secret_99", max_channels=10)
            res = run_sip_diagnostic(pid)
        assert res["success"] is False
        assert res["auth_challenge"] is True
        assert res["register_status_code"] == 403
        assert res["provider_readiness"] == "auth_failed"
        assert "wrong_secret_99" not in json.dumps(res)
    finally:
        stop.set()
        srv_sock.close()


def test_sip_provider_validation_duplicates_and_stale_reconciliation(app):
    with app.app_context():
        with pytest.raises(ValueError, match="SIP port"):
            add_sip_provider("P1", "127.0.0.1", 70000, "u", "p")
        with pytest.raises(ValueError, match="Max channels"):
            add_sip_provider("P2", "127.0.0.1", 5060, "u", "p", max_channels=0)
        with pytest.raises(ValueError, match="Invalid SIP domain"):
            add_sip_provider("P3", "http://bad host/", 5060, "u", "p")

        pid = add_sip_provider("Alpha Carrier", "sip1.example.com", 5060, "acct1", "pass1", max_channels=15)
        assert get_sip_provider_by_id(pid)["max_channels"] == 15

        with pytest.raises(ValueError, match="already exists"):
            add_sip_provider("alpha carrier", "sip2.example.com", 5060, "acct2", "pass2")
        with pytest.raises(ValueError, match="same domain, port, and username"):
            add_sip_provider("Beta Carrier", "SIP1.EXAMPLE.COM", 5060, "acct1", "pass2")

        update_sip_provider(pid, "Alpha Carrier Updated", "sip1.example.com", 5062, "acct1", max_channels=30)
        assert get_sip_provider_by_id(pid)["max_channels"] == 30

        # Insert: (1) truly stale abandoned call, (2) genuinely active call in live registry, (3) recently answered call
        tid = create_tenant({"business_name": "Stale Call Tenant", "email": "stale_call@test.local", "status": "active"})
        stale_cid = db.insert_returning_id(
            """
            INSERT INTO calls (tenant_id, number, caller_id, sip_gateway_id, status, started_at)
            VALUES (%s, '01700000001', '09600000001', %s, 'calling', now() - interval '90 minutes')
            RETURNING id
            """,
            (tid, pid),
        )
        answered_recent_cid = db.insert_returning_id(
            """
            INSERT INTO calls (tenant_id, number, caller_id, sip_gateway_id, status, started_at, answered_at)
            VALUES (%s, '01700000002', '09600000001', %s, 'answered', now() - interval '70 minutes', now() - interval '5 minutes')
            RETURNING id
            """,
            (tid, pid),
        )
        live_cid = db.insert_returning_id(
            """
            INSERT INTO calls (tenant_id, number, caller_id, sip_gateway_id, status, started_at)
            VALUES (%s, '01700000003', '09600000001', %s, 'calling', now() - interval '75 minutes')
            RETURNING id
            """,
            (tid, pid),
        )
        from call_engine import active_calls as registry
        registry.register_active_call(
            live_cid,
            tenant_id=tid,
            order_id=None,
            call_type="direct",
            phone="01700000003",
            caller_id="09600000001",
            sip_gateway_id=pid,
            status="answered",
        )
        try:
            closed = reconcile_stale_calls(provider_id=pid, stale_minutes=60)
            assert closed == 1
            assert db.query_one("SELECT status FROM calls WHERE id = %s", (stale_cid,))["status"] == "failed"
            assert db.query_one("SELECT status FROM calls WHERE id = %s", (answered_recent_cid,))["status"] == "answered"
            assert db.query_one("SELECT status FROM calls WHERE id = %s", (live_cid,))["status"] == "calling"
        finally:
            registry.remove_active_call(live_cid)


def test_multi_signal_cleanup_preview_and_bulk_archive(app):
    with app.app_context():
        admin_id = _make_super_admin("audit_admin@ccall.com", "AdminSecret99!")
        synth_empty = create_tenant({"business_name": "Synth Empty", "email": "synth_empty@test.local", "status": "active"})
        synth_bal = create_tenant({"business_name": "QueueTest-bal", "email": "qtest-bal@test.example", "status": "active"})
        db.execute("UPDATE wallets SET balance = 500.00 WHERE tenant_id = %s", (synth_bal,))
        real_test_name = create_tenant({"business_name": "Test Merchant RealDomain", "email": "owner@realstore.bd", "status": "active"})

        audit = build_cleanup_audit_preview(limit=200)
        by_id = {a["id"]: a for a in audit["all_accounts"]}

        assert by_id[synth_empty]["classification"] == "synthetic_empty"
        assert by_id[synth_empty]["safe_delete_eligible"] is True

        assert by_id[synth_bal]["classification"] == "synthetic_with_balance_or_activity"
        assert by_id[synth_bal]["safe_delete_eligible"] is False
        assert by_id[synth_bal]["archive_eligible"] is True

        # Must NEVER classify as synthetic based solely on business name!
        assert by_id[real_test_name]["classification"] in ("manual_review", "protected_merchant")
        assert by_id[real_test_name]["safe_delete_eligible"] is False

        arch_res = bulk_archive_tenants([synth_bal], admin_id, "AdminSecret99!", "ARCHIVE", reason="QA cleanup")
        assert [a["id"] for a in arch_res["archived"]] == [synth_bal]
        assert get_tenant_by_id(synth_bal)["status"] == "cancelled"


def test_merchant_lifecycle_guards_archive_delete_cleanup_and_sql_filtering(client, app):
    with app.app_context():
        admin_id = _make_super_admin("lifecycle_admin@test.local", "AdminSecret99!")
        free_plan = db.query_one("SELECT id FROM plans ORDER BY id ASC LIMIT 1")
        plan_id = free_plan["id"] if free_plan else None

        # Financial tenant (wallet tx) -> hard delete blocked, archive succeeds
        fin_tid = create_tenant({"business_name": "Paid Merchant", "email": "paid_merchant@company.com", "status": "active", "plan_id": plan_id})
        credit_wallet(fin_tid, Decimal("150.00"), "Initial deposit", "test_seed")

        deps_fin = inspect_tenant_dependencies(fin_tid)
        assert deps_fin["has_financial_history"] is True
        assert deps_fin["delete_eligible"] is False

        with pytest.raises(ValueError, match="financial or billing history"):
            delete_tenant_safely(fin_tid, admin_id, "AdminSecret99!", "DELETE")

        arch = archive_tenant(fin_tid, admin_id, reason="Test archive")
        assert arch["archived"] is True
        assert get_tenant_by_id(fin_tid)["status"] == "cancelled"

        # Clean empty tenant -> edit + password-confirmed delete works + audit log written
        clean_tid = create_tenant({"business_name": "Temp Clean Biz", "email": "temp_clean@test.local", "status": "active", "plan_id": plan_id})
        edit_tenant_profile(clean_tid, admin_id, "Renamed Clean Biz", "renamed_clean@test.local", status="suspended")
        assert get_tenant_by_id(clean_tid)["business_name"] == "Renamed Clean Biz"
        assert get_tenant_by_id(clean_tid)["status"] == "suspended"

        with pytest.raises(ValueError, match="Incorrect super-admin password"):
            delete_tenant_safely(clean_tid, admin_id, "WrongPassword!", "DELETE")

        deleted_res = delete_tenant_safely(clean_tid, admin_id, "AdminSecret99!", "DELETE")
        assert deleted_res["deleted"] is True
        assert get_tenant_by_id(clean_tid) is None
        audit_row = db.query_one(
            "SELECT * FROM audit_log WHERE action = 'tenant_deleted' AND target_id = %s",
            (clean_tid,),
        )
        assert audit_row is not None

        # Synthetic bulk cleanup selects synthetic empty tenants only
        s1 = create_tenant({"business_name": "QA Synth One", "email": "synth_one@test.local", "status": "active"})
        s2 = create_tenant({"business_name": "Real NonSynth", "email": "owner@realshop.bd", "status": "active"})
        candidates = list_synthetic_cleanup_candidates()
        candidate_ids = {c["id"] for c in candidates}
        assert s1 in candidate_ids
        assert s2 not in candidate_ids
        assert fin_tid not in candidate_ids

        cleanup_res = bulk_cleanup_synthetic_tenants([s1, s2], admin_id, "AdminSecret99!", "DELETE")
        assert [d["id"] for d in cleanup_res["deleted"]] == [s1]
        assert any(s["id"] == s2 for s in cleanup_res["skipped"])
        assert get_tenant_by_id(s2) is not None

        # SQL filtering excludes archived unless requested
        res_active = get_all_tenants(page=1, per_page=20, search="Paid Merchant", include_archived=False)
        assert res_active["total"] == 0
        res_with_arch = get_all_tenants(page=1, per_page=20, search="Paid Merchant", include_archived=True)
        assert res_with_arch["total"] == 1


def test_tts_mode_cache_invalidation_plan_duplicate_and_htmx_diag(client, app, monkeypatch):
    monkeypatch.setenv("SIP_DIAGNOSTIC_TIMEOUT_SECONDS", "0.5")
    with app.app_context():
        _make_super_admin("admin_tts_cache@ccall.com", "AdminSecret99!")
        tid = create_tenant({"business_name": "TTS Cache Biz", "email": "tts_cache@ccall.com", "status": "active"})
        # Warm Redis cache with byok
        cached_before = get_tenant_by_id(tid)
        assert (cached_before.get("tts_credential_mode") or "byok") == "byok"

    login_resp = _login(client, "admin_tts_cache@ccall.com", "AdminSecret99!")
    assert login_resp.status_code == 200

    # Switch TTS mode to byok again or platform_managed? Let's test plan duplicate and HTMX SIP partial + CSV export
    with app.app_context():
        plan = db.query_one("SELECT id, name FROM plans WHERE archived_at IS NULL ORDER BY id ASC LIMIT 1")
        plan_id = plan["id"]

        # Simulate stale cache key scenario by calling customer_tts override to byok after setting DB to platform_managed
        db.execute("UPDATE tenants SET tts_credential_mode = 'platform_managed' WHERE id = %s", (tid,))
        redis_ext.client.delete(f"tenant:settings:{tid}")
        assert get_tenant_by_id(tid)["tts_credential_mode"] == "platform_managed"

    resp = client.post(
        f"/admin/customers/{tid}/tts-mode",
        data={"tts_credential_mode": "byok", "reason": "Revert to BYOK"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with app.app_context():
        # Cache must NOT serve stale platform_managed
        assert get_tenant_by_id(tid)["tts_credential_mode"] == "byok"

    # Test plan duplication
    dup_resp = client.post(f"/admin/plans/{plan_id}/duplicate", follow_redirects=True)
    assert dup_resp.status_code == 200
    assert b"-copy" in dup_resp.data

    # Test TTS usage CSV export
    csv_resp = client.get("/admin/tts-usage/export.csv")
    assert csv_resp.status_code == 200
    assert "Estimated Provider Cost (USD)" in csv_resp.get_data(as_text=True)

    # Test HTMX provider diagnostic response returns HTML (not raw JSON) on 404
    port, stop, _, srv_sock = _start_mock_sip_server("always_404")
    try:
        with app.app_context():
            pid = add_sip_provider("HTMX 404 Trunk", "127.0.0.1", port, "u_htmx", "secret_pw")
        htmx_resp = client.post(f"/admin/providers/{pid}/test", headers={"HX-Request": "true"})
        html = htmx_resp.get_data(as_text=True)
        assert htmx_resp.status_code == 200
        assert "data-provider-test-result" in html
        assert "FAIL" in html
        assert "404 Not Found" in html
        assert "secret_pw" not in html
    finally:
        stop.set()
        srv_sock.close()
