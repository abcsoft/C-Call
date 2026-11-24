# C-Call V2 — Phase 1 Deep Audit Report

**Branch:** `feat/c-call-v2-production-foundation` (branched from `main` at `16753d1`)  
**Methodology:** Direct static and runtime inspection of all application blueprints, services, models, telephony engine (`call_engine/`), migrations (`001`–`027`), Docker/Nginx configs, and test suites.

---

## 1. Architecture & Complete Feature Inventory

C-Call is a multi-tenant SaaS voice automation & e-commerce order-confirmation IVR platform built on Python 3.11+/Flask 3.0.3, PostgreSQL (`psycopg3` + `psycopg_pool`), Redis 7, APScheduler, and a custom UDP SIP/RTP + RFC 2833 DTMF engine (`call_engine/`).

| Layer | Key Modules | Implemented Capabilities |
|---|---|---|
| **App Factory & Middleware** | `app/__init__.py`, `app/config.py`, `app/db.py`, `app/middleware/security.py`, `app/middleware/tenant_context.py`, `app/middleware/audit.py` | `create_app()` factory, CSP/security headers with per-request nonce, Redis server-side sessions, `X-API-Key` SHA-256 lookup, `TenantQuery` isolation, structlog phone-number masking, Sentry PII scrubbing. |
| **Auth & RBAC** | `app/blueprints/auth/routes.py`, `app/blueprints/auth/decorators.py`, `app/services/auth_service.py` | Registration, email verification, bcrypt (cost 12) login, Redis brute-force lockout (5 attempts / 15 min), password reset, team invitations, `@require_login`, `@require_role`, `@require_tenant_admin`, `@require_super_admin`, `@require_api_key`. |
| **Admin Console** | `app/blueprints/admin/routes.py`, `routes_numbers.py`, `routes_tts.py`, `app/services/admin_service.py` | Platform dashboard, tenant CRUD, wallet adjustments, tenant impersonation, DID number pool & SIP trunk management, plans & TTS policies, CDR/revenue exports. |
| **Customer Portal** | `app/blueprints/customer/routes.py`, `orders.py`, `flows.py`, `retry.py`, `settings.py`, `wallet.py`, `cdr.py`, `team.py` | Dashboard, quick-call, live-call monitor & hangup, IVR flow builder, order list/detail/recall/CSV export, retry rules & DND windows, wallet & SSLCommerz/mock recharge, TTS preview/settings, team invites. |
| **Public Ecommerce API** | `app/blueprints/api_public/orders.py`, `app/services/order_service.py`, `app/services/webhook_service.py` | `POST /api/v1/orders` (`/order-call`), `GET /api/v1/orders`, `GET /api/v1/orders/<id>`, `POST /api/v1/orders/<id>/recall`, idempotent `external_order_id`, rich e-commerce metadata, Bangla TTS script rendering, HMAC-SHA256 webhooks with SSRF guard. |
| **Telephony & Queue** | `call_engine/ivr_engine.py`, `call_engine/direct_sip_call.py`, `call_engine/call_runner.py`, `app/services/call_job_service.py`, `app/commands/run_call_worker.py` | Custom SIP REGISTER/INVITE/BYE + RTP μ-law streaming + RFC 2833 DTMF + WAV recording; durable `call_jobs` queue with `FOR UPDATE SKIP LOCKED`, worker leases, backoff retries, startup ACK sentinel, atomic billing finalization. |

---

## 2. Audit Findings (by Domain & Severity)

### A. Authentication, Authorization & Access Control

1. **[HIGH] Cross-Tenant IDOR in Quick Call (`flow_id` not scoped to tenant)**  
   - **Location:** `app/blueprints/customer/routes.py:201-212` (`quick_call()`)  
   - **Condition:** When `call_type == "flow"` and `flow_id` is supplied in the POST form, `flow_id = int(flow_id_str)` is passed directly to `call_runner.start_call(...)` without verifying `flows.tenant_id = g.tenant_id`.  
   - **Impact:** Any authenticated tenant admin/agent can place calls using another tenant's private IVR flow.  
   - **Fix:** Validate `SELECT id FROM flows WHERE id = %s AND tenant_id = %s` before dispatching; return 404/error if not found.

