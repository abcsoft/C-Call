# C-Call — Final GO / NO-GO Release Decision

## Verification Summary
- **Committed-Branch Clean-Environment Reproducibility (`git archive` against `ccall_test` + `BILLING_PROCESS_TEST_DB`, `0 failed, 0 skipped`):**
  - Clean `git archive a2367f7` verification (`task-1603`): **1,001 passed, 0 failed, 0 skipped** in 610.58s (re-verified on final committed `HEAD` after adding UDP retransmit resilience in [sip_provider.py](file:///c:/xampp_lite_8_3/www/ccall/app/models/sip_provider.py)).
  - Prior full-workspace run including preserved local WIP tests (`task-1519`, prior to final `sip_provider.py` UDP retry edit): **1,081 passed, 0 failed, 0 skipped** in 839.15s; resolved the remaining conditional skip in `tests/integration/test_admin_tts_management.py` (`TestPlanArchive.test_archive_blocks_if_tenants_assigned`, committed in `45054a7`).
  - Expanded [test_v2_admin_lifecycle_and_sip_diag.py](file:///c:/xampp_lite_8_3/www/ccall/tests/unit/test_v2_admin_lifecycle_and_sip_diag.py) (`7/7` passed post-`sip_provider.py` edit): verifies SIP `404` fail + separate `OPTIONS: 404`/`REGISTER: 404` badges, non-disruptive RFC 3261 Digest `401 -> 200` binding-query `REGISTER` (`Contact`/`Expires` omitted), `403` wrong auth, active-call stale-reconciliation protection (`call_jobs.lease_expires_at > now()`, `answered_at > now() - 30m`, live registry `ccall:active:*`), multi-signal 9-column cleanup audit classification (`build_cleanup_audit_preview()`), bulk archive confirmation guard, and cache invalidation.
- **Owner-Facing Live Acceptance Test on Port `5000` (`http://127.0.0.1:5000`, `ccall` DB, `28/28` migrations applied, `task-1715`):**
  - **11/11 Admin pages** HTTP `200` + **12/12 Merchant pages** HTTP `200` in headless Chromium (`0` console errors, `0` page errors, `0` HTTP `500`s, `0` failed requests, `0` leaked secrets).
  - **Non-Disruptive Live SIP Diagnostic (`203.188.247.3:5060`):** `[OPTIONS: 404]` and `[REGISTER: 200]` rendered as separate badges with header `PASS — REGISTER 200 OK (OPTIONS 404)` and non-disruptive caveat (`existing active calls and SIP bindings left untouched`), zero outbound calls placed, UDP timeout retransmit resilience (`_send_and_recv_sip`), and zero secrets in UI or server logs (`task-1494`).
  - **Complete Functional Workflows (Zero Paid Calls / Zero Paid TTS Credits):**
    - Plan lifecycle Create → Duplicate → Archive → Activate (`PASS`).
    - Merchant plan selection (`qa-accept-edgeonly`) + `POST /api/settings/tts-preview` (`tts_provider="gemini"`) blocked by plan entitlement with HTTP `403` `Provider 'gemini' is not available on your plan.` (`PASS`).
    - Subscription cancellation at period end persisted in DB (`cancel_at_period_end = true`, `PASS`).
    - Admin TTS Usage UI & `/admin/tts-usage/export.csv` matched seeded usage (`2500` chars, `$0.1250`, `PASS`).
  - **Preservation of Existing Merchant Accounts:** Audited all **101** existing merchant accounts in [SYNTHETIC-ACCOUNT-AUDIT-101.md](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/SYNTHETIC-ACCOUNT-AUDIT-101.md) (`4` safe-delete eligible, `89` synthetic domain w/ balance/orders archive-eligible only, `8` protected real-domain/activity accounts, `101` archive-eligible); **`101` preserved intact** (`initial_existing_tenants = 101`, `final_existing_tenants = 101`).
  - Raw JSON evidence & screenshots saved in [live_5000_acceptance_results.json](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/live_5000_acceptance_results.json) and [screenshots](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/screenshots).
- **Backup Security & Isolated PostgreSQL Restore Verification:**
  - `.gitignore` excludes `.env`, `.env.*`, `*.sql`, `*.sql.gz`, `*.dump`, `*.backup`, `*.bak`, and `backups/` (`git ls-files` confirms zero DB/env dumps tracked).
  - Pre-acceptance DB (`473,156` bytes) & `.env` backups stored securely outside Git; isolated restore verified against temporary database `ccall_restore_verify` (`psql.exe -v ON_ERROR_STOP=1`: exit `0`, `tables=44`, `migrations=28`, `tenants=101`, `sip_providers=3`, `plans=3`, cleanly dropped after verification).
- **SIP/RTP & NAT Networking + IP-PBX Acceptance Harness:**
  - Fixed `self.local_ip` leaks in `_refer()` (`Via`, `Contact`) and `_invite_bridge_leg()` `ack()` (`Via`) in [ivr_engine.py](file:///c:/xampp_lite_8_3/www/ccall/call_engine/ivr_engine.py), with SIP latency (`register_latency_ms`, `invite_setup_latency_ms`), RFC 3550 RTP jitter (`rtp_jitter_ms`), and RTP packet-loss tracking.
  - [ippbx_acceptance.py](file:///c:/xampp_lite_8_3/www/ccall/scripts/ippbx_acceptance.py) supports `--mode mock-acceptance` and strict `--confirm-authorized-test --target <approved_number>` gating on `--mode full-call`.
  - **SIP Networking Suite ([test_v2_sip_networking.py](file:///c:/xampp_lite_8_3/www/ccall/tests/unit/test_v2_sip_networking.py)):** **12 passed, 0 failed**.
- **Deployment Package Updated:**
  - [docker-compose.staging.coolify.yml](file:///c:/xampp_lite_8_3/www/ccall/docker-compose.staging.coolify.yml), [.env.production.example](file:///c:/xampp_lite_8_3/www/ccall/.env.production.example), [provision_production_server.sh](file:///c:/xampp_lite_8_3/www/ccall/scripts/provision_production_server.sh), and [PRODUCTION_DEPLOYMENT_GUIDE.md](file:///c:/xampp_lite_8_3/www/ccall/docs/PRODUCTION_DEPLOYMENT_GUIDE.md).
- **Secret Scan:** `scripts/secret_scan.py --staged` → **PASS — no likely secrets found**.

## Test Counts Breakdown
- **Executed & Passed (Committed Clean Branch):** **1,001 passed, 0 failed, 0 skipped** (`git archive` isolated checkout against `ccall_test` + `BILLING_PROCESS_TEST_DB`, `task-1603`; full post-final-edit clean `HEAD` verification executed after final commit) + Live `:5000` Chromium Acceptance (`task-1715`: `11/11` Admin + `12/12` Merchant HTTP `200`, `4/4` end-to-end functional workflows, `0` errors/secret leaks).
- **Failed:** 0.
- **Skipped:** 0.
- **Blocked (External Dependencies):**
  1. **AWS Coolify Server Inspection & Isolated Staging Deployment (`BLOCKED`):** No Coolify URL/API credentials, `~/.ssh/config`, or `~/.aws` configuration exists on this host; `ssh -o BatchMode=yes` to AWS IPs in `~/.ssh/known_hosts` (`18.143.131.230`, `184.197.175.205`, `52.206.238.79`) failed authentication (`exit 255`).
  2. **Docker Build & Container Runtime Verification (`BLOCKED`):** `docker` CLI/daemon is absent on this Windows host and no remote Linux/Coolify SSH access is configured.
  3. **Live Staging Admin/Merchant Panel & Server Public IP NAT Wiring (`BLOCKED`):** Awaits Coolify host SSH/API access and staging domain (`STAGING_DOMAIN`).
  4. **Authorized Live IP-PBX Acceptance Call (`BLOCKED`):** Awaits approved test phone number, explicit authorization, live SIP trunk credentials, and server public IP.
  5. **Automated GitHub CLI Draft PR (`BLOCKED`):** `gh` CLI and `GITHUB_TOKEN` are not present on this host; create via `https://github.com/abcsoft/C-Call/pull/new/feat/c-call-v2-production-foundation`.

## Release Decision
- **Codebase, Local Application (`:5000`), Backup/Restore & Clean-Branch Reproducibility:** **GO** (`0 failed, 0 skipped` clean-branch `pytest`; `11/11` Admin + `12/12` Merchant live `:5000` Playwright acceptance PASS; isolated PG16 restore verified; `101` merchant accounts preserved).
- **AWS Coolify Staging Deployment & Public Production Cutover:** **NO-GO** until AWS/Coolify credentials & SSH key are provided to execute staging Docker build/migrations/workers, live server NAT wiring, and an authorized IP-PBX acceptance call.
