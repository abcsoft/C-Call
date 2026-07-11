-- Migration 006 — Add team invitations and memberships

BEGIN;

CREATE TABLE IF NOT EXISTS team_invitations (
    id              SERIAL PRIMARY KEY,
    tenant_id       INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email           TEXT NOT NULL,
    role            TEXT NOT NULL,
    token           TEXT UNIQUE NOT NULL,
    invited_by      INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_tenant_memberships (
    id              SERIAL PRIMARY KEY,
    user_id         INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id       INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    joined_at       TIMESTAMPTZ DEFAULT now(),
    is_active       BOOLEAN DEFAULT true,
    UNIQUE (user_id, tenant_id)
);

-- Backfill existing users into memberships table
INSERT INTO user_tenant_memberships (user_id, tenant_id, role)
SELECT id, tenant_id, role FROM users WHERE tenant_id IS NOT NULL
ON CONFLICT (user_id, tenant_id) DO NOTHING;

COMMIT;