2. **[CRITICAL] Cross-Tenant Data Leak & Internal Path Disclosure in Debug Endpoint**  
   - **Location:** `app/blueprints/customer/routes.py:261-278` (`api_debug_live_registry()`)  
   - **Condition:** `GET /api/debug/live-registry` only requires `@require_login` and calls `registry.list_active_calls(tenant_id=None)`, returning all tenants' active calls and `call_runner_mod.__file__`.  
   - **Impact:** Any authenticated customer can enumerate active calls across all tenants and inspect server filesystem paths.  
   - **Fix:** Remove the temporary debug endpoint in production (or gate behind `@require_super_admin` and `current_app.config.get("DEBUG")`).

3. **[MEDIUM] Unauthenticated Email Enumeration & Missing Rate Limit on Password Reset**  
   - **Location:** `app/blueprints/auth/routes.py:189-205` (`forgot_password()`), `app/blueprints/auth/routes.py:254-271` (`check_email()`)  
   - **Condition:** `POST /auth/check-email` has no authentication and no rate limit, returning distinct HTML for registered vs unregistered emails. `POST /auth/forgot-password` has no `@limiter.limit(...)`.  
   - **Fix:** Add strict `@limiter.limit` to `/auth/forgot-password` and `/auth/check-email` (or disable `/auth/check-email` in production / require CSRF + rate limit).

---

### B. CSRF, Input Validation, Secret Storage & XSS

4. **[CRITICAL] Hardcoded Fallback Fernet Encryption Key for SIP & Tenant Secrets**  
   - **Location:** `app/models/sip_provider.py:8-18` (`FALLBACK_KEY`, `_get_fernet()`), reused by `app/models/tenant_secrets.py:15`  
   - **Condition:** If `SIP_PASSWORD_ENCRYPTION_KEY` is unset or malformed, `_get_fernet()` silently falls back to a hardcoded `FALLBACK_KEY` in `app/models/sip_provider.py:9`.  
   - **Impact:** In any deployment missing `SIP_PASSWORD_ENCRYPTION_KEY`, all SIP trunk passwords and tenant TTS API keys are encrypted with a publicly committed key.  
   - **Fix:** Fail closed in `FLASK_ENV=production` at startup (`app/config.py`) if `SIP_PASSWORD_ENCRYPTION_KEY` is missing/invalid; remove silent fallback in production.

5. **[HIGH] Webhook Secret Weak UUID Fallback & Plaintext Exposure**  
   - **Location:** `app/blueprints/customer/settings.py:98` (`integration_panel()`), `app/services/webhook_service.py:147` (`deliver_webhook()`)  
   - **Condition:** `settings.get("webhook_secret") or str(tenant["uuid"])` falls back to the tenant's UUID if `webhook_secret` is not set.  
   - **Fix:** Auto-generate a `whsec_` 256-bit secret when missing and remove the UUID fallback.

6. **[MEDIUM] Reflected Error XSS & Unbounded Numeric Inputs**  
   - **Location:** `app/blueprints/customer/routes.py:239-242` (`quick_call()`), `app/blueprints/customer/flows.py:357-358, 439-440`  
   - **Condition:** `quick_call()` interpolates raw `str(e)` into an HTML response without `markupsafe.escape()`. `flows.py` parses `gather_timeout_seconds` and `max_retries` without upper/lower bounds.  
   - **Fix:** Escape error strings with `markupsafe.escape()` and clamp `gather_timeout_seconds` (1–60) and `max_retries` (0–5).

---

### C. Call Recording & Audio File Privacy

