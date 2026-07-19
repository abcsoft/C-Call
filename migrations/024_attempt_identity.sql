-- Migration 024: Durable attempt identity before worker claim.
--
-- Goals
-- -----
-- 1. Add created_at column to order_call_attempts so we know when the attempt
--    was scheduled (distinct from started_at which is set when the call begins).
-- 2. Add UNIQUE(order_id, attempt_no) constraint so a worker cannot create a
--    second attempt row for the same (order, attempt) pair.
-- 3. Add service_heartbeats table for persistent worker/monitor/scheduler heartbeats.
-- 4. Add dispatch_status column to order_call_attempts to distinguish
--    provider startup failures from genuine no-answer outcomes.
--
-- Duplicate detection
-- -------------------
-- If duplicate (order_id, attempt_no) rows already exist the migration raises
-- an error and rolls back. Run scripts/reconcile_call_jobs.py --apply first.
--
-- The unique index is UNIQUE so INSERT ON CONFLICT is usable by application code.

BEGIN;

-- 1. created_at column
ALTER TABLE order_call_attempts
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();

-- 2. dispatch_status column — tracks whether the call was started by the queue
--    system (queued) or reached a telephony outcome (ringing/accepted/no_answer/failed).
--    NULL means pre-created by order transaction, not yet dispatched.
--    'dispatch_failed'  = provider/system failed to start; no dial attempt.
--    'dispatched'       = call was handed off to SIP executor.
ALTER TABLE order_call_attempts
    ADD COLUMN IF NOT EXISTS dispatch_status TEXT;

-- 3. UNIQUE constraint — abort if duplicates exist.
DO $$
DECLARE dup_count INT;
BEGIN
    SELECT COUNT(*) INTO dup_count FROM (
        SELECT order_id, attempt_no
        FROM order_call_attempts
        GROUP BY order_id, attempt_no
        HAVING COUNT(*) > 1
    ) t;
    IF dup_count > 0 THEN
        RAISE EXCEPTION
            'Duplicate (order_id, attempt_no) rows exist in order_call_attempts (% group(s)). '
            'Run: python scripts/reconcile_call_jobs.py --apply  then re-run this migration.',
            dup_count;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uix_order_call_attempts_order_attempt
    ON order_call_attempts (order_id, attempt_no);

-- 4. service_heartbeats — persistent heartbeat storage for health monitoring.
CREATE TABLE IF NOT EXISTS service_heartbeats (
    service_name    TEXT        NOT NULL,
    instance_id     TEXT        NOT NULL,
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT        NOT NULL DEFAULT 'ok',
    metadata        JSONB,
    PRIMARY KEY (service_name, instance_id)
);
COMMENT ON TABLE service_heartbeats IS
    'Last-seen timestamp for each service instance. Updated on every worker/monitor poll tick. '
    'Stale if last_seen_at < now() - interval ''2 minutes''.';

-- 5. add startup_accepted_at / startup_rejected_at / startup_error_code to calls.
--    startup_state was added in migration 023.
ALTER TABLE calls
    ADD COLUMN IF NOT EXISTS startup_accepted_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS startup_rejected_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS startup_error_code   TEXT;

-- Valid startup_state values check (informational — enforced in application too).
ALTER TABLE calls
    DROP CONSTRAINT IF EXISTS calls_startup_state_check;
ALTER TABLE calls
    ADD CONSTRAINT calls_startup_state_check
    CHECK (startup_state IS NULL OR startup_state IN ('pending', 'accepted', 'rejected', 'timeout'));

COMMIT;
