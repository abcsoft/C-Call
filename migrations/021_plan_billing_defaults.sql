-- migrations/021_plan_billing_defaults.sql
-- Phase: Plan Default Billing Fields + Plan Features + Number Entitlement + Rate Snapshot
-- Safe to run multiple times (ADD COLUMN IF NOT EXISTS guards).
-- Depends on: 002_saas_layer.sql (plans, tenants, customer_rates, calls)

-- ============================================================
-- A. Plan-level default billing fields
--    NULL means "not set by this plan — fall through to platform default"
-- ============================================================
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS default_rate_per_min      NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS default_rate_per_call     NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS default_min_charge        NUMERIC(8,4),
    ADD COLUMN IF NOT EXISTS default_billing_pulse_seconds       INT,
    ADD COLUMN IF NOT EXISTS default_minimum_billable_seconds    INT,
    ADD COLUMN IF NOT EXISTS default_charge_failed_calls         BOOLEAN,
    ADD COLUMN IF NOT EXISTS number_entitlement        TEXT NOT NULL DEFAULT 'shared_only'
        CHECK (number_entitlement IN ('shared_only', 'dedicated_allowed', 'dedicated_included')),
    ADD COLUMN IF NOT EXISTS included_dedicated_numbers INT NOT NULL DEFAULT 0
        CHECK (included_dedicated_numbers >= 0);

-- Backfill from the hardcoded name-based rates in rate_service.py.
-- Only sets values where they are currently NULL (idempotent).
UPDATE plans SET
    default_rate_per_min              = 0.80,
    default_rate_per_call             = 0.00,
    default_min_charge                = 0.80,
    default_billing_pulse_seconds     = 60,
    default_minimum_billable_seconds  = 60,
    default_charge_failed_calls       = false
WHERE LOWER(name) = 'free'
  AND default_rate_per_min IS NULL;

UPDATE plans SET
    default_rate_per_min              = 0.70,
    default_rate_per_call             = 0.00,
    default_min_charge                = 0.70,
    default_billing_pulse_seconds     = 60,
    default_minimum_billable_seconds  = 60,
    default_charge_failed_calls       = false
WHERE LOWER(name) = 'starter'
  AND default_rate_per_min IS NULL;

UPDATE plans SET
    default_rate_per_min              = 0.60,
    default_rate_per_call             = 0.00,
    default_min_charge                = 0.60,
    default_billing_pulse_seconds     = 60,
    default_minimum_billable_seconds  = 60,
    default_charge_failed_calls       = false
WHERE LOWER(name) = 'pro'
  AND default_rate_per_min IS NULL;

-- Any other plans (enterprise, custom, etc.) fall back to platform default on NULL.
-- Do NOT force a value here — platform default 0.80 applies at resolver time.

-- ============================================================
-- B. Rate snapshot on calls — stores resolved billing params at dispatch time.
--    NULL for calls before this migration (billing_service falls back to live rate).
-- ============================================================
ALTER TABLE calls
    ADD COLUMN IF NOT EXISTS rate_snapshot JSONB;

-- ============================================================
-- C. plan_features — dynamic key/value feature registry per plan.
--    Replaces hard-coded feature_flags JSONB for structured, per-feature control.
-- ============================================================
CREATE TABLE IF NOT EXISTS plan_features (
    id            BIGSERIAL PRIMARY KEY,
    plan_id       INTEGER   NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    feature_key   TEXT      NOT NULL,
    feature_label TEXT      NOT NULL,
    feature_type  TEXT      NOT NULL DEFAULT 'boolean'
        CHECK (feature_type IN ('boolean', 'integer', 'text', 'decimal')),
    feature_value TEXT      NOT NULL DEFAULT 'true',
    is_public     BOOLEAN   NOT NULL DEFAULT TRUE,
    is_enabled    BOOLEAN   NOT NULL DEFAULT TRUE,
    sort_order    INTEGER   NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (plan_id, feature_key)
);

COMMENT ON TABLE plan_features IS
    'Dynamic per-plan feature registry. is_public=TRUE features are shown to merchants on plan cards.';

CREATE INDEX IF NOT EXISTS plan_features_plan_id_idx ON plan_features (plan_id);
CREATE INDEX IF NOT EXISTS plan_features_public_idx  ON plan_features (plan_id, is_public, sort_order)
    WHERE is_public = TRUE AND is_enabled = TRUE;
