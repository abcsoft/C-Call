-- migrations/028_v2_security_and_reliability.sql
-- C-Call V2 Production Security & Reliability Foundation:
--   1. API key scopes, quotas, expiration, soft revocation & rotation lineage
--   2. Outbound compliance: opt-out/DND blocklist, consent tracking, daily spend limit, prefix rules
--   3. Calling reliability: campaign/tenant pause, provider channel limits, granular call_state tracking

BEGIN;

-- ── 1. API Keys: scopes, quotas, expiration, revocation ──────────────────────
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS scopes           TEXT[]      NOT NULL DEFAULT ARRAY['orders:read', 'orders:write'],
    ADD COLUMN IF NOT EXISTS daily_quota      INTEGER     NOT NULL DEFAULT 10000,
    ADD COLUMN IF NOT EXISTS requests_today   INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS quota_reset_date DATE        NOT NULL DEFAULT CURRENT_DATE,
    ADD COLUMN IF NOT EXISTS expires_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS revoked_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rotated_from_id  INTEGER     REFERENCES api_keys(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_api_keys_active_hash
    ON api_keys (key_hash)
    WHERE is_active = true AND revoked_at IS NULL;

-- ── 2. Outbound Compliance: opt-out numbers, consent, spend limits, dial rules
CREATE TABLE IF NOT EXISTS opt_out_numbers (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   INTEGER     NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    phone       TEXT        NOT NULL,
    reason      TEXT        NOT NULL DEFAULT 'customer_opt_out',
    source      TEXT        NOT NULL DEFAULT 'api',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, phone)
);

CREATE INDEX IF NOT EXISTS idx_opt_out_tenant_phone
    ON opt_out_numbers (tenant_id, phone);

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS consent_recorded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS consent_source      TEXT;

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS daily_spend_limit     NUMERIC(12,4),
    ADD COLUMN IF NOT EXISTS allowed_dial_prefixes TEXT[],
    ADD COLUMN IF NOT EXISTS is_dialing_paused     BOOLEAN NOT NULL DEFAULT false;

-- ── 3. Calling Reliability: provider channel limits & granular call states ────
ALTER TABLE sip_providers
    ADD COLUMN IF NOT EXISTS max_channels INTEGER NOT NULL DEFAULT 10;

ALTER TABLE call_jobs
    ADD COLUMN IF NOT EXISTS provider_id     INTEGER REFERENCES sip_providers(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS call_state      TEXT    NOT NULL DEFAULT 'queued',
    ADD COLUMN IF NOT EXISTS campaign_paused BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE call_jobs DROP CONSTRAINT IF EXISTS chk_call_jobs_call_state;
ALTER TABLE call_jobs ADD CONSTRAINT chk_call_jobs_call_state
    CHECK (call_state IN (
        'queued', 'preparing', 'dialing', 'ringing', 'answered',
        'completed', 'failed', 'canceled', 'busy', 'no_answer'
    ));

ALTER TABLE calls DROP CONSTRAINT IF EXISTS chk_calls_status;
ALTER TABLE calls ADD CONSTRAINT chk_calls_status
    CHECK (status IN (
        'queued', 'initiated', 'preparing', 'dialing', 'calling', 'ringing', 'answered',
        'completed', 'failed', 'no_answer', 'busy', 'canceled'
    ));

COMMIT;
