-- migrations/017_tts_usage_reservations_and_overage.sql
-- Strict TTS quota reservation, overage billing, and idempotency.
-- Forward-only. Safe to run on a database that already has migration 016.
-- DO NOT edit migrations 001-016.

-- ============================================================
-- A. Extend extra_usage_policy CHECK to allow charge_wallet
--    Also add overage pricing columns to plan_tts_policies.
-- ============================================================

-- 1. Drop the old CHECK constraint on extra_usage_policy (only 'block' was allowed).
ALTER TABLE plan_tts_policies
    DROP CONSTRAINT IF EXISTS plan_tts_policies_extra_usage_policy_check;

-- 2. Re-add the constraint with the extended set.
ALTER TABLE plan_tts_policies
    ADD CONSTRAINT plan_tts_policies_extra_usage_policy_check
    CHECK (extra_usage_policy IN ('block', 'charge_wallet'));

-- 3. Overage pricing columns (safe ADD COLUMN IF NOT EXISTS).
ALTER TABLE plan_tts_policies
    ADD COLUMN IF NOT EXISTS overage_rate_per_1000_chars NUMERIC(14,6) NOT NULL DEFAULT 0
        CHECK (overage_rate_per_1000_chars >= 0),
    ADD COLUMN IF NOT EXISTS minimum_overage_charge      NUMERIC(14,6) NOT NULL DEFAULT 0
        CHECK (minimum_overage_charge >= 0),
    ADD COLUMN IF NOT EXISTS allow_preview_overage       BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN plan_tts_policies.extra_usage_policy IS
    'block: deny when quota exhausted. charge_wallet: debit wallet at overage_rate_per_1000_chars.';
COMMENT ON COLUMN plan_tts_policies.overage_rate_per_1000_chars IS
    'BDT charged per 1 000 overage characters. Must be > 0 if extra_usage_policy = charge_wallet.';
COMMENT ON COLUMN plan_tts_policies.minimum_overage_charge IS
    'Minimum wallet charge per overage event. Applied only when charge_wallet policy is active.';
COMMENT ON COLUMN plan_tts_policies.allow_preview_overage IS
    'If TRUE, preview requests may generate overage wallet charges. Default FALSE.';

-- 4. Integrity: charge_wallet plans must have a positive rate.
--    Existing plans are all 'block' so no rows are violated.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM plan_tts_policies
        WHERE extra_usage_policy = 'charge_wallet'
          AND overage_rate_per_1000_chars <= 0
    ) THEN
        RAISE EXCEPTION
            'Integrity error: charge_wallet plan_tts_policies rows must have '
            'overage_rate_per_1000_chars > 0';
    END IF;
END $$;

-- ============================================================
-- B. tts_usage_reservations
--    One row per reservation attempt. Provides strict quota
--    pre-reservation and per-call idempotency.
-- ============================================================
CREATE TABLE IF NOT EXISTS tts_usage_reservations (
    id                          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider                    TEXT         NOT NULL,
    credential_mode             TEXT         NOT NULL
                                CHECK (credential_mode IN ('byok', 'platform_managed')),
    purpose                     TEXT         NOT NULL
                                CHECK (purpose IN ('preview', 'production')),
    idempotency_key             TEXT         NOT NULL,

    -- Billing month (first day of month, UTC).
    billing_month               DATE         NOT NULL,

    -- Characters requested at reservation time (based on rendered text).
    characters_reserved         BIGINT       NOT NULL CHECK (characters_reserved >= 0),

    -- Split: how many characters come from included quota vs overage.
    quota_characters_reserved   BIGINT       NOT NULL DEFAULT 0 CHECK (quota_characters_reserved >= 0),
    overage_characters_reserved BIGINT       NOT NULL DEFAULT 0 CHECK (overage_characters_reserved >= 0),

    -- Estimated wallet amount reserved for overage (Decimal precision).
    estimated_overage_amount    NUMERIC(14,6) NOT NULL DEFAULT 0 CHECK (estimated_overage_amount >= 0),

    -- Filled in on commit.
    actual_characters           BIGINT       CHECK (actual_characters >= 0),
    actual_audio_seconds        NUMERIC(14,3) CHECK (actual_audio_seconds >= 0),
    actual_overage_amount       NUMERIC(14,6) CHECK (actual_overage_amount >= 0),

    -- Lifecycle status.
    status                      TEXT         NOT NULL DEFAULT 'reserved'
                                CHECK (status IN ('reserved', 'committed', 'released', 'failed', 'expired')),

    -- Wallet transaction linked on overage debit (NULL if no wallet charge).
    wallet_transaction_id       INTEGER      NULL REFERENCES wallet_transactions(id) ON DELETE SET NULL,

    -- Structured error code (no provider secret, no raw text).
    error_code                  TEXT         NULL,

    -- Timestamps.
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    committed_at                TIMESTAMPTZ  NULL,
    released_at                 TIMESTAMPTZ  NULL,
    expires_at                  TIMESTAMPTZ  NOT NULL DEFAULT (now() + INTERVAL '10 minutes'),
    updated_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- One reservation per (tenant, idempotency_key). Cross-tenant same key is allowed.
    UNIQUE (tenant_id, idempotency_key)
);

