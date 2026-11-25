# C-Call V2 — Final Release Manifest & Technical Readiness Audit

## 1. Release Identity
- **Repository:** `https://github.com/abcsoft/C-Call`
- **Release Branch:** `feat/c-call-v2-production-foundation` (do NOT merge directly to `main` until external staging + live carrier acceptance complete)
- **Verified Code & Clean-Branch Test Baseline Commit:** `4880385` (preceded by `a2367f7`, `45054a7`, `4f46d2d`)
- **Latest Database Schema Migration (`28/28` applied):** `028_v2_security_and_reliability.sql` (`migrations/001_initial_schema.sql` through `migrations/028_v2_security_and_reliability.sql`, applied via `python migrations/run_migrations.py`)
- **Verified Clean-Branch Test Suite (`git archive 4880385` -> `ccall_test`, `task-1742`):** **1,001 passed, 0 failed, 0 skipped** in `606.27s`
- **Verified Live Owner Application (`http://127.0.0.1:5000`, `ccall` DB, `task-1715`):**
  - `11/11` Admin pages HTTP `200`, `12/12` Merchant pages HTTP `200`
  - `0` console errors, `0` page errors, `0` HTTP `500`s, `0` failed requests, `0` leaked secrets
  - Live non-disruptive SIP diagnostic (`203.188.247.3:5060`): `OPTIONS: 404` & `REGISTER: 200` (`Contact`/`Expires` omitted), active calls and SIP bindings untouched
  - Existing merchant preservation verified: `101 -> 101` accounts intact
- **Backup & Restore Verification:** Pre-acceptance `pg_dump` (`473,156` bytes) restored cleanly (`psql.exe -v ON_ERROR_STOP=1`, exit `0`) into isolated `ccall_restore_verify` (`tables=44`, `migrations=28`, `tenants=101`, `sip_providers=3`, `plans=3`), then dropped cleanly.

---

## 2. Uncommitted Local Owner Tests (`80` Net Tests) vs. Committed GitHub Branch (`1,001` Tests)

Running the uncommitted local owner workspace files unmodified against clean `git archive HEAD` (`ccall_test`, `task-1818`) yields exact dependency and coverage classification:

