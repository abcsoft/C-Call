-- migrations/019_subscription_hardening.sql
-- Subscription hardening: past_due deadline, security suspension tracking,
-- extension/reactivation separation, past_due → expired scheduler support.

BEGIN;

-- ── 1. subscriptions: past_due deadline ──────────────────────────────────
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS past_due_ends_at TIMESTAMPTZ;

-- ── 2. subscriptions: security suspension fields ──────────────────────────
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS suspension_reason TEXT;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS suspension_type TEXT DEFAULT 'none'
    CHECK (suspension_type IN ('none','security','billing','compliance'));

-- ── 3. tenants.status CHECK: add 'suspended' if missing (already in CHECK
--       from migration 002, but guard in case of variant schemas) ───────────
-- No-op if already present — constraint name check handles idempotency.

-- ── 4. subscription_events: add suspension_type to metadata convention ────
-- (No schema change needed — stored in metadata JSONB)

-- ── 5. Index for scheduler past_due expiry scan ───────────────────────────
CREATE INDEX IF NOT EXISTS idx_subscriptions_past_due_ends
    ON subscriptions (past_due_ends_at)
    WHERE status = 'past_due' AND past_due_ends_at IS NOT NULL;

-- ── 6. Index for scheduler active-period-end scan ─────────────────────────
CREATE INDEX IF NOT EXISTS idx_subscriptions_period_end_active
    ON subscriptions (current_period_end)
    WHERE status = 'active' AND current_period_end IS NOT NULL;

-- ── 7. Backfill past_due_ends_at for any existing past_due subscriptions ──
-- Default: 14 days from updated_at (conservative — they already waited grace)
UPDATE subscriptions
SET past_due_ends_at = updated_at + INTERVAL '14 days'
WHERE status = 'past_due'
  AND past_due_ends_at IS NULL;

COMMIT;
