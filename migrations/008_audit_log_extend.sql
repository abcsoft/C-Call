-- Migration 005 — extend audit_log for generic actor/target tracking.
-- 004_audit_log.sql created the table with admin_user_id/target_tenant_id
-- (impersonation-specific). This generalizes it for any audited action
-- (wallet adjustment, tenant suspend, API key generate/revoke, ...)
-- without breaking existing inserts that still use the original columns.

BEGIN;

ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS actor_user_id INT REFERENCES users(id);
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS target_type   TEXT;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS target_id     INT;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS ip_address    TEXT;
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS details       JSONB DEFAULT '{}';

-- Backfill actor_user_id from the original admin_user_id column so old
-- and new rows both query consistently going forward.
UPDATE audit_log SET actor_user_id = admin_user_id WHERE actor_user_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_audit_log_actor_target ON audit_log (actor_user_id, target_type, target_id, created_at DESC);

COMMIT;