7. **[CRITICAL] Arbitrary File Read / Path Traversal in `/recordings/play`**  
   - **Location:** `app/blueprints/customer/routes.py:315-342` (`play_recording()`)  
   - **Condition:** `abs_path = os.path.abspath(path)` is passed to `send_file(abs_path)` without verifying that `abs_path` is strictly inside `RECORDINGS_DIR` (nor checking `os.path.realpath` prefix).  
   - **Impact:** If a valid signature is ever obtained for a manipulated path or relative traversal, arbitrary server files could be read.  
   - **Fix:** Enforce `os.path.realpath(path)` starts with `os.path.realpath(RECORDINGS_DIR) + os.sep` in both `verify_signed_url()` / `play_recording()` and `serve_recording()`.

8. **[HIGH] Unauthenticated Audio Conversion & Path Traversal in `/sounds/<filename>`**  
   - **Location:** `app/blueprints/customer/routes.py:346-373` (`serve_sound()`)  
   - **Condition:** Missing `@require_login`; `sound_path = f"instance/sounds/{filename}"` allows `../` traversal and unauthenticated FFmpeg invocation.  
   - **Fix:** Require `@require_login` and `@require_role(["admin", "agent", "viewer"])`, validate `filename` with `werkzeug.utils.secure_filename` / `realpath` containment within `instance/sounds`, and verify tenant ownership where applicable.

---

### D. API Keys, Ecommerce API & Outbound Safety

9. **[HIGH] Single-Key-Only API Key Management & Missing Scopes/Quotas/Revocation Audit**  
   - **Location:** `app/blueprints/customer/settings.py:108-166` (`key_generate()`, `key_revoke()`), `app/middleware/tenant_context.py:94-123`  
   - **Condition:** `key_generate()` executes `DELETE FROM api_keys WHERE tenant_id = %s`, destroying all existing keys on rotation. `api_keys` lacks scopes, per-key rate/daily quotas, expiration, and soft-revocation (`revoked_at`) with audit logging.  
   - **Fix:** Add `scopes`, `daily_quota`, `expires_at`, `revoked_at` to `api_keys`; support non-destructive key rotation, scope enforcement in `@require_api_key`, per-key quota checks, and audit logging on create/rotate/revoke.

10. **[HIGH] Missing Outbound Destination Restrictions, Spending Limits, Consent & Opt-Out Enforcement**  
    - **Location:** `app/services/order_service.py:282-346`, `app/blueprints/customer/routes.py:162-188`  
    - **Condition:** Phone numbers are validated for BD mobile format, but there is no opt-out / DND blocklist table, no explicit consent tracking, no outbound destination prefix allowlist/blocklist, and no daily/monthly tenant spending cap beyond wallet balance.  
    - **Fix:** Add `opt_out_numbers` (DND/opt-out list), `consent_recorded_at`/`consent_source` on orders, configurable destination prefix policy, and tenant daily spending limit enforcement before queuing/dispatching calls.

11. **[MEDIUM] Raw PII Logged in `create_order` & Thread-Unsafe Global `urllib` Opener in Webhooks**  
    - **Location:** `app/services/order_service.py:287, 302`, `app/services/webhook_service.py:212`  
    - **Condition:** `create_order()` logs the raw `data` dict containing unmasked phone numbers. `deliver_webhook()` calls `urllib.request.install_opener(opener)` globally.  
    - **Fix:** Mask phone numbers before logging `data`; use `opener.open(req, ...)` instead of `install_opener()`.

---

### E. Calling Reliability & IVR State Machine

12. **[HIGH] IVR Retry Prompt Swallows Barge-In DTMF & Inaccurate Terminal States**  
    - **Location:** `call_engine/ivr_engine.py:867-941` (`_run_flow()`)  
    - **Condition:**  
      - Lines 913-915 and 932-934: `self._play_prompt(...)` during retry/invalid-digit re-prompting discards the returned barge-in `digit`, losing keypresses entered while the retry prompt plays.  
      - Line 940: `result.reached_terminal = True` is unconditionally set after the `while node is not None and visited_guard < 100:` loop exits — meaning a missing target node (`flow.nodes.get(next_id) is None`), missing `root_node_id`, or hitting the 100-step cycle guard is falsely reported as `reached_terminal = True`.  
    - **Fix:** Capture `barge = self._play_prompt(...)` on retry prompts; only set `result.reached_terminal = True` on explicit terminal nodes (`hangup`, `transfer`, terminal `message`), and emit explicit error/drop events for missing nodes or traversal limit exceeded.

