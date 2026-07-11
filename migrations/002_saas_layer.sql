-- Migration 002 — SaaS layer (multi-tenant platform)
-- Depends on 001_initial_schema.sql (app_settings, flows, flow_nodes,
-- flow_transitions, contacts, calls, call_events, cdr_records, tts_cache
-- already exist — none of those are recreated here).
--
-- Adds: 17 new tables, tenant_id/order_id columns on existing tables,
-- tenant-isolation indexes, and seed data (plans + super admin user).
-- Idempotent — IF NOT EXISTS everywhere, safe to re-run.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================================
-- 1. NEW TABLES
-- ============================================================================

-- Subscription plans (free / starter / pro / enterprise). Referenced by
-- tenants.plan_id. Seeded below.
CREATE TABLE IF NOT EXISTS plans (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL,
    price_monthly       NUMERIC(10,2) DEFAULT 0,
    included_minutes    INT DEFAULT 0,
    max_flows           INT DEFAULT 1,
    max_users           INT DEFAULT 1,
    feature_flags       JSONB DEFAULT '{}',
    created_at          TIMESTAMPTZ DEFAULT now()
);
COMMENT ON TABLE plans IS 'Subscription tiers (free/starter/pro/enterprise) — feature_flags gates sms_fallback, white_label, etc.';

-- Merchant business accounts. uuid is the public-facing identifier;
-- id (serial) is the internal FK target everywhere else.
CREATE TABLE IF NOT EXISTS tenants (
    id              SERIAL PRIMARY KEY,
    uuid            UUID DEFAULT gen_random_uuid() UNIQUE NOT NULL,
    business_name   TEXT NOT NULL,
    email           TEXT UNIQUE NOT NULL,
    subdomain       TEXT UNIQUE,
    plan_id         INT REFERENCES plans(id),
    status          TEXT NOT NULL DEFAULT 'trial',  -- active | suspended | trial | cancelled
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    settings        JSONB DEFAULT '{}'              -- TTS provider, voice, dial format, DND
);
COMMENT ON TABLE tenants IS 'Merchant business accounts — the root of multi-tenant isolation. Every tenant-scoped table FKs here.';

-- Login accounts. tenant_id is nullable: super_admin rows have
-- tenant_id = NULL (platform-wide access, not tied to a merchant) — see
-- CLAUDE.md Role Hierarchy. All other roles (admin/agent/viewer) require
-- a tenant_id.
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    tenant_id       INT REFERENCES tenants(id) ON DELETE CASCADE,
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,                  -- bcrypt cost 12
    role            TEXT NOT NULL DEFAULT 'admin',  -- super_admin | admin | agent | viewer
    email_verified  BOOLEAN DEFAULT false,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),

    CONSTRAINT users_super_admin_no_tenant
        CHECK (role != 'super_admin' OR tenant_id IS NULL)
);
COMMENT ON TABLE users IS 'Login accounts. tenant_id IS NULL only for role=super_admin (platform operator).';

-- E-commerce orders received via the public API. One row per merchant
-- order; UNIQUE(tenant_id, external_order_id) makes order creation
-- idempotent — a duplicate submission returns the existing row.
CREATE TABLE IF NOT EXISTS orders (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INT NOT NULL REFERENCES tenants(id),
    external_order_id   TEXT,
    phone               TEXT NOT NULL,              -- E.164
    customer_name       TEXT,
    amount              NUMERIC(12,2),
    order_status        TEXT NOT NULL DEFAULT 'NEW',
                        -- NEW | CALL_QUEUED | CALLING | CONFIRMED | CANCELLED
                        -- NO_ANSWER | RETRY_SCHEDULED | UNREACHABLE | FAILED
    attempt_count       INT DEFAULT 0,
    max_attempts        INT DEFAULT 3,
    next_retry_at       TIMESTAMPTZ,
    confirmed_at        TIMESTAMPTZ,
    caller_id           TEXT,                       -- phone number used as CLI
    webhook_status      TEXT DEFAULT 'pending',     -- pending | sent | failed
    webhook_url         TEXT,                       -- snapshot at order creation
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),

    UNIQUE (tenant_id, external_order_id)
);
COMMENT ON TABLE orders IS 'E-commerce orders received via POST /api/v1/orders. UNIQUE(tenant_id, external_order_id) provides submission idempotency.';

