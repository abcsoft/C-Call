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
Brings C-Call V2 (`feat/c-call-v2-production-foundation`) to frozen release readiness across web, SaaS subscription/billing, durable call queue workers, TTS quota/concurrency controls, cloud SIP/RTP NAT traversal, non-disruptive carrier diagnostics, reconciled admin/merchant UI fixes, and hardened deployment manifests.

### Key Engineering, Security & Release Changes
- **Modular App Architecture & Hardening:** Split Flask blueprints (`auth`, `admin`, `customer`, `api`), production fail-fast secret/SSRF validation in `app/config.py`, CSRF + nonced CSP enforcement (zero inline JS handlers; `<meta name="htmx-config">` `inlineScriptNonce` in `base.html` preserving CSP nonces across `hx-boost` navigation), Gunicorn `preload_app = False`, and isolated `run_scheduler` / `run_call_worker` entrypoints.
- **Reconciled UI & Workflow Fixes:**
  - `retry.py`: auto-retry disabled state cleanly sets `retry_rules.is_enabled = FALSE` without failing on disabled attempt inputs.
  - `plans/form.html` & `platform_tts.html`: fixed `{% block admin_content %}` inheritance so admin sidebar & policy controls render properly.
  - `providers.html` & `tts_reservations.html`: fixed `"admin/base_admin.html"` + `{% block admin_content %}` inheritance, `<h1>` heading, and `overflow-x-auto` layout; fixed responsive table/header wrapping across `customers/list.html`, `numbers.html`, `cdr.html`, and `orders/list.html`; fixed `hx-boost="true"` Chart.js/Alpine script races via `base.html` `<head>` loading and shared `static/js/alpine_components.js`.
  - `wallet.py`, `customer/routes.py` & `cdr.html` / `_cdr_table_rows.html`: added `invalidate_wallet_cache(tenant_id)` clearing `wallet:{id}` and `tenant:stats:{id}` across all 5 wallet mutators, read fresh wallet balance on Dashboard KPIs, used `use_cache=False` for `order_service` balance error messages, fixed `/cdr` `expanded is not defined` JS error & immediate `#export-link` sync, and added missing `csrf_token` hidden inputs in `customer/settings.html`, `orders/detail.html`, and `orders/_tr_rows.html`.
  - `_nav_links.html`: added merchant `Subscription & Billing` navigation link (`customer.subscription_page`) and fixed `order_detail` / `tts_reservations_page` active states.
- **Database Schema & Atomic Billing (`migrations/001`–`028`):** Expand-first schema through `028_v2_security_and_reliability.sql`, durable `call_jobs` leasing (`FOR UPDATE SKIP LOCKED`), atomic wallet ledger constraints (`025`, `027`), rate snapshot immutability, and orphan lease/billing reconciliation.
- **SIP / RTP NAT & Non-Disruptive Diagnostics:**
  - Separate bind vs. advertised IPs (`SIP_BIND_IP`, `SIP_ADVERTISED_IP`, `RTP_BIND_IP`, `RTP_ADVERTISED_IP`) across `ivr_engine.py`, `direct_sip_call.py`, and `rtp_probe.py`; UDP-only SIP enforcement; RFC 3550 jitter/loss/latency telemetry; process-safe RTP port allocation.
  - Non-disruptive admin SIP diagnostic (`sip_provider.py`): UDP `OPTIONS` + RFC 3261 binding-query `REGISTER` (omits `Contact`/`Expires`), single UDP timeout retransmit resilience, and separate `[OPTIONS: ...]` / `[REGISTER: ...]` UI badges.
  - `scripts/ippbx_acceptance.py`: local mock IP-PBX rehearsal mode plus `--confirm-authorized-test --target <number>` safety gate for carrier testing.
- **Deployment, Worker Monitor & Firewall Hardening:**
  - `docker-compose.production.yml` & `scripts/deploy_production.sh`: added `build: .` + local source-build fallback when GHCR is unavailable, Coolify-compatible `env_file` `required: false`, Postgres-first startup before network/migration inspection, and `ENABLE_CALL_MONITOR=true` on `call_worker`.
  - `order_service.py` & `run_call_worker.py`: singleton PG advisory lock (`99999`) + `_monitor_stop_event` graceful shutdown (`stop_call_monitor(wait=True)`) on worker exit.
  - `docker-compose.staging.coolify.yml`: isolated `ccall_staging` database, volumes, and Traefik routing.
  - `scripts/provision_production_server.sh`: `net.ipv4.ip_forward = 1` for Docker egress, `SIP_CARRIER_CIDRS`-restricted UDP SIP/RTP firewalling, and `DOCKER-USER` iptables protection.

