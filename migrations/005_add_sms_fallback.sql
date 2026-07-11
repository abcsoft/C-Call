-- migrations/005_add_sms_fallback.sql
-- Add SMS fallback support columns and logging table

-- 1. Add tracking columns to orders table
ALTER TABLE orders ADD COLUMN IF NOT EXISTS sms_sent BOOLEAN DEFAULT false;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS sms_sent_at TIMESTAMPTZ;

-- 2. Create sms_logs table to keep a transaction history of sent SMS messages
CREATE TABLE IF NOT EXISTS sms_logs (
    id          SERIAL PRIMARY KEY,
    tenant_id   INT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    order_id    INT REFERENCES orders(id) ON DELETE SET NULL,
    phone       TEXT NOT NULL,
    message     TEXT NOT NULL,
    gateway     TEXT NOT NULL DEFAULT 'smslayer',
    message_id  TEXT,
    status      TEXT NOT NULL,
    cost        NUMERIC(8,4) DEFAULT 0.0000,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for quick log queries per tenant/order
CREATE INDEX IF NOT EXISTS idx_sms_logs_tenant ON sms_logs (tenant_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_sms_logs_order ON sms_logs (order_id);
