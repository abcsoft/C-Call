"""Permanent regression suite for Admin/Merchant shell layout, hx-boost navigation, CSP, and responsive bounds."""
from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

import bcrypt
import psycopg
import psycopg.rows
import pytest
from werkzeug.serving import make_server

from app import create_app
from app.extensions import db_pool, redis_ext
from tests.conftest import MockRedis, TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parent.parent.parent

ADMIN_ROUTES = [
    ("/admin/dashboard", "Platform Overview"),
    ("/admin/live-calls", "Live Call Monitor"),
    ("/admin/customers", "Tenants"),
    ("/admin/numbers", "SIP CLI Pool Manager"),
    ("/admin/providers", "SIP Trunk Gateways"),
    ("/admin/cdr", "Platform Call Records (CDR)"),
    ("/admin/revenue", "Revenue"),
    ("/admin/settings", "System Settings"),
    ("/admin/plans", "Plans"),
    ("/admin/platform-tts", "Platform TTS Credentials"),
    ("/admin/tts-usage", "TTS Usage"),
    ("/admin/tts-reservations", "TTS Reservations"),
    ("/admin/plans/create", "Create New Plan"),
]

MERCHANT_ROUTES = [
    ("/dashboard", "Merchant Console"),
    ("/live-calls", "Live Calls"),
    ("/orders", "Order Logs"),
    ("/script", "Voice Script Configurator"),
    ("/retry", "Retry & DND Settings"),
    ("/flows", "Interactive IVR Voice Flows"),
    ("/wallet", "Prepaid Wallet"),
    ("/settings/subscription", "Subscription & Billing"),
    ("/cdr", "Call Detail Records (CDR)"),
    ("/integration", "API & Integrations"),
    ("/settings", "SMS Fallback Settings"),
    ("/settings/team", "Team Management"),
]


class TestAdminAndMerchantLayoutContracts:
    """Fast template & HTTP layout checks guarding sidebar inheritance, hx-boost, and static scripts."""

    def test_admin_template_inheritance_and_base_assets(self):
        providers_tpl = (ROOT / "app" / "templates" / "admin" / "providers.html").read_text(encoding="utf-8")
        tts_res_tpl = (ROOT / "app" / "templates" / "admin" / "tts_reservations.html").read_text(encoding="utf-8")
        base_tpl = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")

        assert '{% extends "admin/base_admin.html" %}' in providers_tpl
        assert "{% block admin_content %}" in providers_tpl
        assert "{% block content %}" not in providers_tpl
        assert "overflow-x-auto" in providers_tpl

        assert '{% extends "admin/base_admin.html" %}' in tts_res_tpl
        assert "{% block admin_content %}" in tts_res_tpl
        assert "{% block content %}" not in tts_res_tpl

        assert 'name="htmx-config"' in base_tpl
        assert "chart.js" in base_tpl
        assert "js/alpine_components.js" in base_tpl

    @pytest.mark.parametrize("path,expected_h1", ADMIN_ROUTES)
    def test_admin_routes_preserve_admin_shell_direct_and_boosted(self, super_admin_client, path, expected_h1):
        for headers in ({}, {"HX-Request": "true", "HX-Boosted": "true"}):
            resp = super_admin_client.get(path, headers=headers)
            assert resp.status_code == 200, f"{path} failed with {resp.status_code}"
            html = resp.get_data(as_text=True)
            assert "C-CALL" in html and "ADMIN" in html, f"{path} lost Admin sidebar"
            assert "C-CALL IVR" not in html, f"{path} leaked Merchant sidebar"
            assert "<main" in html and "md:pl-64" in html
            assert expected_h1 in html

    @pytest.mark.parametrize("path,expected_h1", MERCHANT_ROUTES)
    def test_merchant_routes_preserve_merchant_shell_direct_and_boosted(self, tenant_client, path, expected_h1):
        for headers in ({}, {"HX-Request": "true", "HX-Boosted": "true"}):
            resp = tenant_client.get(path, headers=headers)
            assert resp.status_code == 200, f"{path} failed with {resp.status_code}"
            html = resp.get_data(as_text=True)
            assert "C-CALL" in html and "IVR" in html, f"{path} lost Merchant sidebar"
            assert "C-CALL ADMIN" not in html, f"{path} leaked Admin sidebar"
            assert "<main" in html and "md:pl-64" in html
            assert expected_h1 in html

    def test_merchant_cannot_render_admin_providers(self, tenant_client):
        resp = tenant_client.get("/admin/providers", follow_redirects=False)
        assert resp.status_code in (302, 403)
        assert "SIP Trunk Gateways" not in resp.get_data(as_text=True)

    def test_shared_alpine_components_asset_served(self, client):
        resp = client.get("/static/js/alpine_components.js")
        assert resp.status_code == 200
        js = resp.get_data(as_text=True)
        assert "global.liveCallMonitor" in js
        assert "global.retrySettings" in js


