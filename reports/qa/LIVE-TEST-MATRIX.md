# C-Call — Live Test Matrix

| Area | Test Case | Command / Method | Result |
|---|---|---|---|
| Auth | Invalid login blocked (`qa_admin@ccall.example.com`) | Playwright `POST /auth/login` | **PASS** |
| Admin Panel (`:5000` Live Acceptance) | All 11 routes (`/admin/dashboard` .. `/admin/tts-usage`) + live `203.188.247.3:5060` non-disruptive SIP diag (`OPTIONS: 404` & `REGISTER: 200`) + Plan Create/Duplicate/Archive/Activate + TTS Usage `$0.1250` & CSV export | Playwright Chromium on `http://127.0.0.1:5000` (`task-1715`, `0` console/page/HTTP500/secret errors, `101 -> 101` tenants intact) | **PASS** (`11/11` HTTP `200`) |
| Merchant Panel (`:5000` Live Acceptance) | All 12 routes (`/dashboard` .. `/settings/team`) + restricted Plan Select + `tts_provider="gemini"` blocked `403` + Subscription Cancel at Period End | Playwright Chromium on `http://127.0.0.1:5000` (`task-1715`, `0` console/page/HTTP500/secret errors) | **PASS** (`12/12` HTTP `200`) |
| Isolated DB Backup Restore | `psql.exe -v ON_ERROR_STOP=1` restore of pre-acceptance dump (`473,156` bytes) into temporary `ccall_restore_verify` DB + `.gitignore` secret exclusion check | PostgreSQL 16 CLI (`tables=44`, `migrations=28`, `tenants=101`, `sip_providers=3`, `plans=3`, cleanly dropped) | **PASS** |
| Mobile Responsive | `/dashboard`, `/orders`, `/flows`, `/wallet`, `/settings/subscription` at `390x844` | Playwright viewport check | **PASS** (`horizontal_overflow: false`) |
| Security Suite | `TestV2SecurityFoundation` (7 tests) | `pytest tests/unit/test_v2_security_and_reliability.py` | **PASS** |
| SIP / IVR Mocked | `TestV2CallingReliabilityAndIVR` + `test_v2_sip_networking.py` + `test_flows.py` + `test_call_queue_integration.py` | `pytest` | **PASS** |
| Ecommerce & Webhooks | `test_order_api.py` + `test_webhook.py` + `TestV2EcommerceAndSQLite` | `pytest` | **PASS** |
| Live SIP Trunk / Real Calls | Outbound call to real PSTN/SIP carrier | Not executed (no live trunk auth) | **BLOCKED** |
| Live ElevenLabs / Gemini TTS | Live API synthesis with production keys | Not executed in automated QA | **BLOCKED** |
| Docker Compose Build | `docker compose -f docker-compose.production.yml build` | Docker CLI not installed on Windows host | **BLOCKED** |
