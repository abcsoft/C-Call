-- Migration 014: tenant_secrets table for encrypted per-tenant API keys
-- Replaces plaintext gemini_api_key / elevenlabs_api_key in tenants.settings JSON

CREATE TABLE IF NOT EXISTS tenant_secrets (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    secret_type     TEXT    NOT NULL,   -- e.g. 'gemini_api_key', 'elevenlabs_api_key'
    encrypted_value TEXT    NOT NULL,
    last_four       TEXT,               -- last 4 chars of plaintext, for UI display only
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, secret_type)
);

CREATE INDEX IF NOT EXISTS idx_tenant_secrets_tenant_id ON tenant_secrets(tenant_id);

-- Migrate existing plaintext keys from tenants.settings JSON into tenant_secrets.
-- The encrypted_value column will be populated by the Python migration script
-- (migrations/run_migrations.py) since SQL cannot call Fernet.
-- This SQL only creates the table structure.
