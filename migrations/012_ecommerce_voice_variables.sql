-- Migration 012 — E-Commerce Voice Variables
-- Adds metadata JSONB, custom_text, and custom_audio_url columns to orders
-- for rich e-commerce data (product_name, price, delivery_charge, grand_total, etc.)
-- and per-order voice override support.
-- Idempotent — IF NOT EXISTS / safe to re-run.

BEGIN;

-- Rich e-commerce metadata (product details, pricing breakdown, key_actions, etc.)
ALTER TABLE orders ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;

-- Per-order TTS text override (bypasses tenant voice_script template)
ALTER TABLE orders ADD COLUMN IF NOT EXISTS custom_text TEXT;

-- Per-order pre-recorded audio URL (bypasses TTS entirely if valid)
ALTER TABLE orders ADD COLUMN IF NOT EXISTS custom_audio_url TEXT;

-- GIN index on metadata for efficient JSONB queries (e.g. filtering by product_name)
CREATE INDEX IF NOT EXISTS idx_orders_metadata ON orders USING gin (metadata);

COMMIT;
