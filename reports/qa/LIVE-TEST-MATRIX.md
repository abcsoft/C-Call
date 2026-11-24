# C-Call — Live Test Matrix

| Area | Test Case | Command / Method | Result |
|---|---|---|---|
| Auth | Invalid login blocked (`qa_admin@ccall.example.com`) | Playwright `POST /auth/login` | **PASS** |
| Admin Panel | 11 admin routes (`/admin/dashboard` .. `/admin/settings`) | Playwright authenticated walk | **PASS** (11/11 HTTP 200, 0 500s) |
| Merchant Panel | 12 merchant routes (`/dashboard` .. `/settings/subscription`) | Playwright authenticated walk | **PASS** (12/12 HTTP 200, 0 500s) |
| IVR Builder | Create flow `/flows/new` + `/flows/7/validate` | Playwright live browser | **PASS** |
| Cross-Tenant Isolation | Merchant B accesses Merchant A flow & `/api/debug/live-registry` | Playwright (`qa_merchant_b`) | **PASS** (redirected / 404) |
| Mobile Responsive | `/dashboard`, `/orders`, `/flows` at `390x844` | Playwright viewport check | **PASS** (`horizontal_overflow: false`) |
| Security Suite | `TestV2SecurityFoundation` (7 tests) | `pytest tests/unit/test_v2_security_and_reliability.py` | **PASS** |
| SIP / IVR Mocked | `TestV2CallingReliabilityAndIVR` + `test_flows.py` + `test_call_queue_integration.py` | `pytest` | **PASS** |
| Ecommerce & Webhooks | `test_order_api.py` + `test_webhook.py` + `TestV2EcommerceAndSQLite` | `pytest` | **PASS** |
| Live SIP Trunk / Real Calls | Outbound call to real PSTN/SIP carrier | Not executed (no live trunk auth) | **BLOCKED** |
| Live ElevenLabs / Gemini TTS | Live API synthesis with production keys | Not executed in automated QA | **BLOCKED** |
| Docker Compose Build | `docker compose -f docker-compose.production.yml build` | Docker CLI not installed on Windows host | **BLOCKED** |