-- One row per call attempt against an order (initial call + each retry).
CREATE TABLE IF NOT EXISTS order_call_attempts (
    id              SERIAL PRIMARY KEY,
    order_id        INT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    tenant_id       INT NOT NULL REFERENCES tenants(id),
    call_id         INT REFERENCES calls(id),
    attempt_no      INT NOT NULL,
    result          TEXT,                           -- confirmed | cancelled | no_answer | busy | failed | no_input
    dtmf_digit      TEXT,                           -- '1', '2', or NULL
    talk_seconds    INT DEFAULT 0,
    charge          NUMERIC(10,4) DEFAULT 0,
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ
);
COMMENT ON TABLE order_call_attempts IS 'One row per call attempt (initial + retries) against an order, linked to the underlying calls row.';

-- Pre-paid balance, one per tenant. balance CHECK >= 0 — deduction below
-- the floor must fail at the DB level, not just in application code.
CREATE TABLE IF NOT EXISTS wallets (
    id                      SERIAL PRIMARY KEY,
    tenant_id               INT NOT NULL UNIQUE REFERENCES tenants(id),
    balance                 NUMERIC(14,4) DEFAULT 0 CHECK (balance >= 0),
    currency                TEXT DEFAULT 'BDT',
    low_balance_threshold   NUMERIC(14,4) DEFAULT 50,
    updated_at              TIMESTAMPTZ DEFAULT now()
);
COMMENT ON TABLE wallets IS 'Pre-paid balance per tenant. balance CHECK >= 0 enforces no-overdraft at the DB level.';

-- Append-only ledger of every credit/debit. Never updated/deleted —
-- balance_after is a point-in-time snapshot for audit/reconciliation.
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id              SERIAL PRIMARY KEY,
    wallet_id       INT NOT NULL REFERENCES wallets(id),
    tenant_id       INT NOT NULL REFERENCES tenants(id),
    type            TEXT NOT NULL,                  -- credit | debit | refund | adjustment
    amount          NUMERIC(14,4) NOT NULL,
    balance_after   NUMERIC(14,4) NOT NULL,
    description     TEXT,
    ref_id          TEXT,                           -- payment gateway ref or call ID
    created_at      TIMESTAMPTZ DEFAULT now()
);
COMMENT ON TABLE wallet_transactions IS 'Append-only ledger of every wallet credit/debit/refund/adjustment — never updated or deleted.';

