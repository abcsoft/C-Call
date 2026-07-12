-- migrations/016_plan_archive.sql
-- Add archived_at column to plans so archived plans cannot be assigned
-- but still retain TTS policies and usage history.
-- Forward-only, safe to re-run.

ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ DEFAULT NULL;

COMMENT ON COLUMN plans.archived_at IS
    'NULL = active plan. Non-null = archived timestamp. Archived plans cannot be assigned to new tenants.';

-- Index for quick "list active plans" queries
CREATE INDEX IF NOT EXISTS idx_plans_archived_at
    ON plans (archived_at)
    WHERE archived_at IS NULL;
