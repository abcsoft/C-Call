-- migrations/027_wallet_opening_balance.sql
-- Adds wallets.opening_balance so the wallet drift detector can correctly compute:
--
--   expected_balance = opening_balance
--                    + SUM(credits + refunds)
--                    - SUM(debits + withdrawals/adjustments)
--
-- Without this column the drift detector cannot distinguish a "legitimate" wallet
-- whose balance was established before ledger history from a wallet that has drifted
-- due to a bug.
--
-- Production wallets are always created at balance=0 (see models/wallet.py
-- create_wallet()) and every subsequent movement goes through wallet_service
-- which inserts a ledger row, so opening_balance=0 is the correct default for
-- all production rows.
--
-- For wallets that pre-date the ledger (legacy rows seeded directly in the DB),
-- opening_balance should be set manually after reconciliation review.  Until then
-- the drift detector reports them as 'manual_review' rather than auto-repairable.
--
-- Depends on: 002_saas_layer.sql (wallets table)
-- Safe to re-run: ADD COLUMN IF NOT EXISTS.

BEGIN;

ALTER TABLE wallets
    ADD COLUMN IF NOT EXISTS opening_balance NUMERIC(12,4) NOT NULL DEFAULT 0;

COMMENT ON COLUMN wallets.opening_balance IS
    'Balance at wallet creation (before any ledger-tracked transactions). '
    'Drift expected_balance = opening_balance + credits + refunds - debits. '
    'Set to 0 for all wallets created via create_wallet(); set manually for '
    'legacy seeded wallets after reconciliation review. Added in migration 027.';

COMMIT;
