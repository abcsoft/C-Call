# C-Call — Final GO / NO-GO Release Decision

## Verification Summary
- **Complete Repository `pytest` Suite (`pytest -q --tb=short` with `BILLING_PROCESS_TEST_DB`, `task-1519` post-final-edit):** **1,081 passed, 0 failed, 0 skipped** in 839.15s (0:13:59).
  - Resolved the remaining conditional test skip in `tests/integration/test_admin_tts_management.py` (`TestPlanArchive.test_archive_blocks_if_tenants_assigned`) using a deterministic plan+tenant fixture, achieving **0 skipped** across all `1,081` tests (`60/60` passed in `test_admin_tts_management.py`).
  - Expanded [test_v2_admin_lifecycle_and_sip_diag.py](file:///c:/xampp_lite_8_3/www/ccall/tests/unit/test_v2_admin_lifecycle_and_sip_diag.py) (`7/7` passed): verifies SIP `404` fail, Digest `401 -> 200` non-calling REGISTER, `403` wrong auth, active-call stale-reconciliation protection (`call_jobs.lease_expires_at > now()`, `answered_at > now() - 30m`, live registry `ccall:active:*`), multi-signal 9-column cleanup audit classification (`build_cleanup_audit_preview()`), bulk archive confirmation guard, and cache invalidation.
- **Owner-Facing Live Acceptance Test on Port `5000` (`http://127.0.0.1:5000`, `ccall` DB, `28/28` migrations applied, `task-1512`):**
  - Pre-acceptance DB & `.env` backup captured in scratch (`pg_dump`, `473,156` bytes).
  - Audited all **101** existing merchant accounts in [SYNTHETIC-ACCOUNT-AUDIT-101.md](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/SYNTHETIC-ACCOUNT-AUDIT-101.md) (`4` safe-delete eligible, `89` synthetic domain w/ balance/orders archive-eligible only, `8` protected real-domain/activity accounts, `101` archive-eligible; **0** accounts modified without owner approval).
  - Verified all 5 owner-reported pages in Chromium ([live_5000_acceptance_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/live_5000_acceptance_results.json)): `/admin/customers`, `/admin/providers`, `/admin/plans`, `/admin/platform-tts`, `/admin/tts-usage` + `/admin/tts-usage/export.csv`, temp synthetic create/edit/safe-delete roundtrip (`101` preserved), non-calling diagnostic on `203.188.247.3` (`[FAIL] OPTIONS SIP/2.0 404 Not Found` separated from `[PASS] REGISTER SIP/2.0 200 OK`, `No outbound call was placed`), **0 leaked secrets**, **0 console errors**, **0 page errors**, **0 failed requests**, **0 HTTP 500s**. Screenshots saved as `live5000_*.png` under [screenshots](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/screenshots).
- **Isolated Full Browser QA on Port `5001` (`task-1385`):** 11/11 Admin routes + 12/12 Merchant routes HTTP `200`, 0 `page_errors`, 0 HTTP `500`s.
- **SIP/RTP & NAT Networking + IP-PBX Acceptance Harness:**
  - Fixed `self.local_ip` leaks in `_refer()` (`Via`, `Contact`) and `_invite_bridge_leg()` `ack()` (`Via`) in [ivr_engine.py](file:///c:/xampp_lite_8_3/www/ccall/call_engine/ivr_engine.py), and added SIP latency (`register_latency_ms`, `invite_setup_latency_ms`), RFC 3550 RTP jitter (`rtp_jitter_ms`), and RTP packet-loss tracking (`rtp_packets_received`, `rtp_packets_lost`).
  - Upgraded [ippbx_acceptance.py](file:///c:/xampp_lite_8_3/www/ccall/scripts/ippbx_acceptance.py) with NAT env passthrough, latency/jitter/loss reporting, `--mode mock-acceptance` (rehearsed cleanly against local UDP mock IP-PBX: all 7 checks `true`), and strict `--confirm-authorized-test --target <approved_number>` gating on `--mode full-call`.
  - UDP-only transport explicitly enforced (`SUPPORTED_SIP_TRANSPORTS = frozenset({"udp"})`).
  - **SIP Networking & Mock Acceptance Suite ([test_v2_sip_networking.py](file:///c:/xampp_lite_8_3/www/ccall/tests/unit/test_v2_sip_networking.py)):** **12 passed, 0 failed**.
- **Deployment Package Updated:**
  - Added `build: .` to `migrate`/`web`/`scheduler`/`call_worker` in [docker-compose.staging.coolify.yml](file:///c:/xampp_lite_8_3/www/ccall/docker-compose.staging.coolify.yml), NAT env documentation in [.env.production.example](file:///c:/xampp_lite_8_3/www/ccall/.env.production.example), UDP RTP UFW rule in [provision_production_server.sh](file:///c:/xampp_lite_8_3/www/ccall/scripts/provision_production_server.sh), and Standalone Ubuntu / Windows Local / SG-US-BD Region Matrix / Resource Tiers & Troubleshooting in [PRODUCTION_DEPLOYMENT_GUIDE.md](file:///c:/xampp_lite_8_3/www/ccall/docs/PRODUCTION_DEPLOYMENT_GUIDE.md).
- **Secret Scan:** `scripts/secret_scan.py --staged` → **PASS — no likely secrets found**.

## Test Counts Breakdown
- **Executed & Passed:** **1,081** (`pytest` full repository suite with `BILLING_PROCESS_TEST_DB`, `task-1519`) + Live `:5000` acceptance (`task-1512`: `0` console/page/HTTP500/failed-request errors, `0` leaked secrets) + Isolated `:5001` walkthrough (`11` Admin + `12` Merchant routes & E2E workflows).
- **Failed:** 0.
- **Skipped:** 0.
- **Blocked (External Dependencies):**
  1. **AWS Coolify Server Inspection & Isolated Staging Deployment (`BLOCKED`):** No Coolify URL/API credentials, `~/.ssh/config`, or `~/.aws` configuration exists on this host; `ssh -o BatchMode=yes` to AWS IPs in `~/.ssh/known_hosts` (`18.143.131.230`, `184.197.175.205`, `52.206.238.79`) failed authentication (`exit 255`).
  2. **Docker Build, Container Runtime, & Backup Restore Execution (`BLOCKED`):** `docker` CLI/daemon is absent on this Windows host and no remote Linux/Coolify SSH access is configured.
  3. **Live Staging Admin/Merchant Panel & Server Public IP NAT Wiring (`BLOCKED`):** Awaits Coolify host SSH/API access and staging domain (`STAGING_DOMAIN`).
  4. **Authorized Live IP-PBX Acceptance Call (`BLOCKED`):** Awaits approved test phone number, explicit authorization, live SIP trunk credentials, and server public IP.
  5. **Automated GitHub CLI Draft PR (`BLOCKED`):** `gh` CLI and `GITHUB_TOKEN` are not present on this host; create via `https://github.com/abcsoft/C-Call/pull/new/feat/c-call-v2-production-foundation`.

## Release Decision
- **Codebase & Local Verification (`:5000` Live Acceptance & `:5001` Full Regression):** **GO** (`1,081 passed, 0 failed, 0 skipped`; `0` live `:5000` browser/console/HTTP500/secret-leak errors).
- **AWS Coolify Staging Deployment & Public Production Cutover:** **NO-GO** until AWS/Coolify credentials & SSH key are provided to execute staging Docker build/migrations/workers/backup-restore verification, live server NAT wiring, and an authorized IP-PBX acceptance call.
