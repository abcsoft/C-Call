# CLAUDE.md — C-Call IVR SaaS Platform

> This file is read by Claude Code and AI coding assistants to understand the project.
> Keep this file updated as the project evolves.

---

## What Is This Project?

**C-Call IVR** is a multi-tenant SaaS voice automation platform for Bangladesh e-commerce merchants.

Core flow:
```
Merchant places order → C-Call auto-dials customer → Customer presses 1 (Confirm) or 2 (Cancel)
→ Order status updated → Webhook sent back to merchant's store
```

If no answer → retry engine schedules up to 4 more attempts with configurable delays and DND window.

**Live at:** `https://app.ccall.com.bd`  
**PRD:** `prd.md` in project root  
**Dev Prompts:** `docs/dev-prompts.md`

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Web Framework | Flask 3.x — Application Factory + Blueprints |
| Database | PostgreSQL 16 — psycopg3 with psycopg-pool |
| Cache / Sessions | Redis 7 — Flask-Session |
| Task Scheduler | APScheduler 3.x — PostgreSQL JobStore |
| Frontend | Jinja2 + Tailwind CSS + HTMX + Alpine.js |
| Auth (web) | Flask-Login — session cookie |
| Auth (API) | SHA-256 hashed API keys — X-API-Key header |
| Email | Flask-Mail + SendGrid |
| Payments | SSLCommerz (primary) + bKash |
| Audio Storage | Local disk (dev) → MinIO / S3 (prod) |
| Error Tracking | Sentry |
| Metrics | Prometheus + Grafana |
| WSGI | Gunicorn (4 workers, preload) |
| Proxy | Nginx |

---

## Project Structure