| Uncommitted / Modified Test File | Local Status | Committed Test Count Change | Result Against Clean `HEAD` | Coverage & Purpose | Dependencies & Missing Regression Analysis |
|---|---|---|---|---|---|
| [test_plan_lifecycle.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_plan_lifecycle.py) | Untracked | `+13` tests | **13 passed, 0 failed** | Admin plan `Archive`/`Activate` UI buttons, `archived_at` toggle, CSRF presence, merchant visibility filtering, non-admin `403`, `audit_log` writes | **Self-contained against committed `HEAD`.** Zero uncommitted app dependencies. *(Gap: plan archive/activate UI regression coverage is absent from GitHub until owner authorizes commit.)* |
| [test_tts_limits.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_tts_limits.py) | Untracked | `+16` tests | **16 passed, 0 failed** | Daily TTS preview quota (`402`), cross-tenant quota isolation, archived-plan block, Redis `tts_concurrency_slot()` semaphore (`app/services/tts_concurrent.py`) acquire/release/exception recovery/fail-open | **Self-contained against committed `HEAD`** (depends on `app/services/tts_concurrent.py`, committed in `a2367f7`). *(Gap: TTS concurrency semaphore & daily preview cap tests are absent from GitHub until owner authorizes commit.)* |
| [test_retry_settings.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_retry_settings.py) | Untracked | `+24` tests | **23 passed, 1 failed** | `/retry` `max_attempts` validation, stale attempt cleanup, delay min/hr parsing, DND overnight push logic, cumulative preview, tenant isolation, scheduler `max_attempts`, auto-retry-off toggle | **`test_auto_retry_off_saves_without_attempt_validation` depends on uncommitted edit in `app/blueprints/customer/retry.py`** (`if not retry_enabled: UPDATE retry_rules SET is_enabled = FALSE`). Other 23 tests cover committed `retry_service` & routes. |
| [test_admin_tts_management.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_admin_tts_management.py) (`TestTtsPolicyUxValidation` hunk) | Modified (`+11 / -1`) | `+10` net (`11` added, `1` replaced) | **59 passed, 1 failed** | Form validation matrix for `byok_only`, `platform_managed`, `hybrid`, provider allowlist cross-checks, form value preservation, archived plan edit load | All `11` tests in `TestTtsPolicyUxValidation` pass against `HEAD`; pre-existing `TestPlatformTtsPage.test_page_loads` fails on `HEAD` because it expects the sidebar block fix (`{% block admin_content %}`) present in uncommitted `app/templates/admin/platform_tts.html`. |
| [test_subscription_e2e.py](file:///c:/xampp_lite_8_3/www/ccall/tests/integration/test_subscription_e2e.py) (`TestSubscriptionPageLayout`, `TestSubscriptionSelectPlan`) | Modified (`+17`) | `+17` net | **178 passed (`179` parameterized items), 1 failed** | `/settings/subscription` layout checks, available/archived plan filtering, select-plan validation, insufficient wallet redirect, renewal state & idempotency, CSRF, tenant isolation | **`test_subscription_nav_item_present` depends on uncommitted edit in `app/templates/customer/_nav_links.html`** (`Subscription & Billing` sidebar link). |

**Total Reconciliation:** `1,001` committed + (`13 + 16 + 24 + 10 + 17 = 80` net uncommitted tests) = `1,081` local workspace tests (`task-1519`).
**Preservation Decision:** Uncommitted owner app/template/doc edits (`app/blueprints/customer/retry.py`, `app/templates/admin/plans/form.html`, `app/templates/admin/platform_tts.html`, `app/templates/customer/_nav_links.html`, `docs/UCART_INTEGRATION.md`) and all 80 owner WIP test changes remain preserved untouched in the local working tree for explicit owner review/commit.

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
| **Database** | `DB_PASSWORD`, `DATABASE_URL` (`ccall_app` DML-only), `MIGRATION_DATABASE_URL` (`ccall_migrate` DDL at deploy time), `DB_NAME=ccall_production` | Staging overlay overrides `POSTGRES_DB=ccall_staging`, `POSTGRES_USER=ccall_staging_app` |
| **Redis** | `REDIS_PASSWORD`, `REDIS_URL=redis://:CHANGE_ME_REDIS_PASSWORD@redis:6379/0`, `SESSION_REDIS_DB=0`, `RATELIMIT_REDIS_DB=1` | Password required; `call_monitor` fails closed if Redis unreachable |
| **Service Guards** | `ENABLE_SCHEDULER`, `ENABLE_CALL_WORKER`, `ENABLE_CALL_MONITOR` | Base `.env`: all `false`. `scheduler`: `ENABLE_SCHEDULER=true`. `call_worker`: `ENABLE_CALL_WORKER=true`, `ENABLE_CALL_MONITOR=true` |
| **SIP / RTP / NAT** | `SIP_TRANSPORT=udp`, `SIP_BIND_IP=0.0.0.0`, `RTP_BIND_IP=0.0.0.0`, `SIP_ADVERTISED_IP=<PUBLIC_IP>`, `RTP_ADVERTISED_IP=<PUBLIC_IP>`, `RTP_PORT_MIN=10000`, `RTP_PORT_MAX=20000`, `ASTERISK_DIAL_FORMAT=local_bd`, `MOCK_SIP_PROVIDER=false` | UDP-only SIP enforced (`SUPPORTED_SIP_TRANSPORTS={"udp"}`). Shrink `RTP_PORT_MIN..MAX` (e.g. `10000..10200`) if publishing bridge UDP ports |
| **WSGI & Workers** | `GUNICORN_BIND=0.0.0.0:5000`, `WEB_WORKERS=4`, `WEB_THREADS=2`, `CALL_WORKERS=4`, `CALL_MAX_DURATION_SECONDS=120`, `SIP_PROCESS_TIMEOUT_SECONDS=90`, `CALL_WORKER_POLL_SECONDS=2` | `gunicorn.conf.py` enforces `preload_app = False` |
| **Payments / TTS / Mail / S3** | `SSLCOMMERZ_*`, `BKASH_*`, `PAYMENT_WEBHOOK_SECRET`, `WOOCOMMERCE_WEBHOOK_SECRET`, `GEMINI_API_KEY`, `ELEVENLABS_API_KEY`, `SENDGRID_API_KEY`, `SENTRY_DSN`, `METRICS_AUTH_TOKEN`, `BACKUP_ENCRYPTION_PASSPHRASE` | `SSLCOMMERZ_SANDBOX=false`, `BKASH_SANDBOX=false`, `MOCK_TTS_PROVIDER=false`, `MOCK_PAYMENT_PROVIDER=false` verified by `scripts/validate_production_secrets.py` |

---

## 5. Docker Compose, Coolify Overlay & Firewall Review Findings (Fixes Applied vs. External)

1. **`ENABLE_CALL_MONITOR` was disabled in both `scheduler` and `call_worker` (FIXED):**
   - `docker-compose.production.yml` disabled `ENABLE_CALL_MONITOR` on `call_worker` claiming `scheduler` ran it, yet `app/commands/run_scheduler.py` never calls `start_call_monitor(app)`. Fixed by setting `ENABLE_CALL_MONITOR: "true"` on `call_worker` (protected by singleton PostgreSQL session advisory lock `99999` + Redis `ccall:monitor_leader` in `app/services/order_service.py`).
2. **Postgres DB name mismatch between Compose and `.env.production.example` (FIXED):**
   - `docker-compose.production.yml` used `POSTGRES_DB: ccall_prod`, whereas `.env.production.example` and `scripts/validate_production_secrets.py` require `ccall_production`. Updated Compose to `${POSTGRES_DB:-ccall_production}` and `${POSTGRES_USER:-ccall_app}`.
3. **Coolify Staging DB/Redis env overrides missing in `docker-compose.staging.coolify.yml` (FIXED):**
   - Added explicit `x-staging-env` overriding `DATABASE_URL` (`${STAGING_DATABASE_URL}`), `MIGRATION_DATABASE_URL`, and `REDIS_URL` on `migrate`, `web`, `scheduler`, `call_worker`, and `backup` so Coolify staging connects cleanly to `ccall_staging` without disturbing existing apps or exposing URL-embedded credentials in YAML.
   - *Operator note:* Base `x-app-base` retains `env_file: .env` for broad Compose compatibility; in Coolify ensure an empty `.env` exists or provide `.env` in project directory if Compose fails on missing file.
4. **Firewall broke Docker egress (`net.ipv4.ip_forward = 0`) & exposed `10000:20000/udp` to `0.0.0.0/0` (FIXED):**
   - Updated `scripts/provision_production_server.sh` to set `net.ipv4.ip_forward = 1` (required for Docker `egress` bridge routing) and removed blanket `ufw allow 10000:20000/udp` (`0.0.0.0/0`), replacing it with carrier-restricted `SIP_CARRIER_CIDRS` rules.
   - **Docker `DOCKER-USER` Note:** Standard `ufw deny 5432/6379/5000` only governs the `INPUT` chain, while Docker `FORWARD` rules evaluate `DOCKER` first. Ensure internal services remain unpublished (`internal: true`), `web` binds `127.0.0.1:5000:5000`, and enforce `DOCKER-USER` iptables rules (see [OPERATOR_CHECKLIST_SG_COOLIFY_CYBERPANEL.md](file:///c:/xampp_lite_8_3/www/ccall/reports/release/OPERATOR_CHECKLIST_SG_COOLIFY_CYBERPANEL.md)).
5. **Other Script & Image Notes (External/Deployment Timing):**
   - `scripts/deploy_production.sh`: expects pre-built image `ghcr.io/ccall/app:$VERSION` (or run `docker compose build` locally) and runs `docker run --network ccall_internal` before `up -d postgres`; bring `postgres` & `redis` up before Phase 3 migration.
   - `docker-compose.production.yml` (`call_worker`): `egress` bridge works for symmetric outbound SIP/RTP; if carrier-originated inbound RTP requires direct host sockets, bind `call_worker` on host networking (with `127.0.0.1` Postgres/Redis exposure or carrier-restricted `DOCKER-USER`) or publish a bounded RTP range (`RTP_PORT_MIN..MAX`, e.g., `10000-10200/udp`).

---

## 6. Deployment & Verification Commands

```bash
# 1. Validate production secrets (never prints secrets)
chmod 600 .env.production
python3 scripts/validate_production_secrets.py --env .env.production

# 2. Build/pull, start DB & Redis, apply migrations (001..028), verify schema
docker compose -f docker-compose.production.yml --env-file .env.production build
docker compose -f docker-compose.production.yml --env-file .env.production up -d postgres redis
docker compose -f docker-compose.production.yml --env-file .env.production run --rm migrate

# 3. Start singleton scheduler, call worker, and Gunicorn web
docker compose -f docker-compose.production.yml --env-file .env.production up -d scheduler call_worker web

# 4. Health & smoke verification
curl -fsS http://127.0.0.1:5000/health/live
curl -fsS http://127.0.0.1:5000/health/ready
SMOKE_BASE_URL="https://app.ccall.com.bd" bash scripts/smoke_test.sh

# 5. Coolify isolated staging (without disturbing existing apps)
touch .env   # if env_file placeholder needed alongside Coolify env injection
docker compose -f docker-compose.production.yml -f docker-compose.staging.coolify.yml up -d postgres redis
docker compose -f docker-compose.production.yml -f docker-compose.staging.coolify.yml run --rm migrate
docker compose -f docker-compose.production.yml -f docker-compose.staging.coolify.yml up -d scheduler call_worker web
```