def _assert_layout_geometry(page, expected_brand_tokens: tuple[str, str], expected_h1: str | None = None, mobile: bool = False):
    """Assert visible sidebar (desktop) or drawer toggle (mobile), H1, main shift, and zero viewport overflow."""
    metrics = page.evaluate(
        """(mobile) => {
            const h1 = document.querySelector('h1');
            const main = document.querySelector('main');
            const navContainer = Array.from(document.querySelectorAll('nav')).find((n) => {
                const r = n.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            const navLinks = navContainer
                ? Array.from(navContainer.querySelectorAll('a')).filter((el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                })
                : [];
            const sidebarWrapper = navContainer ? navContainer.closest('.md\\\\:w-64, aside, [role="dialog"]') : null;
            const actionButtons = main
                ? Array.from(main.querySelectorAll('button, a')).filter((el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                })
                : [];
            const clippedViewportControls = [];
            for (const el of actionButtons) {
                const scrollParent = el.closest('.overflow-x-auto');
                const r = el.getBoundingClientRect();
                if (scrollParent) {
                    const pr = scrollParent.getBoundingClientRect();
                    if (r.right > pr.right + 2 && scrollParent.scrollWidth <= scrollParent.clientWidth + 2) {
                        clippedViewportControls.push(el.innerText.trim() + ':' + Math.round(r.right) + '>' + Math.round(pr.right));
                    }
                } else if (r.right > window.innerWidth + 2) {
                    clippedViewportControls.push(el.innerText.trim() + ':' + Math.round(r.right) + '>' + window.innerWidth);
                }
            }
            return {
                brandText: sidebarWrapper ? sidebarWrapper.innerText : (document.body ? document.body.innerText : ''),
                h1Text: h1 ? h1.innerText.trim() : null,
                h1Rect: h1 ? h1.getBoundingClientRect().toJSON() : null,
                mainLeft: main ? Math.round(main.getBoundingClientRect().left) : -1,
                navWidth: navContainer ? Math.round(navContainer.getBoundingClientRect().width) : 0,
                visibleLinkCount: navLinks.length,
                scrollWidth: document.documentElement.scrollWidth,
                innerWidth: window.innerWidth,
                clippedViewportControls,
            };
        }""",
        mobile,
    )
    assert metrics["h1Text"] and metrics["h1Rect"] and metrics["h1Rect"]["width"] > 0, f"Missing H1 on {page.url}"
    if expected_h1:
        assert expected_h1 in metrics["h1Text"], f"Expected {expected_h1!r} in {metrics['h1Text']!r} on {page.url}"
    assert metrics["scrollWidth"] <= metrics["innerWidth"] + 1, (
        f"Horizontal overflow on {page.url}: {metrics['scrollWidth']} > {metrics['innerWidth']}"
    )
    assert not metrics["clippedViewportControls"], (
        f"Clipped buttons/links on {page.url}: {metrics['clippedViewportControls']}"
    )
    if not mobile:
        assert metrics["navWidth"] >= 180 and metrics["visibleLinkCount"] >= 8, (
            f"Sidebar missing/collapsed on {page.url}: width={metrics['navWidth']}, links={metrics['visibleLinkCount']}"
        )
        assert metrics["mainLeft"] >= 240, f"Main content not shifted by sidebar on {page.url}: left={metrics['mainLeft']}"
        for tok in expected_brand_tokens:
            assert tok in metrics["brandText"], f"Brand token {tok!r} missing from sidebar on {page.url}"