```
c-call-ivr/
│
├── app/                              ← Main application package (NEW)
│   ├── __init__.py                   ← create_app() factory
│   ├── extensions.py                 ← db, login_manager, scheduler, redis, limiter
│   ├── config.py                     ← Dev / Prod / Test configs
│   ├── db.py                         ← psycopg3 query helpers
│   ├── exceptions.py                 ← Custom exceptions
│   ├── monitoring.py                 ← Prometheus metrics
│   ├── health.py                     ← /health endpoints
│   │
│   ├── blueprints/
│   │   ├── auth/                     ← /auth/* (register, login, verify, reset)
│   │   ├── admin/                    ← /admin/* (super admin panel)
│   │   ├── customer/                 ← / (customer panel — dashboard, orders, wallet)
│   │   └── api_public/               ← /api/v1/* (REST API — API key auth)
│   │
│   ├── services/                     ← Business logic layer
│   │   ├── auth_service.py
│   │   ├── order_service.py          ← Core: create_order, queue_call, update_status
│   │   ├── wallet_service.py         ← Atomic balance deduction / credit
│   │   ├── rate_service.py           ← calculate_charge(talk_seconds, tenant_id)
│   │   ├── retry_service.py          ← schedule_retry, process_due_retries
│   │   ├── webhook_service.py        ← deliver_async, retry_failed_webhooks
│   │   ├── number_service.py         ← select_caller_id (dedicated → shared → fallback)
│   │   ├── tts_service.py            ← Wrapper around existing audio.py
│   │   ├── payment_service.py        ← SSLCommerz + bKash
│   │   ├── sms_service.py            ← SMSLayer fallback
│   │   ├── recording_service.py      ← Signed URL generation
│   │   ├── email_service.py          ← Transactional emails
│   │   ├── dashboard_service.py      ← Stats aggregation
│   │   ├── admin_service.py          ← Platform stats, impersonation
│   │   └── revenue_service.py        ← Revenue reports
│   │
│   ├── models/                       ← Raw SQL query functions (no ORM)
│   │   ├── tenant.py
│   │   ├── user.py
│   │   ├── order.py
│   │   ├── wallet.py
│   │   ├── phone_number.py
│   │   ├── sip_provider.py
│   │   └── webhook_log.py
│   │
│   ├── middleware/
│   │   ├── tenant_context.py         ← Inject g.tenant_id on every request
│   │   ├── security.py               ← CSP, rate limits, request logging
│   │   └── audit.py                  ← Audit log for sensitive actions
│   │
│   └── templates/
│       ├── base.html
│       ├── auth/                     ← login.html, register.html, etc.
│       ├── admin/                    ← base_admin.html, dashboard, customers, numbers
│       ├── customer/                 ← base_customer.html, dashboard, orders, wallet
│       └── emails/                   ← verification.html, reset_password.html, alerts
│
├── call_engine/                      ← ⚠ EXISTING CODE — READ ONLY
│   ├── ivr_engine.py                 ← SIP/RTP IVR engine — DO NOT MODIFY
│   ├── direct_sip_call.py            ← Direct one-way call — DO NOT MODIFY
│   ├── call_runner.py                ← Extended with tenant_id, order_id, caller_id
│   └── audio.py                      ← TTS pipeline — DO NOT MODIFY
│
├── services/                         ← ⚠ EXISTING TTS SERVICES — READ ONLY
│   ├── edge_tts_service.py
│   ├── openvoice_service.py
│   └── tts_manager.py
│
├── migrations/
│   ├── 001_initial_schema.sql        ← Original schema (flows, calls, etc.)
│   ├── 002_saas_layer.sql            ← SaaS tables (tenants, orders, wallets, etc.)
│   └── run_migrations.py             ← Migration runner script
│
├── tests/
│   ├── conftest.py                   ← Fixtures: app, client, db, tenant, auth_client
│   ├── unit/                         ← Pure function tests (no DB)
│   └── integration/                  ← Full request tests (with test DB)
│
├── docs/
│   ├── dev-prompts.md                ← 32 development prompts (phase-by-phase)
│   ├── openapi.yaml                  ← API specification
│   ├── integration-guide.md          ← WooCommerce + custom API guide
│   └── admin-guide.md
│
├── static/
│   ├── css/tailwind.css              ← Purged Tailwind (< 20KB)
│   └── js/                           ← htmx.min.js, alpine.min.js, chart.min.js
│
├── instance/                         ← ⚠ GITIGNORED — local DB, audio, recordings
├── woocommerce-plugin/               ← PHP WooCommerce plugin (Phase 8)
│
├── CLAUDE.md                         ← This file
├── prd.md                            ← Product Requirements Document
├── README.md                         ← Setup and run instructions
├── requirements.txt
├── .env.example
├── .env                              ← ⚠ GITIGNORED — never commit
├── run.py                            ← Entry point
├── gunicorn.conf.py
├── Dockerfile
├── docker-compose.yml
└── Makefile
```

---

## ⚠ Critical Rules — Always Follow These

### 1. Multi-tenant Isolation (Most Important)
Every DB query on tenant-scoped tables **MUST** include `tenant_id`:

```python
# ✅ CORRECT
db.query("SELECT * FROM orders WHERE tenant_id = %s AND id = %s", (g.tenant_id, order_id))

# ❌ WRONG — never do this
db.query("SELECT * FROM orders WHERE id = %s", (order_id,))
```

Tenant-scoped tables: `orders`, `calls`, `flows`, `contacts`, `wallets`, `wallet_transactions`,
`api_keys`, `retry_rules`, `customer_rates`, `customer_number_assignments`, `webhook_logs`,
`order_call_attempts`, `tts_cache`, `cdr_records`, `users`, `payment_intents`, `sms_logs`

### 2. Wallet Deductions — Always Atomic

```python
# ✅ CORRECT — single transaction
with db.transaction() as conn:
    conn.execute("SELECT balance FROM wallets WHERE tenant_id = %s FOR UPDATE", (tenant_id,))
    # check balance
    conn.execute("UPDATE wallets SET balance = balance - %s WHERE tenant_id = %s", (charge, tenant_id))
    conn.execute("INSERT INTO wallet_transactions ...", (...))

# ❌ WRONG — two separate queries, race condition possible
db.execute("UPDATE wallets SET balance = balance - %s ...", ...)
db.execute("INSERT INTO wallet_transactions ...", ...)
```

