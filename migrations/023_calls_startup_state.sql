-- Migration 023: Add startup_state to calls table.
--
-- startup_state records whether the SIP executor accepted the call thread.
-- Set to 'accepted' in queue_confirmation_call AFTER start_call() returns
-- a valid call_id. The worker only marks a job completed when startup_state
-- is 'accepted' in the DB — executor submission alone is insufficient.
--
-- Values:
--   NULL         → call created by call_runner but startup not yet confirmed
--   'accepted'   → executor accepted the thread; call is underway
--   'rejected'   → startup failed (set by error handling, not currently used)

BEGIN;

ALTER TABLE calls ADD COLUMN IF NOT EXISTS startup_state TEXT;

COMMENT ON COLUMN calls.startup_state IS
    'Set to ''accepted'' once SIP executor confirmed thread started. '
    'NULL means startup not yet confirmed by the queue worker path.';

COMMIT;
