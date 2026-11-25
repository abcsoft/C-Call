# C-Call V2 — Draft Pull Request & Remaining External Acceptance Checklist

---

## Part A: Ready-to-Use GitHub Draft PR Description

**Base branch:** `main`  
**Compare branch:** `feat/c-call-v2-production-foundation`  
**Create PR URL:** `https://github.com/abcsoft/C-Call/pull/new/feat/c-call-v2-production-foundation`  
**PR Title:** `feat(v2): production foundation, cloud SIP/NAT hardening, billing atomicity, and live QA gates`

### PR Body (Copy-Paste)

```markdown
> **Draft PR — Do NOT merge into `main` until External Staging & Authorized Live Carrier Acceptance gates below pass.**

## Summary
Brings C-Call V2 (`feat/c-call-v2-production-foundation`) to production-ready local verification parity across web, SaaS subscription/billing, durable call queue workers, TTS quota/concurrency controls, cloud SIP/RTP NAT traversal, non-disruptive carrier diagnostics, and hardened deployment manifests.

### Key Engineering & Security Changes
- **Modular App Architecture & Hardening:** Split Flask blueprints (`auth`, `admin`, `customer`, `api`), production fail-fast secret/SSRF validation in `app/config.py`, CSRF + nonced CSP enforcement (zero inline `onclick`/`onsubmit`/`onchange` handlers in templates), Gunicorn `preload_app = False`, and isolated `run_scheduler` / `run_call_worker` entrypoints.
- **Database Schema & Atomic Billing (`migrations/001`–`028`):** Expand-first schema through `028_v2_security_and_reliability.sql`, durable `call_jobs` leasing (`FOR UPDATE SKIP LOCKED`), atomic wallet ledger constraints (`025`, `027`), rate snapshot immutability, and orphan lease/billing reconciliation.
- **SIP / RTP NAT & Non-Disruptive Diagnostics:**
  - Separate bind vs. advertised IPs (`SIP_BIND_IP`, `SIP_ADVERTISED_IP`, `RTP_BIND_IP`, `RTP_ADVERTISED_IP`) across `ivr_engine.py` (including `_refer` & `_invite_bridge_leg`), `direct_sip_call.py`, and `rtp_probe.py`; UDP-only SIP transport validation; RFC 3550 jitter/loss/latency telemetry; process-safe RTP port allocation.
  - Non-disruptive admin SIP diagnostic (`sip_provider.py`): UDP `OPTIONS` probe + RFC 3261 binding-query `REGISTER` (omits `Contact` and `Expires` so active calls and trunk registrations stay untouched), single UDP timeout retransmit resilience, and separate `[OPTIONS: ...]` / `[REGISTER: ...]` UI badges with zero credential leakage.
  - `scripts/ippbx_acceptance.py`: local mock IP-PBX rehearsal mode plus `--confirm-authorized-test --target <number>` safety gate for carrier call testing.
- **Tenant Lifecycle & Cleanup Safety:** Protected financial-history delete guards, multi-signal synthetic cleanup preview (`build_cleanup_audit_preview`), and active-call stale-reconciliation protection.
- **Deployment & Firewall Hardening:** Updated `docker-compose.production.yml`, `docker-compose.staging.coolify.yml`, `scripts/provision_production_server.sh`, `docs/PRODUCTION_DEPLOYMENT_GUIDE.md`, and `reports/release/` manifests/checklists (`ENABLE_CALL_MONITOR=true` singleton-guarded on `call_worker`, aligned `ccall_production`/`ccall_staging` DB names, `net.ipv4.ip_forward = 1`, and carrier-CIDR-restricted SIP/RTP firewall rules).

## Verification Evidence
- **Clean-Branch Automated Suite (`git archive` -> `ccall_test`, `BILLING_PROCESS_TEST_DB`):** **1,001 passed, 0 failed, 0 skipped** on commit `4880385` (`task-1742`).
  - *(Note: Local owner workspace also holds 80 net uncommitted WIP tests + associated local WIP edits in `retry.py`, `plans/form.html`, `platform_tts.html`, `_nav_links.html`, and `docs/UCART_INTEGRATION.md`, intentionally left uncommitted for explicit owner review.)*
- **Secret Scan:** `scripts/secret_scan.py --staged` — **PASS (no likely secrets found)**.
- **Live Port `:5000` Playwright Chromium Acceptance (`task-1715`):**
  - `11/11` Admin pages HTTP `200`, `12/12` Merchant pages HTTP `200`.
  - `0` console errors, `0` page errors, `0` HTTP `500`s, `0` failed requests, `0` leaked secrets.
  - Live `203.188.247.3:5060` non-disruptive probe rendered `OPTIONS: 404` & `REGISTER: 200` separately.
  - Verified Plan Create→Duplicate→Archive→Activate, `tts_provider="gemini"` HTTP `403` plan restriction denial (`0` paid TTS credits consumed), subscription cancel at period end, TTS usage `$0.1250` / CSV export, and `101 -> 101` existing merchants preserved untouched.
- **Backup & Restore:** Verified isolated PostgreSQL 16 restore test (`ccall_restore_verify`, `psql -v ON_ERROR_STOP=1` exit `0`, `44` tables, `28` migrations, cleanly dropped).

## Blocking Gates Before Merging to `main`
- [ ] AWS Coolify host access & isolated staging deployment (`docker-compose.staging.coolify.yml`)
- [ ] Container build, migration, worker restart recovery & health checks on Linux Docker host
- [ ] Live NAT (`SIP_ADVERTISED_IP` / `RTP_ADVERTISED_IP`) & carrier-restricted UDP firewall verification
- [ ] Authorized live IP-PBX call acceptance (`scripts/ippbx_acceptance.py --mode full-call --confirm-authorized-test --target <APPROVED_NUMBER>`)
```

