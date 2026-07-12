-- migrations/015_tts_subscription_foundation.sql
-- Subscription-based TTS access foundation.
-- Safe to run multiple times (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS guards).
-- Depends on: 002_saas_layer.sql (plans, tenants, users)

-- ============================================================
-- A. plan_tts_policies
-- ============================================================
CREATE TABLE IF NOT EXISTS plan_tts_policies (
    id                          BIGSERIAL PRIMARY KEY,
    plan_id                     INTEGER NOT NULL UNIQUE REFERENCES plans(id) ON DELETE CASCADE,
    -- credential_policy: which source of TTS credentials this plan allows
    credential_policy           TEXT    NOT NULL DEFAULT 'byok_only'
                                CHECK (credential_policy IN ('byok_only', 'platform_managed', 'hybrid')),
    -- full set of provider IDs accessible to tenants on this plan (BYOK + platform)
    allowed_providers           JSONB   NOT NULL DEFAULT '[]',
    -- providers tenants may use with their own BYOK key
    byok_providers              JSONB   NOT NULL DEFAULT '[]',
    -- providers the platform key covers (empty by default → no platform TTS)
    platform_managed_providers  JSONB   NOT NULL DEFAULT '[]',
    -- NULL = not tracked/unlimited only for BYOK (no platform charge). Must be set for platform_managed.
    monthly_character_quota     BIGINT  CHECK (monthly_character_quota IS NULL OR monthly_character_quota >= 0),
    monthly_audio_seconds_quota BIGINT  CHECK (monthly_audio_seconds_quota IS NULL OR monthly_audio_seconds_quota >= 0),
    -- max preview generations per tenant per calendar day
    daily_preview_limit         INTEGER NOT NULL DEFAULT 10 CHECK (daily_preview_limit >= 0),
    -- what to do when monthly quota exhausted
    extra_usage_policy          TEXT    NOT NULL DEFAULT 'block'
                                CHECK (extra_usage_policy IN ('block')),
    max_concurrent_tts          INTEGER NOT NULL DEFAULT 1 CHECK (max_concurrent_tts >= 1),
    premium_voices_enabled      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE plan_tts_policies IS
    'Per-plan TTS policy: which credential modes, providers, and quotas are allowed.';
COMMENT ON COLUMN plan_tts_policies.credential_policy IS
    'byok_only: tenant must provide own key. platform_managed: use platform key. hybrid: either.';
COMMENT ON COLUMN plan_tts_policies.platform_managed_providers IS
    'Empty array = no platform TTS access. Never populated automatically for existing plans.';

-- ============================================================
-- B. tenants.tts_credential_mode (dedicated column, NOT in settings JSON)
-- ============================================================
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS tts_credential_mode TEXT NOT NULL DEFAULT 'byok'
        CHECK (tts_credential_mode IN ('byok', 'platform_managed'));

COMMENT ON COLUMN tenants.tts_credential_mode IS
    'Active TTS credential mode for this tenant. Must be within plan policy. Default byok — never auto-upgraded.';

-- ============================================================
-- C. platform_secrets
-- ============================================================
CREATE TABLE IF NOT EXISTS platform_secrets (
    id               BIGSERIAL PRIMARY KEY,
    provider         TEXT    NOT NULL UNIQUE
                     CHECK (provider IN ('gemini', 'elevenlabs')),
    encrypted_value  TEXT    NOT NULL,      -- Fernet(PLATFORM_SECRET_ENCRYPTION_KEY)
    last_four        VARCHAR(4),
    status           TEXT    NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'disabled', 'invalid')),
    created_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    last_tested_at   TIMESTAMPTZ,
    last_test_status TEXT    CHECK (last_test_status IN ('ok', 'failed')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE platform_secrets IS
    'Encrypted platform-wide TTS API keys. Super-admin only. encrypted_value never returned to routes.';
COMMENT ON COLUMN platform_secrets.encrypted_value IS
    'Fernet-encrypted with PLATFORM_SECRET_ENCRYPTION_KEY. Never exposed via any API endpoint.';

-- ============================================================
-- D. tenant_tts_usage
-- ============================================================
CREATE TABLE IF NOT EXISTS tenant_tts_usage (
    id                      BIGSERIAL PRIMARY KEY,
    tenant_id               INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    billing_month           DATE    NOT NULL,   -- always first day of month e.g. 2026-07-01
    provider                TEXT    NOT NULL,
    credential_mode         TEXT    NOT NULL,
    characters_used         BIGINT  NOT NULL DEFAULT 0 CHECK (characters_used >= 0),
    audio_seconds           NUMERIC(14,3) NOT NULL DEFAULT 0 CHECK (audio_seconds >= 0),
    preview_requests        INTEGER NOT NULL DEFAULT 0 CHECK (preview_requests >= 0),
    production_requests     INTEGER NOT NULL DEFAULT 0 CHECK (production_requests >= 0),
    failed_requests         INTEGER NOT NULL DEFAULT 0 CHECK (failed_requests >= 0),
    estimated_provider_cost NUMERIC(14,6) NOT NULL DEFAULT 0 CHECK (estimated_provider_cost >= 0),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, billing_month, provider, credential_mode)
);