-- Payment gateway sessions (SSLCommerz / bKash / Stripe). status moves
-- pending -> completed/failed/refunded as the gateway callback resolves.
CREATE TABLE IF NOT EXISTS payment_intents (
    id              SERIAL PRIMARY KEY,
    tenant_id       INT NOT NULL REFERENCES tenants(id),
    gateway         TEXT NOT NULL,                  -- sslcommerz | bkash | stripe
    gateway_ref     TEXT,
    amount          NUMERIC(14,4) NOT NULL,
    currency        TEXT DEFAULT 'BDT',
    status          TEXT DEFAULT 'pending',         -- pending | completed | failed | refunded
    payload         JSONB,                          -- gateway-specific data
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
COMMENT ON TABLE payment_intents IS 'Payment gateway sessions for wallet recharge — SSLCommerz/bKash/Stripe.';

-- SIP trunk credentials. sip_password is application-layer AES-256
-- encrypted before insert — this column stores ciphertext, never plaintext.
CREATE TABLE IF NOT EXISTS sip_providers (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    sip_domain      TEXT NOT NULL,
    sip_port        INT DEFAULT 5060,
    sip_user        TEXT NOT NULL,
    sip_password    TEXT NOT NULL,                  -- AES-256 encrypted at rest
    status          TEXT DEFAULT 'active',
    created_at      TIMESTAMPTZ DEFAULT now()
);
COMMENT ON TABLE sip_providers IS 'SIP trunk credentials. sip_password is AES-256 ciphertext, encrypted/decrypted in the application layer — never stored plaintext.';

-- Pool of caller-ID numbers, shared or dedicated, backed by a SIP provider.
CREATE TABLE IF NOT EXISTS phone_numbers (
    id              SERIAL PRIMARY KEY,
    number          TEXT UNIQUE NOT NULL,
    provider_id     INT NOT NULL REFERENCES sip_providers(id),
    number_type     TEXT NOT NULL DEFAULT 'shared',  -- shared | dedicated
    status          TEXT NOT NULL DEFAULT 'active',  -- active | inactive | suspended
    monthly_cost    NUMERIC(10,2) DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now()
);
COMMENT ON TABLE phone_numbers IS 'Pool of caller-ID (CLI) numbers — shared (round-robin) or dedicated (assigned to one tenant).';

-- Which tenant currently holds which dedicated number.
CREATE TABLE IF NOT EXISTS customer_number_assignments (
    id              SERIAL PRIMARY KEY,
    tenant_id       INT NOT NULL REFERENCES tenants(id),
    phone_number_id INT NOT NULL REFERENCES phone_numbers(id),
    is_primary      BOOLEAN DEFAULT true,
    assigned_at     TIMESTAMPTZ DEFAULT now(),
    unassigned_at   TIMESTAMPTZ,

    UNIQUE (phone_number_id, tenant_id)
);
COMMENT ON TABLE customer_number_assignments IS 'Tenant <-> dedicated phone_number assignment history (unassigned_at NULL = currently active).';

-- Per-tenant call rate override. Falls back to a platform default in
-- rate_service when no row exists for a tenant.
CREATE TABLE IF NOT EXISTS customer_rates (
    id              SERIAL PRIMARY KEY,
    tenant_id       INT NOT NULL UNIQUE REFERENCES tenants(id),
    rate_per_min    NUMERIC(8,4) DEFAULT 0.80,
    rate_per_call   NUMERIC(8,4) DEFAULT 0,          -- if > 0, charged per call not per min
    min_charge      NUMERIC(8,4) DEFAULT 0.80,
    updated_at      TIMESTAMPTZ DEFAULT now()
);
COMMENT ON TABLE customer_rates IS 'Per-tenant call-rate override (rate_per_min or flat rate_per_call). No row = platform default rate applies.';

-- Retry schedule per attempt number per tenant, including the DND window.
CREATE TABLE IF NOT EXISTS retry_rules (
    id              SERIAL PRIMARY KEY,
    tenant_id       INT NOT NULL REFERENCES tenants(id),
    attempt_no      INT NOT NULL,
    delay_minutes   INT NOT NULL DEFAULT 30,
    retry_on        TEXT[] DEFAULT ARRAY['no_answer','busy'],
    dnd_start       TIME DEFAULT '21:00',
    dnd_end         TIME DEFAULT '09:00',
    max_attempts    INT DEFAULT 3,
    is_enabled      BOOLEAN DEFAULT true,

    UNIQUE (tenant_id, attempt_no)
);
COMMENT ON TABLE retry_rules IS 'Retry schedule per (tenant_id, attempt_no) — delay, which results trigger a retry, and the DND window (Asia/Dhaka by default).';

-- REST API credentials. key_hash is SHA-256 of the raw key — the raw key
-- is shown to the user exactly once at creation time and never stored.
CREATE TABLE IF NOT EXISTS api_keys (
    id              SERIAL PRIMARY KEY,
    tenant_id       INT NOT NULL REFERENCES tenants(id),
    name            TEXT DEFAULT 'Default',
    key_hash        TEXT NOT NULL UNIQUE,            -- SHA-256 of the actual key
    key_prefix      TEXT NOT NULL,                   -- first 8 chars for display: "cc_live_"
    last_used_at    TIMESTAMPTZ,
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now()
);
COMMENT ON TABLE api_keys IS 'REST API credentials. key_hash = SHA-256(raw key) — the raw key itself is never stored, only shown once on creation.';

-- Outbound webhook delivery history (one row per attempt, not per event).
CREATE TABLE IF NOT EXISTS webhook_logs (
    id              SERIAL PRIMARY KEY,
    tenant_id       INT NOT NULL REFERENCES tenants(id),
    order_id        INT REFERENCES orders(id),
    url             TEXT NOT NULL,
    payload         JSONB NOT NULL,
    status_code     INT,
    response_body   TEXT,
    attempt_no      INT DEFAULT 1,
    sent_at         TIMESTAMPTZ DEFAULT now(),
    success         BOOLEAN DEFAULT false
);
COMMENT ON TABLE webhook_logs IS 'Outbound webhook delivery attempts to the merchant webhook_url — one row per attempt (up to 3x retry).';

-- Server-side session store (Flask-Session backend, mirrored in Redis for
-- fast lookup; this table is the durable copy / audit trail).
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,                   -- session token (random 32 bytes hex)
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id   INT REFERENCES tenants(id),
    data        JSONB DEFAULT '{}',
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);
COMMENT ON TABLE sessions IS 'Server-side session store — durable copy alongside the Redis-backed Flask-Session cache.';