### 3. API Keys — Hash Only

```python
# ✅ Store
import hashlib
key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
db.execute("INSERT INTO api_keys (key_hash, ...) VALUES (%s, ...)", (key_hash,))

# ✅ Verify
incoming_hash = hashlib.sha256(request.headers['X-API-Key'].encode()).hexdigest()
row = db.query_one("SELECT * FROM api_keys WHERE key_hash = %s AND is_active = true", (incoming_hash,))

# ❌ Never store raw key
db.execute("INSERT INTO api_keys (key) VALUES (%s)", (raw_key,))  # WRONG
```

### 4. Phone Numbers — Always Mask in Logs

```python
# ✅ CORRECT
def mask_phone(phone: str) -> str:
    if len(phone) >= 7:
        return phone[:3] + '****' + phone[-3:]
    return '***'

logger.info(f"Calling {mask_phone(phone)}")  # → "017****765"

# ❌ WRONG
logger.info(f"Calling {phone}")  # leaks PII
```

### 5. No String Concatenation in SQL

```python
# ✅ CORRECT
db.query("SELECT * FROM orders WHERE status = %s", (status,))

# ❌ WRONG — SQL injection risk
db.query(f"SELECT * FROM orders WHERE status = '{status}'")
```

### 6. Passwords — bcrypt cost 12

```python
import bcrypt
password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
```

### 7. Existing Call Engine Files — Read Only

These files work. Do not modify them unless specifically asked:
- `call_engine/ivr_engine.py`
- `call_engine/direct_sip_call.py`
- `call_engine/audio.py`
- `services/edge_tts_service.py`
- `services/openvoice_service.py`
- `services/tts_manager.py`

`call_engine/call_runner.py` is extended (not rewritten) — only add `tenant_id`, `order_id`, `caller_id` support.

---

## Database Schema Summary

### New SaaS Tables (migrations/002_saas_layer.sql)

| Table | Purpose |
|-------|---------|
| `tenants` | Merchant business accounts |
| `users` | Login accounts (belong to a tenant) |
| `plans` | Free / Starter / Pro / Enterprise tiers |
| `orders` | E-commerce orders received via API |
| `order_call_attempts` | Each call attempt per order |
| `wallets` | Pre-paid balance per tenant |
| `wallet_transactions` | Every credit/debit event |
| `payment_intents` | SSLCommerz / bKash payment sessions |
| `sip_providers` | SIP trunk credentials (password AES-256 encrypted) |
| `phone_numbers` | Pool of caller ID numbers |
| `customer_number_assignments` | Which numbers belong to which tenant |
| `customer_rates` | Per-tenant call rate overrides |
| `retry_rules` | Retry schedule per attempt per tenant |
| `api_keys` | SHA-256 hashed REST API keys |
| `webhook_logs` | Outgoing webhook delivery history |
| `sessions` | Server-side session store |
| `email_verifications` | Email verify / password reset tokens |

### Order Status Flow

```
NEW → CALL_QUEUED → CALLING
                      ├── CONFIRMED   (DTMF 1)
                      ├── CANCELLED   (DTMF 2)
                      ├── NO_ANSWER   → RETRY_SCHEDULED → CALLING (loop)
                      ├── UNREACHABLE (max retries exhausted)
                      └── FAILED      (technical failure)
```

### Key Indexes (already in migration)

```sql
-- Retry scheduler (runs every 30s)
CREATE INDEX ON orders (next_retry_at)
WHERE order_status = 'RETRY_SCHEDULED';

-- Order list per tenant
CREATE INDEX ON orders (tenant_id, created_at DESC);

-- CDR per tenant
CREATE INDEX ON calls (tenant_id, started_at DESC);
```

---

