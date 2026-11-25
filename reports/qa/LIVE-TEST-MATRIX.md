# C-Call — Live Test Matrix

| Area | Test Case | Command / Method | Result |
|---|---|---|---|
| Auth | Invalid login blocked (`qa_admin@ccall.example.com`) | Playwright `POST /auth/login` | **PASS** |
| Admin Panel (`:5000` Live Acceptance) | Owner-reported pages (`/admin/customers`, `/admin/providers`, `/admin/plans`, `/admin/platform-tts`, `/admin/tts-usage` + CSV) + 101-account multi-signal audit preview + live `203.188.247.3` non-calling SIP diag | Playwright Chromium on `http://127.0.0.1:5000` (`task-1512`, `0` console/page/HTTP500/secret errors) | **PASS** (`5/5` HTTP 200) |
| Admin Panel (`:5001` Isolated Walk) | 11 sidebar routes (`/admin/dashboard` .. `/admin/tts-usage`) + customer Edit/Archive/Delete/Synthetic Cleanup, SIP 404/Digest diag, plan duplicate, TTS cost/CSV | Playwright authenticated walk (`task-1385`, `0` page errors, `0` 500s) | **PASS** (11/11 HTTP 200) |
| Merchant Panel | 12 sidebar routes (`/dashboard`, `/live-calls`, `/orders`, `/script`, `/retry`, `/flows`, `/wallet`, `/settings/subscription`, `/cdr`, `/integration`, `/settings`, `/settings/team`) + API key + order queue | Playwright authenticated walk (`task-1385`, `0` page errors, `0` 500s) | **PASS** (12/12 HTTP 200) |
| Mobile Responsive | `/dashboard`, `/orders`, `/flows`, `/wallet`, `/settings/subscription` at `390x844` | Playwright viewport check | **PASS** (`horizontal_overflow: false`) |
| Security Suite | `TestV2SecurityFoundation` (7 tests) | `pytest tests/unit/test_v2_security_and_reliability.py` | **PASS** |
| SIP / IVR Mocked | `TestV2CallingReliabilityAndIVR` + `test_flows.py` + `test_call_queue_integration.py` | `pytest` | **PASS** |
| Ecommerce & Webhooks | `test_order_api.py` + `test_webhook.py` + `TestV2EcommerceAndSQLite` | `pytest` | **PASS** |
| Live SIP Trunk / Real Calls | Outbound call to real PSTN/SIP carrier | Not executed (no live trunk auth) | **BLOCKED** |
| Live ElevenLabs / Gemini TTS | Live API synthesis with production keys | Not executed in automated QA | **BLOCKED** |
| Docker Compose Build | `docker compose -f docker-compose.production.yml build` | Docker CLI not installed on Windows host | **BLOCKED** |
