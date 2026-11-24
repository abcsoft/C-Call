# C-Call — Final GO / NO-GO Release Decision

## Verification Summary
- **Complete Repository `pytest` Suite (`pytest -q --tb=short` with `BILLING_PROCESS_TEST_DB`):** **1,073 passed, 0 failed, 0 skipped** in 641.19s (0:10:41).
  - Resolved the previously skipped 18 process-level billing crash/race tests in [test_billing_process.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_billing_process.py) by seeding valid `rate_snapshot` JSON in `_setup_tenant_and_call()` and executing against isolated `ccall_test` (`18 passed` in `task-903`, and all `1,073` repository tests passed together in `task-938`).
  - Executed subscription, billing resolver, TTS management, and rate-snapshot integration suites ([test_admin_tts_management.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_admin_tts_management.py), [test_subscription_e2e.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_subscription_e2e.py), [test_billing_resolver.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_billing_resolver.py), [test_rate_snapshot_immutability.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_rate_snapshot_immutability.py)): **283 passed, 0 skipped**.
- **Live Browser QA (Local Playwright against `ccall` `:5000` & isolated `ccall_test` `:5001`):**
  - Full page walk: 11/11 Admin routes + 12/12 Merchant routes HTTP 200, mobile layout (`390x844`) verified, cross-tenant isolation verified.
  - Full CRUD workflow ([browser_crud_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/browser_crud_results.json)): Admin Create Tenant modal, Credit Wallet modal, Add SIP Provider; Merchant IVR Flow/Node/Transition + `/validate` (`valid: true`); API Key generation + `POST /api/v1/orders` (`200`, `CALL_QUEUED`) + `/orders` UI visibility; Retry Rules save (`saved: true`); `0` browser console errors, `0` HTTP 500s.
- **SIP/RTP & NAT Networking + IP-PBX Acceptance Harness:**
  - Fixed `self.local_ip` leaks in `_refer()` (`Via`, `Contact`) and `_invite_bridge_leg()` `ACK` (`Via`) in [ivr_engine.py](file:///c:/xampp_lite_8_3/www/ccall/call_engine/ivr_engine.py) to use `self.advertised_ip`.
  - Documented `SIP_TRANSPORT`, `SIP_BIND_IP`, `RTP_BIND_IP`, `SIP_ADVERTISED_IP`, `RTP_ADVERTISED_IP`, `RTP_PORT_MIN`, `RTP_PORT_MAX` in [.env.production.example](file:///c:/xampp_lite_8_3/www/ccall/.env.production.example) and added `build: .` to `migrate`/`web`/`scheduler`/`call_worker` in [docker-compose.staging.coolify.yml](file:///c:/xampp_lite_8_3/www/ccall/docker-compose.staging.coolify.yml).
  - Upgraded [ippbx_acceptance.py](file:///c:/xampp_lite_8_3/www/ccall/scripts/ippbx_acceptance.py) with NAT env passthrough, 7 explicit checks (`registration`, `outbound_call`, `audio_prompt`, `dtmf_capture`, `recording_saved`, `hangup`, `transfer`), `--mode mock-acceptance` (verified against local UDP mock IP-PBX: `MOCK_PBX_RTP_PACKETS_RECEIVED=3`, all 7 checks `true`), and strict `--confirm-authorized-test --target <approved_number>` gating on `--mode full-call`.
  - **SIP Networking Unit & Mock Acceptance Suite ([test_v2_sip_networking.py](file:///c:/xampp_lite_8_3/www/ccall/tests/unit/test_v2_sip_networking.py)):** **11 passed, 0 failed**.
- **Secret Scan:** `scripts/secret_scan.py --staged` → **PASS — no likely secrets found**.

## Test Counts Breakdown
- **Executed & Passed:** **1,073** (`pytest` full suite with `BILLING_PROCESS_TEST_DB`, 0 skipped) + local Playwright page walk (23 routes) + local Playwright CRUD suite (`admin_crud`, `merchant_ivr_crud`, `merchant_api_order_crud`, `merchant_retry_crud`, `cross_tenant_crud`, `0` console errors, `0` HTTP 500s) + local IP-PBX mock acceptance rehearsal (`7/7` checks `true`).
- **Failed:** 0.
- **Skipped:** 0.
- **Blocked (External Dependencies):**
  1. **AWS Coolify Server Inspection & Live Staging Deployment (`BLOCKED`):** No Coolify API credentials/URL configured locally, no `~/.ssh/config` or AWS key accepted by `known_hosts` AWS IPs (`ssh -o BatchMode=yes` returned exit `255` on `18.143.131.230`, `184.197.175.205`, `52.206.238.79`), and Docker CLI/daemon is absent on this Windows host.
  2. **Docker Build & Runtime Stack Verification (`BLOCKED`):** Blocked by absence of Docker on local host and absence of remote Linux/Coolify SSH access.
  3. **Live Staging Admin/Merchant Panel & Server-IP NAT Wiring (`BLOCKED`):** Blocked by absence of remote Coolify deployment access & server public IP / staging hostname (`STAGING_DOMAIN`).
  4. **Authorized Live IP-PBX Acceptance Call (`BLOCKED`):** No approved test phone number / authorization or live Coolify public IP supplied ([ippbx_acceptance.py](file:///c:/xampp_lite_8_3/www/ccall/scripts/ippbx_acceptance.py) `--mode full-call` ready once authorized).

## Release Decision
- **Codebase & Local PostgreSQL Verification:** **GO** (all 1,073 tests pass with 0 skips and 0 failures).
- **AWS Coolify Staging Deployment & Public Production Cutover:** **NO-GO** until AWS/Coolify credentials & SSH key are provided to execute staging container build/startup, server NAT IP wiring, staging browser smoke QA, and an authorized live IP-PBX acceptance call.