13. **[HIGH] Call Job Queue State Coverage, Campaign Pause/Resume & Channel Limits**  
    - **Location:** `app/services/call_job_service.py:5-16`, `app/commands/run_call_worker.py:86-125`  
    - **Condition:** `call_jobs.job_status` only tracks `queued`, `leased`, `retry_wait`, `completed`, `failed`, `cancelled` — missing granular call lifecycle states (`preparing`, `dialing`, `ringing`, `answered`, `busy`, `no_answer`, `canceled`), campaign/tenant pause-resume controls, and configurable per-provider/global channel concurrency limits.  
    - **Fix:** Extend job/call state tracking & reconciliation, add tenant/campaign pause-resume and configurable `MAX_PROVIDER_CHANNELS` concurrency semaphore/db check in `run_call_worker.py`.

14. **[MEDIUM] IVR Flow Validation & Root Ownership Gaps**  
    - **Location:** `app/blueprints/customer/flows.py:331-533`, `call_engine/call_runner.py:226-258`  
    - **Condition:** `load_flow()` falls back to `node_rows[0]["id"]` if `root_node_id` is NULL without verifying flow integrity; `flows.py` does not validate unreachable nodes, missing audio on non-hangup nodes, or cycles; `flow_delete_node` leaves dangling `flow_transitions` pointing to deleted nodes if cascade isn't checked.  
    - **Fix:** Add server-side flow validator (root ownership, valid transitions, no missing target nodes, cycle/depth warning) and clean transition handling.

---

### F. SQLite & PostgreSQL Compatibility

15. **[HIGH] `app/` Package Hard-Blocks SQLite**  
    - **Location:** `app/config.py:154-157`, `app/db.py`, `migrations/run_migrations.py`  
    - **Condition:** `app/config.py` raises `RuntimeError("DATABASE_URL is required — C-Call IVR only supports PostgreSQL")` and `app/db.py` directly imports `psycopg` / `psycopg_pool`, while legacy root `db.py` uses SQLite.  
    - **Fix:** Provide a clean SQLite compatibility adapter / test harness (or dual-backend support in `db.py`/tests) so SQLite and PostgreSQL can be tested independently as required by Phase 6.

---

### G. Docker, Coolify Deployment, Dependencies & Secret History

16. **[HIGH] Production `docker-compose.production.yml` Blocks Outbound Egress & Lacks SIP/RTP Port/NAT Config**  
    - **Location:** `docker-compose.production.yml:20-21, 170-189, 222-225`  
    - **Condition:** `web`, `scheduler`, and `call_worker` are attached ONLY to `internal: true` network, which blocks outbound HTTPS (TTS APIs, merchant webhooks, SendGrid) and outbound SIP/RTP traffic.  
    - **Fix:** Keep `postgres` and `redis` on `internal: true` only; attach `web`, `scheduler`, and `call_worker` to both `internal` and an `egress` bridge network, and document/configure SIP/RTP networking and Coolify deployment.

17. **[LOW/INFO] Git History & Dependency Hygiene**  
    - `.env` is gitignored and has **not** been committed to git history (`git log --all --full-history -- .env` returned empty).  
    - `.env` on disk contains a local `ELEVENLABS_API_KEY` (untracked/gitignored).  
    - `requirements.txt` pins `Flask==3.0.3`, `Werkzeug==3.0.3`, `gunicorn==22.0.0`, `cryptography==49.0.0`, `bcrypt==4.2.0`; `.github/workflows/security_scan.yml` runs `safety`, `pip-audit`, and `semgrep` weekly.
