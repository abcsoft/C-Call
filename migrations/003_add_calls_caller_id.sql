-- Migration 003 — Add caller_id to calls table
-- This enables storing the CLI caller_id used for placing each call directly on the calls table.

BEGIN;

ALTER TABLE calls ADD COLUMN IF NOT EXISTS caller_id TEXT;

COMMIT;