-- Email verification + password reset tokens, single-use (used_at set on
-- consumption).
CREATE TABLE IF NOT EXISTS email_verifications (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token       TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ
);
COMMENT ON TABLE email_verifications IS 'Email verification / password reset tokens — single-use, used_at set on consumption.';

-- ============================================================================
-- 2. ALTER EXISTING TABLES — add tenant_id / order_id / rate columns
-- ============================================================================

ALTER TABLE calls       ADD COLUMN IF NOT EXISTS tenant_id INT REFERENCES tenants(id);
ALTER TABLE calls       ADD COLUMN IF NOT EXISTS order_id  INT REFERENCES orders(id);
ALTER TABLE calls       ADD COLUMN IF NOT EXISTS caller_id TEXT;
ALTER TABLE flows       ADD COLUMN IF NOT EXISTS tenant_id INT REFERENCES tenants(id);
ALTER TABLE contacts    ADD COLUMN IF NOT EXISTS tenant_id INT REFERENCES tenants(id);
ALTER TABLE tts_cache   ADD COLUMN IF NOT EXISTS tenant_id INT REFERENCES tenants(id);
ALTER TABLE cdr_records ADD COLUMN IF NOT EXISTS tenant_id INT REFERENCES tenants(id);
ALTER TABLE cdr_records ADD COLUMN IF NOT EXISTS rate      NUMERIC(8,4);
ALTER TABLE cdr_records ADD COLUMN IF NOT EXISTS charge    NUMERIC(10,4);

-- ============================================================================
-- 3. INDEXES
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_orders_tenant_status ON orders (tenant_id, order_status);
CREATE INDEX IF NOT EXISTS idx_orders_created       ON orders (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_next_retry    ON orders (next_retry_at)
                                                     WHERE order_status = 'RETRY_SCHEDULED';

CREATE INDEX IF NOT EXISTS idx_order_call_attempts_order ON order_call_attempts (order_id);

CREATE INDEX IF NOT EXISTS idx_wallet_tx_tenant ON wallet_transactions (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_webhook_logs_tenant ON webhook_logs (tenant_id, sent_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (key_hash);

-- Tenant isolation on existing tables (calls/flows/contacts already
-- carried a non-tenant index from 001; these are additive)
CREATE INDEX IF NOT EXISTS idx_calls_tenant    ON calls    (tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_flows_tenant    ON flows    (tenant_id);
CREATE INDEX IF NOT EXISTS idx_contacts_tenant ON contacts (tenant_id);

-- ============================================================================
-- 4. SEED DATA
-- ============================================================================

-- 3 plans. ON CONFLICT keyed on name so re-running this migration doesn't
-- duplicate rows (plans has no UNIQUE(name) constraint by design — it's
-- an admin-managed table — so we upsert via a NOT EXISTS guard instead).
INSERT INTO plans (name, price_monthly, included_minutes, max_flows, max_users, feature_flags)
SELECT 'free', 0, 50, 1, 1, '{"sms_fallback": false, "white_label": false}'
WHERE NOT EXISTS (SELECT 1 FROM plans WHERE name = 'free');

INSERT INTO plans (name, price_monthly, included_minutes, max_flows, max_users, feature_flags)
SELECT 'starter', 990, 500, 3, 2, '{"sms_fallback": false, "white_label": false}'
WHERE NOT EXISTS (SELECT 1 FROM plans WHERE name = 'starter');

INSERT INTO plans (name, price_monthly, included_minutes, max_flows, max_users, feature_flags)
SELECT 'pro', 2490, 5000, 10, 5, '{"sms_fallback": true, "white_label": true}'
WHERE NOT EXISTS (SELECT 1 FROM plans WHERE name = 'pro');

-- 1 super admin user. tenant_id NULL (platform-wide role — see users
-- table comment + CHECK constraint above).
--
-- *** PLACEHOLDER PASSWORD — CHANGE IMMEDIATELY AFTER FIRST LOGIN ***
-- Hash below is bcrypt(cost=12) of the literal string "ChangeMe123!".
-- Generated with:
--   python -c "import bcrypt; print(bcrypt.hashpw(b'ChangeMe123!', bcrypt.gensalt(rounds=12)).decode())"
INSERT INTO users (tenant_id, email, password_hash, role, email_verified)
SELECT NULL, 'admin@ccall.com.bd',
       '$2b$12$D1WTItiYm0IzlTlV81a1fuGY8XocIzajNe8RMhEa/MclQ63HsvKuu',
       'super_admin', true
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'admin@ccall.com.bd');

COMMIT;
