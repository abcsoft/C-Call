from __future__ import annotations

import os
from unittest.mock import patch


def test_unauthenticated_root_renders_landing_page(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "C-CALL IVR" in html
    assert "/auth/login" in html
    assert "/auth/register" in html


def test_require_login_uses_clean_relative_next_path(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert loc.endswith("/auth/login?next=%2Fdashboard") or "/auth/login?next=/dashboard" in loc
    assert "http://" not in loc


def test_proxy_fix_honors_x_forwarded_proto_https():
    with patch.dict(os.environ, {"TRUSTED_PROXIES": "127.0.0.1,10.0.0.0/8"}):
        from app import create_app

        app = create_app("testing")
        app.config["SERVER_NAME"] = None
        captured = {}

        @app.route("/_test_scheme")
        def _test_scheme():
            from flask import request

            captured["scheme"] = request.scheme
            captured["url"] = request.url
            return "ok"

        test_client = app.test_client()
        r2 = test_client.get(
            "/_test_scheme",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "example.sslip.io",
            },
        )
        assert r2.status_code == 200
        assert captured["scheme"] == "https"
        assert captured["url"].startswith("https://example.sslip.io/_test_scheme")
