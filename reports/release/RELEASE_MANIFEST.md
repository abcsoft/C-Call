# C-Call V2 — Final Release Manifest & Technical Readiness Audit

## 1. Release Identity
- **Repository:** `https://github.com/abcsoft/C-Call`
- **Release Branch:** `feat/c-call-v2-production-foundation` (do NOT merge directly to `main` until external staging + live carrier acceptance complete)
- **Prior Verified Baselines:** `4880385` (`1,001 passed, 0 failed, 0 skipped` clean archive, `task-1742`; full `:5000` acceptance, `task-1715`), `11468ee` (release artifact & firewall hardening)
- **Latest Database Schema Migration (`28/28` applied):** `028_v2_security_and_reliability.sql` (`migrations/001_initial_schema.sql` through `migrations/028_v2_security_and_reliability.sql`, applied via `python migrations/run_migrations.py`)
- **Final Integrated Verification (`task-1915` & `task-1951` + clean `git archive` freeze verification):**
  - Targeted integrated test suites + deployment checks (`task-1915`): **298 passed, 0 failed, 0 skipped** (`tests/unit/test_v2_deployment_release_checks.py`, `test_plan_lifecycle.py`, `test_tts_limits.py`, `test_retry_settings.py`, `test_admin_tts_management.py`, `test_subscription_e2e.py`).
  - Live port `:5000` focused browser verification (`task-1951`): Admin `/admin/platform-tts` & `/admin/plans/create` sidebar layout, Merchant `_nav_links` `Subscription & Billing` boosted navigation (`0` CSP console errors after `htmx-config` `inlineScriptNonce` fix in [base.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/base.html#L7)), and `/retry` disabled auto-retry save (`PASS`, `0` console/page/HTTP500 errors, `101 -> 101` tenants intact).
  - Full clean-environment `git archive HEAD` `pytest` verification executed on the release freeze commit against `ccall_test` (`BILLING_PROCESS_TEST_DB`).
- **Backup & Restore Verification:** Pre-acceptance `pg_dump` (`473,156` bytes) restored cleanly (`psql.exe -v ON_ERROR_STOP=1`, exit `0`) into isolated `ccall_restore_verify` (`tables=44`, `migrations=28`, `tenants=101`, `sip_providers=3`, `plans=3`), then dropped cleanly.

---

## 2. Reconciled Functionality & Integrated Tests (`+86` Net New Tests Over `4880385`)

All legitimate local WIP fixes and 80 net owner integration tests have been audited, security-scanned (`secret_scan.py`: PASS), and integrated cleanly alongside 6 deployment/call-monitor regression tests:

| File | Integration Action | Coverage & Purpose |
|---|---|---|
| [retry.py](file:///c:/xampp_lite_8_3/www/ccall/app/blueprints/customer/retry.py) | Committed | When `retry_enabled` is unchecked, marks tenant `retry_rules.is_enabled = FALSE` without failing validation on disabled attempt inputs; consolidates error flash. |
| [plans/form.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/admin/plans/form.html) | Committed | Uses `{% block admin_content %}` (restoring admin sidebar), provider status checkboxes (`allowed`/`byok`/`platform_managed`), plan billing defaults, and number entitlement fields. |
| [platform_tts.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/admin/platform_tts.html) | Committed | Uses `{% block admin_content %}` so Admin sidebar and layout render cleanly. |
| [_nav_links.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/customer/_nav_links.html) | Committed | Adds `Subscription & Billing` navigation link (`customer.subscription_page`) to merchant sidebar. |
| [base.html](file:///c:/xampp_lite_8_3/www/ccall/app/templates/base.html#L7) | Fixed & Committed | Adds `<meta name="htmx-config" content='{"inlineScriptNonce":"{{ csp_nonce }}"}'>` so `hx-boost="true"` navigation preserves CSP nonces on inline page scripts cleanly (`0` CSP console errors). |
| [test_plan_lifecycle.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_plan_lifecycle.py) | Committed (`+13`) | Plan Archive/Activate UI buttons, CSRF, audit log events, merchant visibility, non-admin `403`. |
| [test_tts_limits.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_tts_limits.py) | Committed (`+16`) | Daily TTS preview quota (`402`), tenant isolation, archived-plan block, Redis `tts_concurrency_slot()` semaphore. |
| [test_retry_settings.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_retry_settings.py) | Committed (`+24`) | `/retry` `max_attempts`, stale attempt cleanup, delay min/hr parsing, DND overnight push, scheduler rules, auto-retry-off save. |
| [test_admin_tts_management.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_admin_tts_management.py) | Committed (`+10` net) | `TestTtsPolicyUxValidation` (`+11` added, `-1` replaced) covering policy/provider cross-validation and form value preservation. |
| [test_subscription_e2e.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_subscription_e2e.py) | Committed (`+17`) | `TestSubscriptionPageLayout` & `TestSubscriptionSelectPlan` covering sidebar nav link, plan filtering, wallet balance checks, renewal & idempotency. |
| [test_v2_deployment_release_checks.py](file:///c:/xampp_lite_8_3/www/ccall/tests/unit/test_v2_deployment_release_checks.py) | Committed (`+6`) | Verifies Compose `build: .`, Coolify `env_file` `required: false`, DB isolation, `deploy_production.sh` source-build & network ordering, `provision_production_server.sh` `ip_forward=1`/`DOCKER-USER`/carrier-scoped SIP/RTP, and `call_monitor` advisory lock & graceful shutdown. |

**Unrelated/Legacy Files Preserved Untracked Locally (Excluded From Release Branch):**
- `check_db.py` (WordPress PHP helper), `backup.py`, `cleanup_openvoice_cache.py`, `schema_saas.sql`, `settings_page.html`, `.stash-recovery-31d75/`, `.github/workflows/*`, `AUDIT_VERIFICATION_2026-07-09.md`, `PERFORMANCE.md`, `prd.md`, `docs/admin-guide.md`, `docs/integration-guide.md`, `docs/openapi.yaml`, `nginx/nginx.conf`, `package.json`, `package-lock.json`, `tailwind.config.js`.

---

## 3. Runtime & Package Dependencies

### Python Packages (`requirements.txt` — Python `3.11+` / Docker `python:3.11-slim`)
- **Web & WSGI:** `Flask==3.0.3`, `Werkzeug==3.0.3`, `gunicorn==22.0.0`, `Flask-Login==0.6.3`, `Flask-Session==0.8.0`, `Flask-Limiter==3.8.0`, `Flask-Mail==0.10.0`, `Flask-WTF==1.2.1`, `Flask-Babel==4.0.0`, `python-dotenv==1.0.1`
- **Database & Cache:** `psycopg[binary]>=3.1`, `psycopg-pool>=3.1`, `SQLAlchemy>=2.0`, `redis==5.0.8`
- **Scheduler & Tasks:** `Flask-APScheduler==1.13.1`, `APScheduler==3.10.4`
- **Crypto & Validation:** `bcrypt==4.2.0`, `cryptography==49.0.0`, `email-validator>=2.0`, `pytz>=2024.1`
- **TTS:** `edge-tts==7.2.8`, `gTTS==2.5.4`
- **Observability:** `prometheus-client==0.25.0`, `sentry-sdk==2.63.0`, `structlog==26.1.0`
- **Test Only:** `pytest>=8.0` (plus Playwright in local QA `.venv`)

### OS / Infrastructure Dependencies
- **PostgreSQL 16** (`postgres:16-alpine`) + `psql` / `pg_dump` / `pg_restore`
- **Redis 7** (`redis:7-alpine`, password-protected `--requirepass`, AOF persistence)
- **ffmpeg** (installed in [Dockerfile](file:///c:/xampp_lite_8_3/www/ccall/Dockerfile) & [provision_production_server.sh](file:///c:/xampp_lite_8_3/www/ccall/scripts/provision_production_server.sh))
- **Docker Engine + Docker Compose v2** (or `systemd` + Nginx / OpenLiteSpeed on CyberPanel)
- **GPG (`gpg`) & `sha256sum`** for encrypted `scripts/backup.sh` dumps

---

## 4. Environment Variable Reference (`.env.production.example` & Compose Overlays)

| Category | Required Variables | Critical Defaults / Rules |
|---|---|---|
| **App & Security** | `FLASK_ENV=production`, `APP_ENV=production`, `DEBUG=false`, `TESTING=false`, `FLASK_SECRET_KEY` (128 hex chars), `API_KEY_PEPPER` (64 hex chars), `SIP_PASSWORD_ENCRYPTION_KEY` (valid Fernet key), `ALLOW_PRIVATE_WEBHOOKS_FOR_DEV=false` | `app/config.py` fails fast at boot if `FLASK_SECRET_KEY`, `SIP_PASSWORD_ENCRYPTION_KEY`, or `ALLOW_PRIVATE_WEBHOOKS_FOR_DEV` violate production constraints |
| **URLs & Proxy** | `APP_BASE_URL`, `API_BASE_URL`, `WEBHOOK_BASE_URL`, `APP_URL`, `SERVER_NAME`, `ADMIN_EMAIL`, `HOST=127.0.0.1`, `PORT=5000`, `TRUSTED_PROXIES=127.0.0.1` | Set `STAGING_DOMAIN` for Coolify Traefik staging (`docker-compose.staging.coolify.yml`) |
| **Database** | `DB_PASSWORD`, `DATABASE_URL` (`ccall_app` DML-only), `MIGRATION_DATABASE_URL` (`ccall_migrate` DDL at deploy time), `DB_NAME=ccall_production` | Staging overlay overrides `POSTGRES_DB=ccall_staging`, `POSTGRES_USER=ccall_staging_app`, `DATABASE_URL=${STAGING_DATABASE_URL}` |
| **Redis** | `REDIS_PASSWORD`, `REDIS_URL=redis://:CHANGE_ME_REDIS_PASSWORD@redis:6379/0`, `SESSION_REDIS_DB=0`, `RATELIMIT_REDIS_DB=1` | Password required; `call_monitor` fails closed if Redis unreachable |
| **Service Guards** | `ENABLE_SCHEDULER`, `ENABLE_CALL_WORKER`, `ENABLE_CALL_MONITOR` | Base `.env`: all `false`. `scheduler`: `ENABLE_SCHEDULER=true`. `call_worker`: `ENABLE_CALL_WORKER=true`, `ENABLE_CALL_MONITOR=true` |
| **SIP / RTP / NAT** | `SIP_TRANSPORT=udp`, `SIP_BIND_IP=0.0.0.0`, `RTP_BIND_IP=0.0.0.0`, `SIP_ADVERTISED_IP=<PUBLIC_IP>`, `RTP_ADVERTISED_IP=<PUBLIC_IP>`, `RTP_PORT_MIN=10000`, `RTP_PORT_MAX=20000`, `ASTERISK_DIAL_FORMAT=local_bd`, `MOCK_SIP_PROVIDER=false` | UDP-only SIP enforced (`SUPPORTED_SIP_TRANSPORTS={"udp"}`). Carrier access restricted via `SIP_CARRIER_CIDRS` |
| **WSGI & Workers** | `GUNICORN_BIND=0.0.0.0:5000`, `WEB_WORKERS=4`, `WEB_THREADS=2`, `CALL_WORKERS=4`, `CALL_MAX_DURATION_SECONDS=120`, `SIP_PROCESS_TIMEOUT_SECONDS=90`, `CALL_WORKER_POLL_SECONDS=2` | `gunicorn.conf.py` enforces `preload_app = False` |
| **Payments / TTS / Mail / S3** | `SSLCOMMERZ_*`, `BKASH_*`, `PAYMENT_WEBHOOK_SECRET`, `WOOCOMMERCE_WEBHOOK_SECRET`, `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`, `SENDGRID_API_KEY`, `SENTRY_DSN`, `METRICS_AUTH_TOKEN`, `BACKUP_ENCRYPTION_PASSPHRASE` | `SSLCOMMERZ_SANDBOX=false`, `BKASH_SANDBOX=false`, `MOCK_TTS_PROVIDER=false`, `MOCK_PAYMENT_PROVIDER=false` verified by `scripts/validate_production_secrets.py` |

---

## 5. Deployment, Coolify, Firewall & Call-Monitor Hardening Summary

1. **No Premature `ccall_internal` Network Assumption & Direct Source Builds (`docker-compose.production.yml`, `scripts/deploy_production.sh`):**
   - Added `build: .` and `env_file: [{path: .env, required: false}]` under `x-app-base` so Docker builds directly from source without GHCR access and Coolify environment injection succeeds without a physical `.env` file.
   - Updated `scripts/deploy_production.sh` to build locally when `BUILD_FROM_SOURCE=true` or if `docker pull` fails, start `postgres` & `redis` first (`up -d postgres redis`) so Compose creates the `internal` network before schema inspection, inspect existing schema via `docker compose run --rm --no-deps migrate`, take backups via `--profile backup`, and then run `migrate` → `web scheduler call_worker`.
2. **Staging & Production Database Isolation (`docker-compose.staging.coolify.yml`, `docker-compose.production.yml`):**
   - Production uses `ccall_production` / `ccall_app` + `postgres_data`; staging uses `ccall_staging` / `ccall_staging_app` + `staging_postgres_data`, `staging_redis_data`, `staging_audio_data`, `staging_backup_data`, and internal-only Traefik routing (`ports: []`).
3. **Call-Monitor Leadership & Graceful Shutdown (`order_service.py`, `run_call_worker.py`, `docker-compose.production.yml`):**
   - Enabled `ENABLE_CALL_MONITOR: "true"` on `call_worker`, guarded by PostgreSQL session advisory lock (`99999`) + Redis `ccall:monitor_leader` renewal.
   - Added `_monitor_stop_event` wake-up and `stop_call_monitor(wait=True, timeout=5.0)` invoked in `run_call_worker.main()` `finally:` block so `SIGTERM` shuts down cleanly and releases `pg_advisory_unlock(99999)` immediately.
4. **Firewall & Carrier-Restricted SIP/RTP (`scripts/provision_production_server.sh`):**
   - Set `net.ipv4.ip_forward = 1` for Docker bridge routing, replaced open `0.0.0.0/0` UDP RTP with `SIP_CARRIER_CIDRS`-restricted allow rules, and added `DOCKER-USER` chain guards blocking WAN `5432,6379,5000,9090,9093` and dropping unauthorized public UDP `5060`/RTP.

---

## 6. Deployment Commands

```bash
# 1. Validate production secrets (never prints secrets)
chmod 600 .env.production
python3 scripts/validate_production_secrets.py --env .env.production

# 2. Build from source, start DB & Redis, apply migrations (001..028), start services
BUILD_FROM_SOURCE=true bash scripts/deploy_production.sh

# 3. Coolify isolated staging (without disturbing existing apps)
docker compose -f docker-compose.production.yml -f docker-compose.staging.coolify.yml up -d --build postgres redis
docker compose -f docker-compose.production.yml -f docker-compose.staging.coolify.yml run --rm migrate
docker compose -f docker-compose.production.yml -f docker-compose.staging.coolify.yml up -d scheduler call_worker web

# 4. Health & smoke checks
curl -fsS http://127.0.0.1:5000/health/live
curl -fsS http://127.0.0.1:5000/health/ready
```
