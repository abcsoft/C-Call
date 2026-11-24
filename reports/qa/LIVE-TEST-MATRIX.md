# C-Call — Live Test Matrix

| Area | Test Case | Command / Method | Result |
|---|---|---|---|
| Auth | Invalid login blocked (`qa_admin@ccall.example.com`) | Playwright `POST /auth/login` | **PASS** |
| Admin Panel | 12 admin routes (`/admin/dashboard` .. `/admin/settings` incl. `/admin/tts-reservations`) + wallet credit | Playwright authenticated walk (`0` console errors, `0` 500s) | **PASS** (12/12 HTTP 200) |
| Merchant Panel | 12 merchant routes (`/dashboard` .. `/settings/subscription`) + IVR CRUD/validate/delete + API key + order queue | Playwright authenticated walk (`0` console errors, `0` 500s) | **PASS** (12/12 HTTP 200) |
| Mobile Responsive | `/dashboard`, `/orders`, `/flows`, `/wallet`, `/settings/subscription` at `390x844` | Playwright viewport check | **PASS** (`horizontal_overflow: false`) |
| Security Suite | `TestV2SecurityFoundation` (7 tests) | `pytest tests/unit/test_v2_security_and_reliability.py` | **PASS** |
| SIP / IVR Mocked | `TestV2CallingReliabilityAndIVR` + `test_flows.py` + `test_call_queue_integration.py` | `pytest` | **PASS** |
| Ecommerce & Webhooks | `test_order_api.py` + `test_webhook.py` + `TestV2EcommerceAndSQLite` | `pytest` | **PASS** |
| Live SIP Trunk / Real Calls | Outbound call to real PSTN/SIP carrier | Not executed (no live trunk auth) | **BLOCKED** |
| Live ElevenLabs / Gemini TTS | Live API synthesis with production keys | Not executed in automated QA | **BLOCKED** |
| Docker Compose Build | `docker compose -f docker-compose.production.yml build` | Docker CLI not installed on Windows host | **BLOCKED** |