@pytest.mark.real_db
class TestPlaywrightLiveLayoutRegression:
    """Real Playwright Chromium checks covering layout geometry, hx-boost transitions, HTMX swaps, and responsive viewports."""

    def test_admin_and_merchant_ui_layout_and_navigation(self, monkeypatch):
        playwright_sync = pytest.importorskip("playwright.sync_api")

        run_id = secrets.token_hex(5)
        admin_email = f"ui_reg_admin_{run_id}@test.example"
        merchant_email = f"ui_reg_merch_{run_id}@test.example"
        provider_name = f"UI_REG_GW_{run_id}"
        password_plain = "UiRegPass123!"
        password_hash = bcrypt.hashpw(password_plain.encode(), bcrypt.gensalt(rounds=4)).decode()

        created: dict[str, int | None] = {
            "admin_user_id": None,
            "tenant_id": None,
            "merchant_user_id": None,
            "provider_id": None,
        }

        with psycopg.connect(TEST_DATABASE_URL, autocommit=False, row_factory=psycopg.rows.dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (email, password_hash, role, tenant_id, email_verified)"
                    " VALUES (%s, %s, 'super_admin', NULL, true) RETURNING id",
                    (admin_email, password_hash),
                )
                created["admin_user_id"] = cur.fetchone()["id"]

                cur.execute(
                    "INSERT INTO tenants (business_name, email, status) VALUES (%s, %s, 'active') RETURNING id",
                    (f"UI Reg Merch {run_id}", merchant_email),
                )
                created["tenant_id"] = cur.fetchone()["id"]

                cur.execute("INSERT INTO wallets (tenant_id, balance) VALUES (%s, 100.00)", (created["tenant_id"],))
                cur.execute(
                    "INSERT INTO users (email, password_hash, role, tenant_id, email_verified)"
                    " VALUES (%s, %s, 'admin', %s, true) RETURNING id",
                    (merchant_email, password_hash, created["tenant_id"]),
                )
                created["merchant_user_id"] = cur.fetchone()["id"]
            conn.commit()

        # Boot an isolated in-process WSGI server with MockRedis so browser sessions never depend on external Redis.
        os.environ["FLASK_ENV"] = "testing"
        os.environ["DATABASE_URL"] = TEST_DATABASE_URL
        os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-that-is-long-enough-for-tests-32x")
        os.environ.setdefault("PLATFORM_SECRET_ENCRYPTION_KEY", "kf5PjsX2XFRAHr4o5KMstpKlovTExW7PLCg-y6Hqa88=")

        mock_redis = MockRedis()
        redis_ext._client = mock_redis
        live_app = create_app("testing")
        live_app.config["SERVER_NAME"] = None
        try:
            live_app.session_interface.client = mock_redis
        except AttributeError:
            pass
        live_app.config["SESSION_REDIS"] = mock_redis

        with live_app.app_context():
            from app.models.sip_provider import create_provider

            created["provider_id"] = create_provider({
                "name": provider_name,
                "sip_domain": "sip.example.invalid",
                "sip_port": 5060,
                "sip_user": f"reguser_{run_id}",
                "sip_password": "reg_secret_pass_unused",
                "max_channels": 15,
            })

        def _fake_diag(_provider_id: int) -> dict:
            return {
                "success": True,
                "udp_reachable": True,
                "provider_readiness": "ready",
                "readiness_label": "REGISTER 200 OK (OPTIONS 404)",
                "latency_ms": 12,
                "options_status_code": 404,
                "register_status_code": 200,
                "steps": [
                    {"label": "UDP OPTIONS", "status": "warn", "detail": "404 Not Found"},
                    {"label": "SIP REGISTER", "status": "pass", "detail": "200 OK"},
                ],
                "explanation": "Synthetic non-calling diagnostic response.",
                "suggested_action": "None required.",
                "caveat": "Mocked test diagnostic.",
            }

        monkeypatch.setattr("app.blueprints.admin.routes_numbers.test_connection", _fake_diag)

        server = make_server("127.0.0.1", 0, live_app, threaded=True)
        base_url = f"http://127.0.0.1:{server.server_port}"
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        console_errors: list[str] = []
        page_errors: list[str] = []
        server_errors: list[str] = []

        def _login_via_form(page, email: str):
            page.goto(f"{base_url}/auth/login", wait_until="networkidle")
            page.fill('input[name="email"]', email)
            page.fill('input[name="password"]', password_plain)
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")

        try:
            with playwright_sync.sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                page = context.new_page()
                page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: page_errors.append(str(e)))
                page.on("response", lambda r: server_errors.append(f"{r.status} {r.url}") if r.status >= 500 else None)

                # 1. Super Admin layout, hx-boost transitions, back/forward, reload, and SIP diagnostic swap
                _login_via_form(page, admin_email)
                page.goto(f"{base_url}/admin/providers", wait_until="networkidle")
                _assert_layout_geometry(page, ("C-CALL", "ADMIN"), "SIP Trunk Gateways")

                # Trigger HTMX SIP diagnostic button and confirm separate badges + preserved sidebar
                diag_btn = page.locator(f"#test-res-{created['provider_id']}").locator("..").locator("button")
                diag_btn.click()
                page.locator(f"#test-res-{created['provider_id']} [data-options-badge]").wait_for(state="visible", timeout=5000)
                assert "OPTIONS: 404" in page.locator(f"#test-res-{created['provider_id']}").inner_text()
                assert "REGISTER: 200" in page.locator(f"#test-res-{created['provider_id']}").inner_text()
                _assert_layout_geometry(page, ("C-CALL", "ADMIN"), "SIP Trunk Gateways")

                # Navigate via hx-boost sidebar links across critical Admin views
                for nav_href, h1_text in [
                    ("/admin/revenue", "Revenue"),
                    ("/admin/customers", "Tenants"),
                    ("/admin/numbers", "SIP CLI Pool Manager"),
                    ("/admin/providers", "SIP Trunk Gateways"),
                ]:
                    page.locator(f'.md\\:w-64 nav a[href="{nav_href}"]').first.click()
                    page.wait_for_url(f"**{nav_href}", timeout=5000)
                    page.wait_for_load_state("networkidle")
                    _assert_layout_geometry(page, ("C-CALL", "ADMIN"), h1_text)

                # Browser reload, back, forward must keep Admin sidebar intact
                page.reload(wait_until="networkidle")
                _assert_layout_geometry(page, ("C-CALL", "ADMIN"), "SIP Trunk Gateways")
                page.go_back(wait_until="networkidle")
                page.wait_for_url("**/admin/numbers", timeout=5000)
                _assert_layout_geometry(page, ("C-CALL", "ADMIN"), "SIP CLI Pool Manager")
                page.go_forward(wait_until="networkidle")
                page.wait_for_url("**/admin/providers", timeout=5000)
                _assert_layout_geometry(page, ("C-CALL", "ADMIN"), "SIP Trunk Gateways")

                # Direct navigation to fixed /admin/tts-reservations
                page.goto(f"{base_url}/admin/tts-reservations", wait_until="networkidle")
                _assert_layout_geometry(page, ("C-CALL", "ADMIN"), "TTS Reservations")

                # Laptop 1366x768 must not clip table action controls
                page.set_viewport_size({"width": 1366, "height": 768})
                for url_path, h1_text in [
                    ("/admin/providers", "SIP Trunk Gateways"),
                    ("/admin/customers", "Tenants"),
                    ("/admin/numbers", "SIP CLI Pool Manager"),
                ]:
                    page.goto(f"{base_url}{url_path}", wait_until="networkidle")
                    _assert_layout_geometry(page, ("C-CALL", "ADMIN"), h1_text)

                # Mobile 390x844 drawer open & close cleanly without overflow
                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(f"{base_url}/admin/providers", wait_until="networkidle")
                _assert_layout_geometry(page, ("C-CALL", "ADMIN"), "SIP Trunk Gateways", mobile=True)
                page.locator('button:has-text("Open sidebar")').first.click()
                page.wait_for_timeout(150)
                assert page.locator('[role="dialog"] nav a[href="/admin/customers"]').first.is_visible()

                # 2. Merchant login, hx-boost navigation across Chart.js / Alpine / Orders pages
                page.set_viewport_size({"width": 1024, "height": 768})
                page.goto(f"{base_url}/auth/logout", wait_until="networkidle")
                _login_via_form(page, merchant_email)
                for url_path, h1_text in [
                    ("/dashboard", "Merchant Console"),
                    ("/orders", "Order Logs"),
                    ("/retry", "Retry & DND Settings"),
                    ("/live-calls", "Live Calls"),
                    ("/settings/subscription", "Subscription & Billing"),
                ]:
                    page.locator(f'.md\\:w-64 nav a[href="{url_path}"]').first.click()
                    page.wait_for_url(f"**{url_path}", timeout=5000)
                    page.wait_for_load_state("networkidle")
                    _assert_layout_geometry(page, ("C-CALL", "IVR"), h1_text)

                # Re-visit /dashboard via boost to confirm Chart.js re-init has 0 errors
                page.locator('.md\\:w-64 nav a[href="/dashboard"]').first.click()
                page.wait_for_url("**/dashboard", timeout=5000)
                page.wait_for_load_state("networkidle")
                _assert_layout_geometry(page, ("C-CALL", "IVR"), "Merchant Console")

                page.set_viewport_size({"width": 390, "height": 844})
                _assert_layout_geometry(page, ("C-CALL", "IVR"), "Merchant Console", mobile=True)

                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            if db_pool._pool:
                try:
                    db_pool._pool.close()
                except Exception:
                    pass
                db_pool._pool = None
            redis_ext._client = None

            with psycopg.connect(TEST_DATABASE_URL, autocommit=False) as clean_conn:
                if created["provider_id"]:
                    clean_conn.execute("DELETE FROM sip_providers WHERE id = %s", (created["provider_id"],))
                if created["merchant_user_id"]:
                    clean_conn.execute("DELETE FROM users WHERE id = %s", (created["merchant_user_id"],))
                if created["tenant_id"]:
                    clean_conn.execute("DELETE FROM wallets WHERE tenant_id = %s", (created["tenant_id"],))
                    clean_conn.execute("DELETE FROM tenants WHERE id = %s", (created["tenant_id"],))
                if created["admin_user_id"]:
                    clean_conn.execute("DELETE FROM users WHERE id = %s", (created["admin_user_id"],))
                clean_conn.commit()

        assert not console_errors, f"Console errors observed: {console_errors}"
        assert not page_errors, f"Page errors observed: {page_errors}"
        assert not server_errors, f"HTTP 500 responses observed: {server_errors}"
