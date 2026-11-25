# C-Call — Final GO / NO-GO Release Decision

## Verification Summary
- **Complete Repository `pytest` Suite (`pytest -q --tb=short` with `BILLING_PROCESS_TEST_DB`, `task-1397` post-final-edit):** **1,079 passed, 0 failed, 1 skipped** in 591.62s (0:09:51).
  - Includes new unit/integration regression suite [test\_v2\_admin\_lifecycle\_and\_sip\_diag.py](file:///c:/xampp_lite_8_3/www/ccall/tests/unit/test_v2_admin_lifecycle_and_sip_diag.py) (`6/6` passed): SIP `404` non-success guard, RFC 2617 `401 -> 200` Digest REGISTER without `INVITE`, `403` wrong-auth fail, domain/port/`max_channels`/duplicate checks, `reconcile_stale_calls()`, financial-history delete block, archive/restore, password-confirmed hard delete + `audit_log`, synthetic bulk cleanup, SQL filter ordering, `invalidate_tenant_caches(tenant_id)` (`tenant:settings:{id}`), plan duplicate, TTS cost CSV export, and HTMX HTML diagnostic partial.
  - Note on `1 skipped`: conditional plan-presence skip in user-modified `tests/integration/test_admin_tts_management.py`.
- **Exhaustive Live Browser QA (Playwright against isolated `ccall_test` on `:5001` with CSRF enabled, `task-1385`):**
  - Walked all **11/11** Admin Sidebar routes + **12/12** Merchant Sidebar routes (all HTTP `200`, `has_content: true`), **0 uncaught `page_errors`**, **0 HTTP `500`s**, and **0 inline `on*=` CSP handlers** across `app/templates/`. (`failed_requests` recorded 1 transient localhost Windows `net::ERR_NO_BUFFER_SPACE` on `tailwind.css` under rapid loop navigation.)
  - Verified Admin Customer lifecycle (Create, Search/Filter, Edit, Suspend/Unsuspend, Financial Delete Guard + Archive modal, Safe Delete with password confirmation, Synthetic Test Bulk Cleanup), Admin SIP Diagnostics (`404 Not Found` HTML `FAIL` card vs Digest `200 OK` HTML `PASS` card + `max_channels` edit), Plan Duplicate, TTS Usage Cost (`$0.0480` + CSV), Merchant Self-Registration, SMS Fallback CSP-clean toggle, REST API Key + `POST /api/v1/orders` (`200`, `CALL_QUEUED`). Screenshots saved under [screenshots](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/screenshots).
- **SIP/RTP & NAT Networking + IP-PBX Acceptance Harness:**
  - Fixed `self.local_ip` leaks in `_refer()` (`Via`, `Contact`) and `_invite_bridge_leg()` `ack()` (`Via`) in [ivr_engine.py](file:///c:/xampp_lite_8_3/www/ccall/call_engine/ivr_engine.py), and added SIP latency (`register_latency_ms`, `invite_setup_latency_ms`), RFC 3550 RTP jitter (`rtp_jitter_ms`), and RTP packet-loss tracking (`rtp_packets_received`, `rtp_packets_lost`).
  - Upgraded [ippbx_acceptance.py](file:///c:/xampp_lite_8_3/www/ccall/scripts/ippbx_acceptance.py) with NAT env passthrough, latency/jitter/loss reporting, `--mode mock-acceptance` (rehearsed cleanly against local UDP mock IP-PBX: all 7 checks `true`), and strict `--confirm-authorized-test --target <approved_number>` gating on `--mode full-call`.
  - UDP-only transport explicitly enforced (`SUPPORTED_SIP_TRANSPORTS = frozenset({"udp"})`).
  - **SIP Networking & Mock Acceptance Suite ([test_v2_sip_networking.py](file:///c:/xampp_lite_8_3/www/ccall/tests/unit/test_v2_sip_networking.py)):** **12 passed, 0 failed**.
- **Deployment Package Updated:**
  - Added `build: .` to `migrate`/`web`/`scheduler`/`call_worker` in [docker-compose.staging.coolify.yml](file:///c:/xampp_lite_8_3/www/ccall/docker-compose.staging.coolify.yml), NAT env documentation in [.env.production.example](file:///c:/xampp_lite_8_3/www/ccall/.env.production.example), UDP RTP UFW rule in [provision_production_server.sh](file:///c:/xampp_lite_8_3/www/ccall/scripts/provision_production_server.sh), and Standalone Ubuntu / Windows Local / SG-US-BD Region Matrix / Resource Tiers & Troubleshooting in [PRODUCTION_DEPLOYMENT_GUIDE.md](file:///c:/xampp_lite_8_3/www/ccall/docs/PRODUCTION_DEPLOYMENT_GUIDE.md).
- **Secret Scan:** `scripts/secret_scan.py --staged` → **PASS — no likely secrets found**.

## Test Counts Breakdown
- **Executed & Passed:** **1,079** (`pytest` full repository suite with `BILLING_PROCESS_TEST_DB`, `task-1397`) + Playwright walkthrough across 11 Admin + 12 Merchant routes & E2E workflows (`0` page errors, `0` HTTP 500s).
- **Failed:** 0.
- **Skipped:** 1 (conditional plan-presence skip in `tests/integration/test_admin_tts_management.py`).
- **Blocked (External Dependencies):**
  1. **AWS Coolify Server Inspection & Isolated Staging Deployment (`BLOCKED`):** No Coolify URL/API credentials, `~/.ssh/config`, or `~/.aws` configuration exists on this host; `ssh -o BatchMode=yes` to AWS IPs in `~/.ssh/known_hosts` (`18.143.131.230`, `184.197.175.205`, `52.206.238.79`) failed authentication (`exit 255`).
  2. **Docker Build, Container Runtime, & Backup Restore Execution (`BLOCKED`):** `docker` CLI/daemon is absent on this Windows host and no remote Linux/Coolify SSH access is configured.
  3. **Live Staging Admin/Merchant Panel & Server Public IP NAT Wiring (`BLOCKED`):** Awaits Coolify host SSH/API access and staging domain (`STAGING_DOMAIN`).
  4. **Authorized Live IP-PBX Acceptance Call (`BLOCKED`):** Awaits approved test phone number, explicit authorization, live SIP trunk credentials, and server public IP.
  5. **Automated GitHub CLI Draft PR (`BLOCKED`):** `gh` CLI and `GITHUB_TOKEN` are not present on this host; create via `https://github.com/abcsoft/C-Call/pull/new/feat/c-call-v2-production-foundation`.

## Release Decision
- **Codebase & Local PostgreSQL Verification:** **GO** (1,079 passed, 0 failed, 1 skipped; 0 Playwright `page_errors`, 0 HTTP 500s).
- **AWS Coolify Staging Deployment & Public Production Cutover:** **NO-GO** until AWS/Coolify credentials & SSH key are provided to execute staging Docker build/migrations/workers/backup-restore verification, live server NAT wiring, and an authorized IP-PBX acceptance call.