COMMENT ON TABLE tts_usage_reservations IS
    'Pre-call TTS quota and wallet reservation. One row per idempotency key. '
    'Provides strict concurrency-safe quota enforcement.';
COMMENT ON COLUMN tts_usage_reservations.idempotency_key IS
    'Production: SHA256(tenant_id|order_id|script_hash|provider|purpose|attempt). '
    'Preview: UUID per request.';
COMMENT ON COLUMN tts_usage_reservations.billing_month IS
    'First day of the billing month (UTC). Matches tenant_tts_usage.billing_month.';
COMMENT ON COLUMN tts_usage_reservations.estimated_overage_amount IS
    'Decimal amount held/debited from wallet for overage characters. 0 for block/BYOK plans.';
COMMENT ON COLUMN tts_usage_reservations.wallet_transaction_id IS
    'wallet_transactions.id for the overage debit. NULL if no wallet charge was made.';

-- ── wallet_transactions.ref_id uniqueness (needed for idempotent refund inserts) ─
-- Only TTS overage refund rows need uniqueness; other rows may have NULL ref_id.
CREATE UNIQUE INDEX IF NOT EXISTS idx_wallet_tx_ref_id_unique
    ON wallet_transactions (ref_id)
    WHERE ref_id IS NOT NULL;

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_tts_reservations_tenant_month_status
    ON tts_usage_reservations (tenant_id, billing_month, status);

CREATE INDEX IF NOT EXISTS idx_tts_reservations_expires_status
    ON tts_usage_reservations (expires_at, status)
    WHERE status = 'reserved';

CREATE INDEX IF NOT EXISTS idx_tts_reservations_provider_month
    ON tts_usage_reservations (provider, billing_month);

CREATE INDEX IF NOT EXISTS idx_tts_reservations_idempotency
    ON tts_usage_reservations (idempotency_key);

CREATE INDEX IF NOT EXISTS idx_tts_reservations_tenant_status
    ON tts_usage_reservations (tenant_id, status);

-- ============================================================
-- C. Add overage_characters + wallet_overage_amount to tenant_tts_usage
--    for committed monthly totals.
-- ============================================================
ALTER TABLE tenant_tts_usage
    ADD COLUMN IF NOT EXISTS overage_characters    BIGINT        NOT NULL DEFAULT 0
        CHECK (overage_characters >= 0),
    ADD COLUMN IF NOT EXISTS wallet_overage_amount NUMERIC(14,6) NOT NULL DEFAULT 0
        CHECK (wallet_overage_amount >= 0);

COMMENT ON COLUMN tenant_tts_usage.overage_characters IS
    'Total committed overage characters this billing month.';
COMMENT ON COLUMN tenant_tts_usage.wallet_overage_amount IS
    'Total wallet amount charged for overage this billing month.';

-- ============================================================
-- D. updated_at trigger helper (reuse pattern from earlier migrations)
-- ============================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_tts_reservations_updated_at ON tts_usage_reservations;
CREATE TRIGGER trg_tts_reservations_updated_at
    BEFORE UPDATE ON tts_usage_reservations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- E. Integrity assertion
-- ============================================================
DO $$
DECLARE
    v_count INTEGER;
BEGIN
    -- Verify tts_usage_reservations table was created
    SELECT COUNT(*) INTO v_count FROM information_schema.tables
    WHERE table_schema = 'public' AND table_name = 'tts_usage_reservations';
    IF v_count = 0 THEN
        RAISE EXCEPTION 'tts_usage_reservations table not found after migration';
    END IF;

    -- Verify overage columns exist on tenant_tts_usage
    SELECT COUNT(*) INTO v_count FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'tenant_tts_usage'
      AND column_name IN ('overage_characters', 'wallet_overage_amount');
    IF v_count < 2 THEN
        RAISE EXCEPTION 'overage columns not added to tenant_tts_usage';
    END IF;
END $$;
