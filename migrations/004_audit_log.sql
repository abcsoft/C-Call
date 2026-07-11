-- Migration 004 — Audit log for sensitive admin actions (impersonation, etc.)

BEGIN;

CREATE TABLE IF NOT EXISTS audit_log (
    id              SERIAL PRIMARY KEY,
    admin_user_id   INT NOT NULL REFERENCES users(id),
    action          TEXT NOT NULL,               -- e.g. 'impersonate_start', 'impersonate_end'
    target_tenant_id INT REFERENCES tenants(id),
    reason          TEXT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);
COMMENT ON TABLE audit_log IS 'Audit trail for sensitive super-admin actions — impersonation start/end, manual wallet adjustments, etc.';

CREATE INDEX IF NOT EXISTS idx_audit_log_target ON audit_log (target_tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_admin  ON audit_log (admin_user_id, created_at DESC);

COMMIT;