COMMENT ON TABLE tenant_tts_usage IS
    'Monthly per-tenant TTS usage counters. billing_month = first day of month.';
COMMENT ON COLUMN tenant_tts_usage.billing_month IS
    'Must be the first day of the calendar month: DATE_TRUNC(''month'', now())::DATE';

CREATE INDEX IF NOT EXISTS idx_tenant_tts_usage_tenant_month
    ON tenant_tts_usage (tenant_id, billing_month);

CREATE INDEX IF NOT EXISTS idx_tenant_tts_usage_billing_month
    ON tenant_tts_usage (billing_month);

CREATE INDEX IF NOT EXISTS idx_tenant_tts_usage_provider_month
    ON tenant_tts_usage (provider, billing_month);

-- ============================================================
-- E. Seed conservative plan_tts_policies for all existing plans
-- ============================================================
-- safe BYOK-only defaults: all free providers allowed via BYOK,
-- no platform-managed providers, no quota (NULL = irrelevant for BYOK).
INSERT INTO plan_tts_policies (
    plan_id,
    credential_policy,
    allowed_providers,
    byok_providers,
    platform_managed_providers,
    monthly_character_quota,
    monthly_audio_seconds_quota,
    daily_preview_limit,
    extra_usage_policy,
    max_concurrent_tts,
    premium_voices_enabled
)
SELECT
    p.id,
    'byok_only',
    '["edge", "gtts", "gemini", "elevenlabs", "openvoice"]'::jsonb,
    '["edge", "gtts", "gemini", "elevenlabs", "openvoice"]'::jsonb,
    '[]'::jsonb,   -- zero platform access
    NULL,          -- no platform quota (byok_only plan — irrelevant)
    NULL,
    20,
    'block',
    2,
    FALSE
FROM plans p
WHERE NOT EXISTS (
    SELECT 1 FROM plan_tts_policies WHERE plan_id = p.id
)
ON CONFLICT (plan_id) DO NOTHING;

-- ============================================================
-- F. Safety assertion (will error if violated, preventing silent bad state)
-- ============================================================
DO $$
DECLARE
    bad_tenants INTEGER;
    bad_plans   INTEGER;
BEGIN
    SELECT COUNT(*) INTO bad_tenants FROM tenants WHERE tts_credential_mode != 'byok';
    SELECT COUNT(*) INTO bad_plans   FROM plan_tts_policies
    WHERE credential_policy != 'byok_only'
       OR jsonb_array_length(platform_managed_providers) > 0;

    IF bad_tenants > 0 THEN
        RAISE EXCEPTION 'SAFETY: % tenant(s) not in byok mode after migration', bad_tenants;
    END IF;
    IF bad_plans > 0 THEN
        RAISE EXCEPTION 'SAFETY: % plan(s) have non-byok-only or platform providers after migration', bad_plans;
    END IF;
END $$;
