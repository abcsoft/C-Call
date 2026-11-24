# C-Call — Final GO / NO-GO Release Decision

## Verification Summary
- **Complete Repository `pytest` Suite (`pytest -q --tb=short` with `BILLING_PROCESS_TEST_DB`, `task-1089` post-final-edit):** **1,074 passed, 0 failed, 0 skipped** in 708.53s (0:11:48).
  - Resolved all 18 previously skipped process-level billing crash/race tests in [test_billing_process.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_billing_process.py) by seeding valid `rate_snapshot` JSON in `_setup_tenant_and_call()` against isolated `ccall_test` (`18/18` passed, `leftover_proctest: 0`).
  - Executed subscription, billing resolver, TTS management, and rate-snapshot integration suites (`task-788`): **283 passed, 0 skipped**.
- **Live Browser QA (Playwright against isolated `ccall_test` on `:5001` with CSRF enabled):**
  - Full page walk ([browser_qa_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/browser_qa_results.json), `task-1068`): 12/12 Admin routes (`/admin/dashboard` .. `/admin/settings` incl. `/admin/tts-reservations`) + 12/12 Merchant routes (`/dashboard` .. `/settings/subscription`) HTTP `200`, 5/5 Mobile routes (`390x844`) zero horizontal overflow, **0 browser console errors** (fixed 3 `htmx:syntax:error`s on `/script` by replacing invalid `click debounce:500ms` with `click delay:500ms` and placing `<script nonce>` inside `{% block customer_content %}`), **0 failed requests**, **0 HTTP 500s**.
  - End-to-end workflows verified: Admin login/invalid-login block + wallet credit (`+250`), Merchant IVR create/nodes/DTMF-`1` transition/validate (`valid: true`)/delete (`Flow deleted`), API key generation + `POST /api/v1/orders` (`200`, `CALL_QUEUED`) + `/orders` UI visibility + cross-tenant isolation (`merchant_b` cannot see `merchant_a` order), Retry Rules save.
- **SIP/RTP & NAT Networking + IP-PBX Acceptance Harness:**
  - Fixed `self.local_ip` leaks in `_refer()` (`Via`, `Contact`) and `_invite_bridge_leg()` `ack()` (`Via`) in [ivr_engine.py](file:///c:/xampp_lite_8_3/www/ccall/call_engine/ivr_engine.py), and added SIP latency (`register_latency_ms`, `invite_setup_latency_ms`), RFC 3550 RTP jitter (`rtp_jitter_ms`), and RTP packet-loss tracking (`rtp_packets_received`, `rtp_packets_lost`).
  - Upgraded [ippbx_acceptance.py](file:///c:/xampp_lite_8_3/www/ccall/scripts/ippbx_acceptance.py) with NAT env passthrough, latency/jitter/loss reporting, `--mode mock-acceptance` (rehearsed cleanly against local UDP mock IP-PBX: all 7 checks `true`), and strict `--confirm-authorized-test --target <approved_number>` gating on `--mode full-call`.
  - UDP-only transport explicitly enforced (`SUPPORTED_SIP_TRANSPORTS = frozenset({"udp"})`).
  - **SIP Networking & Mock Acceptance Suite ([test_v2_sip_networking.py](file:///c:/xampp_lite_8_3/www/ccall/tests/unit/test_v2_sip_networking.py)):** **12 passed, 0 failed**.
- **Deployment Package Updated:**
  - Added `build: .` to `migrate`/`web`/`scheduler`/`call_worker` in [docker-compose.staging.coolify.yml](file:///c:/xampp_lite_8_3/www/ccall/docker-compose.staging.coolify.yml), NAT env documentation in [.env.production.example](file:///c:/xampp_lite_8_3/www/ccall/.env.production.example), UDP RTP UFW rule in [provision_production_server.sh](file:///c:/xampp_lite_8_3/www/ccall/scripts/provision_production_server.sh), and Standalone Ubuntu / Windows Local / SG-US-BD Region Matrix / Resource Tiers & Troubleshooting in [PRODUCTION_DEPLOYMENT_GUIDE.md](file:///c:/xampp_lite_8_3/www/ccall/docs/PRODUCTION_DEPLOYMENT_GUIDE.md).
- **Secret Scan:** `scripts/secret_scan.py --staged` → **PASS — no likely secrets found**.

## Test Counts Breakdown
- **Executed & Passed:** **1,074** (`pytest` full repository suite with `BILLING_PROCESS_TEST_DB`, 0 skipped) + Playwright full walk & workflows (24 desktop routes + 5 mobile routes + CRUD/isolation, `0` console errors, `0` failed requests, `0` HTTP 500s).
- **Failed:** 0.
- **Skipped:** 0.
- **Blocked (External Dependencies):**
  1. **AWS Coolify Server Inspection & Isolated Staging Deployment (`BLOCKED`):** No Coolify URL/API credentials, `~/.ssh/config`, or `~/.aws` configuration exists on this host; `ssh -o BatchMode=yes` to AWS IPs in `~/.ssh/known_hosts` (`18.143.131.230`, `184.197.175.205`, `52.206.238.79`) failed authentication (`exit 255`).
  2. **Docker Build, Container Runtime, & Backup Restore Execution (`BLOCKED`):** `docker` CLI/daemon is absent on this Windows host and no remote Linux/Coolify SSH access is configured.
  3. **Live Staging Admin/Merchant Panel & Server Public IP NAT Wiring (`BLOCKED`):** Awaits Coolify host SSH/API access and staging domain (`STAGING_DOMAIN`).
  4. **Authorized Live IP-PBX Acceptance Call (`BLOCKED`):** Awaits approved test phone number, explicit authorization, live SIP trunk credentials, and server public IP.
  5. **Automated GitHub CLI Draft PR (`BLOCKED`):** `gh` CLI and `GITHUB_TOKEN` are not present on this host; create via `https://github.com/abcsoft/C-Call/pull/new/feat/c-call-v2-production-foundation`.

## Release Decision
- **Codebase & Local PostgreSQL Verification:** **GO** (1,074 passed, 0 failed, 0 skipped; 0 Playwright console/HTTP errors).
- **AWS Coolify Staging Deployment & Public Production Cutover:** **NO-GO** until AWS/Coolify credentials & SSH key are provided to execute staging Docker build/migrations/workers/backup-restore verification, live server NAT wiring, and an authorized IP-PBX acceptance call.
