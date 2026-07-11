-- Migration 010 — Fix wallet billing rules and CDR usage fields
-- Adds pulse billing options, minimum billable seconds, failed call charge configuration,
-- and links calls/attempts to wallet_transactions for idempotency / double-billing prevention.

BEGIN;

ALTER TABLE customer_rates ADD COLUMN IF NOT EXISTS billing_pulse_seconds INT DEFAULT 60;
ALTER TABLE customer_rates ADD COLUMN IF NOT EXISTS minimum_billable_seconds INT DEFAULT 60;
ALTER TABLE customer_rates ADD COLUMN IF NOT EXISTS charge_failed_calls BOOLEAN DEFAULT FALSE;

ALTER TABLE calls ADD COLUMN IF NOT EXISTS charge NUMERIC(10,4) DEFAULT 0.0000;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS billable_seconds INT DEFAULT 0;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS wallet_transaction_id INT REFERENCES wallet_transactions(id) ON DELETE SET NULL;

ALTER TABLE order_call_attempts ADD COLUMN IF NOT EXISTS billable_seconds INT DEFAULT 0;
ALTER TABLE order_call_attempts ADD COLUMN IF NOT EXISTS wallet_transaction_id INT REFERENCES wallet_transactions(id) ON DELETE SET NULL;

COMMIT;
