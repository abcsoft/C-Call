-- Migration 020: Persistent call job queue for exclusive worker processing.
-- Replaces in-memory ThreadPoolExecutor dispatch with a durable DB-backed queue.
-- Each job is atomically leased by one worker via FOR UPDATE SKIP LOCKED.
-- Dead worker leases are recovered after lease_expires_at passes.

CREATE TABLE IF NOT EXISTS call_jobs (
    id               BIGSERIAL PRIMARY KEY,
    order_id         BIGINT       NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    tenant_id        INTEGER      NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    caller_id        TEXT,
    audio_path       TEXT,
    attempt_no       INTEGER      NOT NULL DEFAULT 1,
    priority         SMALLINT     NOT NULL DEFAULT 0,    -- higher = sooner
    job_status       TEXT         NOT NULL DEFAULT 'queued'
                         CHECK (job_status IN ('queued','leased','processing',
                                               'completed','failed','cancelled')),
    leased_by        TEXT,
    leased_at        TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    error_message    TEXT,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ
);

-- Fast queue poll: pending jobs ordered by priority, age
CREATE INDEX IF NOT EXISTS idx_call_jobs_queue
    ON call_jobs (priority DESC, created_at ASC)
    WHERE job_status IN ('queued','leased');

-- Lease expiry recovery
CREATE INDEX IF NOT EXISTS idx_call_jobs_lease_expiry
    ON call_jobs (lease_expires_at)
    WHERE job_status = 'leased';

-- Per-order lookup (e.g. cancel pending job when order is manually cancelled)
CREATE INDEX IF NOT EXISTS idx_call_jobs_order_id
    ON call_jobs (order_id);

-- Tenant queue depth
CREATE INDEX IF NOT EXISTS idx_call_jobs_tenant_status
    ON call_jobs (tenant_id, job_status);

COMMENT ON TABLE call_jobs IS
    'Persistent call job queue. Workers use FOR UPDATE SKIP LOCKED to lease jobs exclusively.';
