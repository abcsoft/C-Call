# C-Call — Live Test Matrix

| Area | Test Case | Command / Method | Result |
|---|---|---|---|
| Auth | Invalid login blocked (`qa_admin@ccall.example.com`) | Playwright `POST /auth/login` | **PASS** |
| Admin Panel (`:5000` Visual & Layout QA) | All `11/11` primary routes + `4/4` subpages (`customer_detail`, `plan_create`, `plan_edit`, `tts_reservations`), `/admin/providers` sidebar + `<h1>`, SIP Gateway CRUD + `OPTIONS: 404` / `REGISTER: 200` diag with preserved shell | Playwright Chromium `149` & Firefox `151` on `http://127.0.0.1:5000` (`0` console/page/HTTP500/secret errors, `101 -> 101` tenants intact) | **PASS** (`15/15` HTTP `200`, `overflow_px=0`) |
| Merchant Panel (`:5000` Visual & Layout QA) | All `12/12` primary routes + `2/2` subpages (`order_detail`, `flow_edit`), Chart.js + Alpine `hx-boost` navigation, restricted Plan Select, `tts_provider="gemini"` blocked `403`, Subscription Cancel | Playwright Chromium `149` & Firefox `151` on `http://127.0.0.1:5000` (`0` console/page/HTTP500/secret errors) | **PASS** (`14/14` HTTP `200`, `overflow_px=0`) |
| Navigation & Responsive Matrix | Direct, `hx-boost` sidebar click, reload, back, forward across `1920x1080`, `1440x900`, `1366x768`, `1024x768`, `768x1024`, `390x844` + mobile drawer in Chromium & Firefox | Playwright dual-browser runner (`exhaustive_ui_qa_results.json`) | **PASS** |
| Permanent UI Layout Regression Suite | Template inheritance, `13` Admin + `12` Merchant direct & `HX-Boosted` routes, role guard, shared JS asset, Playwright Chromium geometry + `hx-boost` + diag swap | `pytest tests/integration/test_v2_ui_layout_regression.py -q` (`task-2287`) | **PASS** (`29 passed, 0 failed, 0 skipped`) |
| Isolated DB Backup Restore | `psql.exe -v ON_ERROR_STOP=1` restore of pre-acceptance dump (`473,156` bytes) into temporary `ccall_restore_verify` DB + `.gitignore` secret exclusion check | PostgreSQL 16 CLI (`tables=44`, `migrations=28`, `tenants=101`, `sip_providers=3`, `plans=3`, cleanly dropped) | **PASS** |
| Security Suite | `TestV2SecurityFoundation` (7 tests) | `pytest tests/unit/test_v2_security_and_reliability.py` | **PASS** |
| SIP / IVR Mocked | `TestV2CallingReliabilityAndIVR` + `test_v2_sip_networking.py` + `test_flows.py` + `test_call_queue_integration.py` | `pytest` | **PASS** |
| Ecommerce & Webhooks | `test_order_api.py` + `test_webhook.py` + `TestV2EcommerceAndSQLite` | `pytest` | **PASS** |
| Live SIP Trunk / Real Calls | Outbound call to real PSTN/SIP carrier | Not executed (no live trunk auth) | **BLOCKED** |
| Live ElevenLabs / Gemini TTS | Live API synthesis with production keys | Not executed in automated QA | **BLOCKED** |
| Docker Compose Build | `docker compose -f docker-compose.production.yml build` | Docker CLI not installed on Windows host | **BLOCKED** |
