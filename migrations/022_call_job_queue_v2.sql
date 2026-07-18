-- Migration 022: Extend call_jobs for full durable dispatch support.
--
-- Adds missing fields required for:
--   - DND-aware available_at scheduling
--   - Dispatch attempt tracking with bounded backoff
--   - Structured error classification
--   - Crash-after-call duplicate guard (call_id, order_call_attempt_id)
--   - Job type extension
--   - Idempotency unique constraint per (order_id, attempt_no)
--
-- All new columns are nullable or have defaults so existing production rows
-- remain valid with no data migration required.
--
-- STATUS VOCABULARY (preserved + extended):
--   queued      → worker polls and sees it (equivalent to 'pending')
--   leased      → atomically claimed by one worker
--   retry_wait  → dispatch failed temporarily; available_at = future  [NEW]
--   processing  → legacy intermediate state (unused by new worker)
--   completed   → dispatch accepted; call_id set; queue work done
--   failed      → permanently failed after max_attempts
--   cancelled   → job no longer needed (order terminal / duplicate)

BEGIN;

-- ── New columns ───────────────────────────────────────────────────────────────

ALTER TABLE call_jobs
    ADD COLUMN IF NOT EXISTS available_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS attempts              INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts          INTEGER     NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS last_error_code       TEXT,
    ADD COLUMN IF NOT EXISTS last_error_summary    TEXT,
    ADD COLUMN IF NOT EXISTS updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS dispatched_at         TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS call_id               BIGINT REFERENCES calls(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS job_type              TEXT NOT NULL DEFAULT 'order_call',
    ADD COLUMN IF NOT EXISTS order_call_attempt_id BIGINT REFERENCES order_call_attempts(id) ON DELETE SET NULL;

-- ── Status constraint: add 'retry_wait' ──────────────────────────────────────

ALTER TABLE call_jobs DROP CONSTRAINT IF EXISTS call_jobs_job_status_check;
ALTER TABLE call_jobs ADD CONSTRAINT call_jobs_job_status_check
    CHECK (job_status IN (
        'queued', 'leased', 'processing',
        'completed', 'failed', 'cancelled',
        'retry_wait'
    ));

-- ── Indexes ───────────────────────────────────────────────────────────────────

-- Worker polling: ordered by available_at then priority.
-- Replaces old idx_call_jobs_queue which only covered (priority, created_at).
DROP INDEX IF EXISTS idx_call_jobs_queue;
CREATE INDEX IF NOT EXISTS idx_call_jobs_available
    ON call_jobs (available_at ASC, priority DESC)
    WHERE job_status IN ('queued', 'retry_wait');

-- Idempotency: one active job per (order_id, attempt_no).
-- Prevents duplicate enqueue from concurrent schedulers or request retries.
CREATE UNIQUE INDEX IF NOT EXISTS uix_call_jobs_active_order_attempt
    ON call_jobs (order_id, attempt_no)
    WHERE job_status NOT IN ('completed', 'failed', 'cancelled');

-- Crash-after-call guard: one active job per attempt row (once linked).
CREATE UNIQUE INDEX IF NOT EXISTS uix_call_jobs_active_attempt_id
    ON call_jobs (order_call_attempt_id)
    WHERE order_call_attempt_id IS NOT NULL
      AND job_status NOT IN ('completed', 'failed', 'cancelled');

-- Existing indexes are preserved:
--   idx_call_jobs_lease_expiry  ON call_jobs (lease_expires_at) WHERE job_status='leased'
--   idx_call_jobs_order_id      ON call_jobs (order_id)
--   idx_call_jobs_tenant_status ON call_jobs (tenant_id, job_status)

COMMENT ON COLUMN call_jobs.available_at IS
    'Earliest time a worker may dispatch this job. Future-dated on retry_wait.';
COMMENT ON COLUMN call_jobs.attempts IS
    'Dispatch attempts made by the queue worker (separate from order call attempts).';
COMMENT ON COLUMN call_jobs.max_attempts IS
    'Maximum dispatch attempts before permanent failure.';
COMMENT ON COLUMN call_jobs.call_id IS
    'Set to calls.id after successful dispatch acceptance. NULL until then.';
COMMENT ON COLUMN call_jobs.order_call_attempt_id IS
    'Set to order_call_attempts.id after the attempt row is created. '
    'Used as crash-recovery duplicate guard.';
COMMENT ON COLUMN call_jobs.job_type IS
    'Discriminator for future job types (currently always ''order_call'').';

COMMIT;