## Verification Evidence
- **Clean-Branch Automated Suite (`git archive HEAD` -> `ccall_test`, `BILLING_PROCESS_TEST_DB`):** Verified with **0 failed, 0 skipped** across all committed unit & integration tests (reconciling previous `1,001` baseline + `80` net integrated owner integration tests + `6` deployment/call-monitor regression tests).
- **Secret Scan:** `scripts/secret_scan.py --staged` — **PASS (no likely secrets found)**.
- **Live Port `:5000` Playwright Chromium Acceptance:**
  - `11/11` Admin pages + `12/12` Merchant pages HTTP `200`, plus focused checks on `/admin/platform-tts`, `/admin/plans/create`, boosted `/settings/subscription` nav link, and `/retry` disabled save.
  - `0` console/CSP errors, `0` page errors, `0` HTTP `500`s, `0` failed requests, `0` leaked secrets.
  - Live `203.188.247.3:5060` non-disruptive probe rendered `OPTIONS: 404` & `REGISTER: 200` separately; Plan Create→Duplicate→Archive→Activate, `tts_provider="gemini"` HTTP `403` restriction (`0` paid TTS credits consumed), subscription cancel at period end, TTS usage `$0.1250` / CSV export, and `101 -> 101` merchants preserved untouched.
- **Backup & Restore:** Verified isolated PostgreSQL 16 restore test (`ccall_restore_verify`, `psql -v ON_ERROR_STOP=1` exit `0`, `44` tables, `28` migrations, cleanly dropped).

## Blocking Gates Before Merging to `main`
- [ ] AWS Coolify host access & isolated staging deployment (`docker-compose.staging.coolify.yml`)
- [ ] Container build, migration, worker restart recovery & health checks on Linux Docker host
- [ ] Live NAT (`SIP_ADVERTISED_IP` / `RTP_ADVERTISED_IP`) & carrier-restricted UDP firewall (`SIP_CARRIER_CIDRS` + `DOCKER-USER`) verification
- [ ] Authorized live IP-PBX call acceptance (`scripts/ippbx_acceptance.py --mode full-call --confirm-authorized-test --target <APPROVED_NUMBER>`)
```

---

## Part B: Remaining External Acceptance Checklist (Owner / Ops Sign-Off)

Every local engineering, test-reconciliation, and browser acceptance gate has passed cleanly. Remaining external gates cannot execute from this offline Windows workstation:

### 1. GitHub PR & Data Governance
- [ ] Open the GitHub Draft PR using `https://github.com/abcsoft/C-Call/pull/new/feat/c-call-v2-production-foundation` and Part A above (blocked locally because `gh` CLI and `GITHUB_TOKEN` are absent).
- [ ] Explicitly review [SYNTHETIC-ACCOUNT-AUDIT-101.md](file:///c:/xampp_lite_8_3/www/ccall/reports/qa/SYNTHETIC-ACCOUNT-AUDIT-101.md) before authorizing any archive/cleanup of the `101` local merchants (`4` safe-delete eligible, `89` synthetic domain with balance/orders, `8` real/protected).

### 2. AWS Singapore / Coolify / CyberPanel Execution
- [ ] Provide SSH/Coolify API access and configure runtime secrets on the isolated `ccall-staging` Coolify project (`STAGING_DATABASE_URL`, `STAGING_REDIS_URL`, `FLASK_SECRET_KEY`, `API_KEY_PEPPER`, `SIP_PASSWORD_ENCRYPTION_KEY`, `STAGING_DOMAIN`).
- [ ] Confirm Docker Compose build succeeds directly from source without GHCR access and leaves existing host apps/DBs undisturbed.
- [ ] Run `migrate` (`001`–`028`), verify `postgres`, `redis`, `web`, singleton `scheduler`, and `call_worker` pass `/health/live` & `/health/ready`, verify `call_monitor` advisory lock release on worker restart, and confirm container restart recovery.
- [ ] Supply `SIP_CARRIER_CIDRS` and verify AWS Security Group + UFW + `DOCKER-USER` restrict UDP `5060` & `RTP_PORT_MIN:RTP_PORT_MAX` strictly to authorized SIP carrier IP(s), keeping `5000`/`5432`/`6379`/`9090`/`9093` private.
- [ ] Smoke-test Staging Admin & Merchant panels over HTTPS and verify `scripts/backup.sh` + `scripts/restore_verify.sh`.

### 3. Authorized Live Carrier IP-PBX & Paid Integrations
- [ ] Supply explicit carrier authorization, live trunk credentials, `SIP_ADVERTISED_IP` / `RTP_ADVERTISED_IP`, and an approved destination test number.
- [ ] Execute authorized live call acceptance:
  ```bash
  python scripts/ippbx_acceptance.py \
    --mode full-call \
    --confirm-authorized-test \
    --target <APPROVED_TEST_NUMBER>
  ```
  Verify `REGISTER 200 OK`, outbound `INVITE` -> `200 OK`, bidirectional RTP audio, DTMF capture, WAV recording saved & playable, graceful `BYE`, `REFER` transfer (if enabled), and accurate CDR + wallet billing debit.
- [ ] *(If enabled for production tenants)* Smoke-test one real ElevenLabs / Gemini TTS synthesis with production billing key and confirm `tenant_tts_usage` cost accounting matches provider invoice.