## API Overview

### Public REST API (X-API-Key auth)

```
POST   /api/v1/orders              ← receive order, queue call
GET    /api/v1/orders/:id          ← order status + attempts
GET    /api/v1/orders              ← list orders
POST   /api/v1/orders/:id/recall   ← manual re-call
GET    /api/v1/health              ← platform health
```

### Order Request Body

```json
{
  "order_id": "WC-10053",
  "phone": "01711234567",
  "customer_name": "Rafiqul Islam",
  "amount": "1200.00",
  "items": "2x Panjabi",
  "store_name": "Dhaka Fashion",
  "webhook_url": "https://mystore.com/webhook/ccall"
}
```

### Webhook Payload (outbound to merchant)

```json
{
  "event": "order.confirmed",
  "order_id": "WC-10053",
  "order_status": "CONFIRMED",
  "dtmf_digit": "1",
  "talk_seconds": 32,
  "confirmed_at": "2026-06-19T10:34:22Z",
  "signature": "sha256=abc123..."
}
```

Events: `order.confirmed` | `order.cancelled` | `order.no_answer` | `order.unreachable` | `order.failed`

---

## Authentication

| Context | Method |
|---------|--------|
| Web panel | Session cookie — Flask-Login + Redis |
| REST API | `X-API-Key` header — SHA-256 hash lookup |
| Webhooks (outbound) | HMAC-SHA256 signature on payload |

### Role Hierarchy

```
super_admin    → full platform access (tenant_id IS NULL in DB)
tenant_admin   → full access to own tenant
tenant_agent   → view orders, CDR, balance only
tenant_viewer  → read-only orders and CDR
```

### Decorators Available

```python
@require_login           # redirect to /auth/login if not authenticated
@require_tenant_admin    # 403 if role != 'admin'
@require_super_admin     # 403 if not super_admin
@require_api_key         # 401 JSON if X-API-Key invalid
@require_role(['admin', 'agent'])  # flexible role check
```

---

## Environment Variables

```bash
# Required
DATABASE_URL=postgresql://ccall:password@localhost:5432/ccall
REDIS_URL=redis://localhost:6379/0
SECRET_KEY=<64-char random string>
FLASK_ENV=production

# SIP (can also be set in admin panel)
SIP_DOMAIN=sip.amarip.com
SIP_PORT=5060
SIP_USER=your_sip_user
SIP_PASSWORD=your_sip_password

# Call Behaviour
CALL_WORKERS=4
CALL_MAX_DURATION_SECONDS=120
RECORD_CALLS=1
FFMPEG_PATH=ffmpeg

# TTS (optional — can be set per tenant in UI)
GEMINI_API_KEY=
ELEVENLABS_API_KEY=

# Security
SIP_PASSWORD_ENCRYPTION_KEY=<Fernet key>

# Payments
SSLCOMMERZ_STORE_ID=
SSLCOMMERZ_STORE_PASSWORD=
SSLCOMMERZ_SANDBOX=true
BKASH_APP_KEY=
BKASH_APP_SECRET=
BKASH_USERNAME=
BKASH_PASSWORD=
BKASH_SANDBOX=true

# Email
SENDGRID_API_KEY=
MAIL_FROM=noreply@ccall.com.bd

# Monitoring
SENTRY_DSN=

# App
APP_URL=https://app.ccall.com.bd
ADMIN_EMAIL=admin@ccall.com.bd
TTS_MAX_CONCURRENT=2
```

---

## Development Setup

```bash
# 1. Clone and enter project
git clone <repo>
cd c-call-ivr

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
# .venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment
cp .env.example .env
# Edit .env with your values

# 5. Start PostgreSQL and Redis (Docker)
docker-compose up -d postgres redis

# 6. Run migrations
python migrations/run_migrations.py

# 7. Start development server
python run.py
# → http://127.0.0.1:5000

# Or with Docker (full stack)
docker-compose up
```

### Makefile shortcuts