---

## Part B: Remaining External Acceptance Checklist (Owner / Ops Sign-Off)

Every local engineering & browser acceptance gate has passed cleanly. The remaining external gates cannot run from this offline Windows workstation and must be completed before production cutover:

### 1. Owner GitHub & WIP Decisions
- [ ] Review the 5 uncommitted local owner test files (`test_plan_lifecycle.py`, `test_tts_limits.py`, `test_retry_settings.py`, `test_admin_tts_management.py`, `test_subscription_e2e.py`) and paired uncommitted app/template changes (`retry.py`, `plans/form.html`, `platform_tts.html`, `_nav_links.html`, `UCART_INTEGRATION.md`); decide which to commit into `feat/c-call-v2-production-foundation`.
- [ ] Open the GitHub Draft PR using `https://github.com/abcsoft/C-Call/pull/new/feat/c-call-v2-production-foundation` and the PR template in Part A (blocked here because `gh` CLI and `GITHUB_TOKEN` are absent).
- [ ] Explicitly review [SYNTHETIC-ACCOUNT-AUDIT-101.md](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/SYNTHETIC-ACCOUNT-AUDIT-101.md) before authorizing any archive/cleanup of the `101` local merchants (`4` safe-delete eligible, `89` synthetic domain with balance/orders, `8` real/protected).

### 2. AWS Singapore / Coolify Staging Execution
- [ ] Provide SSH/Coolify API access and configure `.env` secrets on the isolated `ccall-staging` Coolify project.
- [ ] Confirm Docker Compose build succeeds (`Dockerfile` multi-stage `python:3.11-slim` + `ffmpeg` + `ccall` UID 1001) without affecting existing applications or DBs.
- [ ] Run `migrate` (`001`–`028`), confirm `postgres`, `redis`, `web`, singleton `scheduler`, and `call_worker` start cleanly, pass `/health/live` & `/health/ready`, and recover automatically after container restart.
- [ ] Verify UFW + `DOCKER-USER` + AWS Security Group restrict UDP `5060` & `RTP_PORT_MIN:RTP_PORT_MAX` strictly to authorized SIP carrier IP(s), with `5000`/`5432`/`6379`/`9090`/`9093` closed publicly.
- [ ] Smoke-test Staging Admin & Merchant panels in browser over HTTPS and confirm `scripts/backup.sh` + `scripts/restore_verify.sh` on container volume dumps.

### 3. Authorized Live Carrier IP-PBX & Paid Integrations
- [ ] Supply explicit carrier authorization, live trunk credentials, `SIP_ADVERTISED_IP` / `RTP_ADVERTISED_IP`, and an approved destination test number.
- [ ] Execute authorized live call acceptance:
  ```bash
  python scripts/ippbx_acceptance.py \
    --mode full-call \
    --confirm-authorized-test \
    --target <APPROVED_TEST_NUMBER>
  ```
  Verify: `REGISTER 200 OK`, outbound `INVITE` -> `200 OK`, bidirectional RTP audio, DTMF capture, WAV recording saved & playable, graceful `BYE`, `REFER` transfer (if enabled), and accurate CDR + wallet billing debit.
- [ ] *(If enabled for production tenants)* Smoke-test one real ElevenLabs / Gemini TTS synthesis with production billing key and confirm `tenant_tts_usage` cost accounting matches provider invoice.