```bash
make dev        # docker-compose up (full stack)
make test       # pytest tests/ -v
make lint       # ruff check . && ruff format .
make migrate    # python migrations/run_migrations.py
make shell      # Flask shell with app context
make logs       # docker-compose logs -f app
```

---

## Running Tests

```bash
# All tests
pytest tests/ -v

# Unit tests only (fast, no DB)
pytest tests/unit/ -v

# Integration tests (needs PostgreSQL)
pytest tests/integration/ -v

# With coverage
pytest tests/ --cov=app --cov-report=html

# Specific test file
pytest tests/integration/test_order_flow.py -v

# Load tests (needs running server)
locust -f tests/load/locustfile.py --headless -u 100 -r 10 --run-time 30s
```

**Coverage target:** >80% on `app/services/` and `app/blueprints/api_public/`

---

## Service Layer — Key Functions

### order_service.py
```python
create_order(tenant_id, data)              # validate + create + queue call
queue_confirmation_call(order_id, tid)     # select number, gen TTS, dispatch
update_status_from_call(order_id, ...)     # DTMF → status, deduct wallet, webhook
manual_recall(order_id, tenant_id)         # admin-triggered re-call
```

### wallet_service.py
```python
check_balance(tenant_id, required)         # True/False
deduct(tenant_id, amount, description)     # ATOMIC — raises InsufficientBalanceError
credit(tenant_id, amount, description)     # ATOMIC
get_balance(tenant_id)                     # Decimal
```

### rate_service.py
```python
calculate_charge(talk_seconds, tenant_id)  # Decimal — ceil(seconds/60) × rate
estimate_charge(tenant_id)                 # min_charge for pre-call balance check
get_tenant_rate(tenant_id)                 # {rate_per_min, rate_per_call, min_charge}
```

### retry_service.py
```python
schedule_retry(order_id, tenant_id, n)    # set RETRY_SCHEDULED + next_retry_at
process_due_retries()                      # APScheduler job — runs every 30s
cancel_pending_retries(order_id)           # for manual cancel
```

### number_service.py
```python
select_caller_id(tenant_id)               # dedicated → shared round-robin → None
```

---

## Frontend Patterns

### HTMX — Use for:
- Live order status polling: `hx-trigger="every 5s"` (stop when terminal status)
- Dashboard KPI refresh: `hx-trigger="every 30s"`
- CDR pagination: `hx-get` on Next/Prev buttons
- Inline form feedback: submit → validate → swap error or success

### Alpine.js — Use for:
- Modals (confirm dialogs, assign number, recharge)
- Tab switching (integration guide tabs)
- Dynamic form rows (retry rules)
- TTS variable chip insertion (cursor position)

### Chart.js — Use for:
- Dashboard 7-day stacked bar (confirmed / cancelled / no_answer)
- Admin revenue chart (30-day daily bars)
- Status distribution pie chart

### CSS conventions:
- Tailwind utility classes only — no custom CSS files
- Status badge colors: `bg-green-100 text-green-800` (confirmed), `bg-red-100 text-red-800` (cancelled), `bg-yellow-100 text-yellow-800` (no_answer), `bg-gray-100 text-gray-600` (unreachable)
- Low balance alert: `bg-yellow-50 border-yellow-400`
- Admin panel: distinct color scheme from customer panel (use indigo/slate vs teal)

---

## Scheduler Jobs

All managed by APScheduler with PostgreSQL JobStore (survives restarts):

| Job ID | Interval | Function |
|--------|----------|----------|
| `process_retries` | 30 seconds | `retry_service.process_due_retries()` |
| `retry_webhooks` | 5 minutes | `webhook_service.retry_failed_webhooks()` |
| `cleanup_sessions` | 1 hour | Delete expired session rows |
| `low_balance_alerts` | 6 hours | Alert tenants with balance < threshold |

**Distributed lock:** Only 1 instance processes `process_retries` even with multiple workers (PostgreSQL advisory lock: `pg_try_advisory_lock(12345)`).

---

## Security Checklist

Before shipping any feature, verify:

- [ ] All queries include `WHERE tenant_id = %s`
- [ ] No raw phone numbers in log lines
- [ ] No SQL string concatenation
- [ ] New API endpoints use `@require_api_key` or `@require_login`
- [ ] State-changing routes have CSRF protection (or explicit exemption for webhooks/API)
- [ ] User-provided URLs validated (HTTPS only, not localhost)
- [ ] Sensitive fields (SIP passwords) encrypted before DB write
- [ ] New wallet operations are atomic transactions

---

## Common Pitfalls

| Mistake | Fix |
|---------|-----|
| Querying orders without tenant_id | Always `WHERE tenant_id = %s` |
| Using Python `float` for money | Use `Decimal` from `decimal` module |
| Blocking the Flask request thread with a long SIP call | `call_runner` uses `ThreadPoolExecutor` — never await calls in route handlers |
| Calling `audio.py` functions directly for order TTS | Use `tts_service.generate_order_audio()` wrapper (handles tenant context + caching) |
| Storing API key plaintext | SHA-256 hash only — `hashlib.sha256(key.encode()).hexdigest()` |
| Webhook URL not validated | Use `urllib.parse.urlparse()`, check scheme == 'https' |
| Assuming a call thread can access Flask `g` | `g` is request-scoped — pass `tenant_id` explicitly to thread functions |
| Importing `order_service` at module level in `call_runner.py` | Import inside the function to avoid circular imports |

---

## Deployment

```bash
# Build Docker image
docker build -t ccall:latest .

# Run migrations on production
docker-compose exec app python migrations/run_migrations.py

# Validate setup
docker-compose exec app python scripts/validate_setup.py

# Smoke test
curl https://app.ccall.com.bd/api/v1/health
```

### CI/CD (GitHub Actions)

Push to `main` → lint → test → Docker build → deploy to staging → smoke test → manual approval → deploy to production → Sentry release notification.

See `.github/workflows/deploy.yml`.

---

## Development Roadmap

| Phase | Status | What |
|-------|--------|------|
| Phase 1 | ✅ Done | Schema migration, App factory, Auth, Tenant isolation |
| Phase 2 | ✅ Done | Order service, Call runner ext., Wallet, Webhooks |
| Phase 3 | ✅ Done | Retry engine + settings UI |
| Phase 4 | ✅ Done | Customer panel (dashboard, orders, CDR, wallet) |
| Phase 5 | ✅ Done | Admin panel (tenants, numbers, revenue) |
| Phase 6 | ✅ Done | Monitoring, Docker, CI/CD |
| Phase 7 | ✅ Done | SSLCommerz + bKash payments |
| Phase 8 | ✅ Done | WooCommerce plugin, SMS fallback, recordings, multi-user |

Update `[ ]` to `[x]` as tasks complete.

---

## Questions? Start Here

| Question | Answer |
|----------|--------|
| Where do I add a new API endpoint? | `app/blueprints/api_public/orders.py` — use `@require_api_key` |
| Where does business logic go? | `app/services/` — never in route handlers |
| Where do raw DB queries go? | `app/models/` — one file per table group |
| How do I access current tenant? | `g.tenant_id` (set by `middleware/tenant_context.py`) |
| How do I send a webhook? | `webhook_service.deliver_async(order_id)` |
| How do I deduct wallet? | `wallet_service.deduct(tenant_id, amount, description)` |
| How do I schedule a retry? | `retry_service.schedule_retry(order_id, tenant_id, attempt_no)` |
| How do I select a caller ID? | `number_service.select_caller_id(tenant_id)` |
| How do I generate TTS for an order? | `tts_service.generate_order_audio(tenant_id, order_data)` |
| How do I run a background task? | Use APScheduler job or `threading.Thread(daemon=True)` |

---

*Last updated: June 2026 | Version: 1.0*
