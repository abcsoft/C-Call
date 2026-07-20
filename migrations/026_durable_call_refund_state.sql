-- migrations/026_durable_call_refund_state.sql
-- Adds durable refund state to the calls table so a process crash after
-- wallet credit but before application return cannot result in a double-refund
-- on retry.  refund_call_charge() reads this column (via SELECT FOR UPDATE)
-- before issuing a refund, making the idempotency check durable across process
-- restarts even if the wallet_transactions ledger row is the only other evidence.
--
-- Depends on:
--   002_saas_layer.sql       (calls table, wallet_transactions table)
--   010_fix_wallet_billing_rules.sql (calls.billing_finalized, calls.charge)
--   025_atomic_billing_constraints.sql (wallet_transactions_call_refund_uniq)
--
-- Safe to re-run: all DDL uses IF NOT EXISTS or ADD COLUMN IF NOT EXISTS.

BEGIN;

-- ── Safety pre-check: abort if duplicate refunds already exist ────────────────
-- Any row in wallet_transactions with type='refund' and ref_id='refund:call:X'
-- appearing more than once per tenant means financial data is already inconsistent.
-- Repairs belong in reconciliation, not migration.
DO $$
DECLARE
    dup_count INT;
    dup_info  TEXT;
BEGIN
    SELECT COUNT(*) INTO dup_count
    FROM (
        SELECT ref_id, tenant_id
        FROM wallet_transactions
        WHERE type = 'refund' AND ref_id LIKE 'refund:call:%'
        GROUP BY ref_id, tenant_id
        HAVING COUNT(*) > 1
    ) t;

    IF dup_count > 0 THEN
        SELECT string_agg(ref_id || ' (tenant=' || tenant_id::text || ')', ', ' ORDER BY ref_id)
        INTO dup_info
        FROM (
            SELECT DISTINCT ref_id, tenant_id
            FROM wallet_transactions
            WHERE type = 'refund' AND ref_id LIKE 'refund:call:%'
            GROUP BY ref_id, tenant_id
            HAVING COUNT(*) > 1
            LIMIT 10
        ) t;

        RAISE EXCEPTION
            'Duplicate call refund rows exist in wallet_transactions (% group(s)): %. '
            'Run: python scripts/reconcile_call_billing.py --dry-run to identify affected rows. '
            'After reconciliation re-run this migration.',
            dup_count, dup_info;
    END IF;
END $$;

-- ── Durable refund state columns on calls ─────────────────────────────────────
ALTER TABLE calls
    ADD COLUMN IF NOT EXISTS refund_status TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS refund_transaction_id BIGINT,
    ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS refund_reason_code TEXT,
    ADD COLUMN IF NOT EXISTS refund_source TEXT;

-- ── Constraints ───────────────────────────────────────────────────────────────
-- Only add constraints if not already present (idempotent via DO block).
DO $$
BEGIN
    -- refund_status domain check
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'calls'
          AND constraint_name = 'calls_refund_status_check'
    ) THEN
        ALTER TABLE calls
            ADD CONSTRAINT calls_refund_status_check
            CHECK (refund_status IN ('none', 'refunded'));
    END IF;

    -- refunded_at required when refunded
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'calls'
          AND constraint_name = 'calls_refund_timestamp_required'
    ) THEN
        ALTER TABLE calls
            ADD CONSTRAINT calls_refund_timestamp_required
            CHECK (refund_status = 'none' OR refunded_at IS NOT NULL);
    END IF;

    -- refund_transaction_id required when refunded
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'calls'
          AND constraint_name = 'calls_refund_txn_required'
    ) THEN
        ALTER TABLE calls
            ADD CONSTRAINT calls_refund_txn_required
            CHECK (refund_status = 'none' OR refund_transaction_id IS NOT NULL);
    END IF;
END $$;

-- ── FK: refund_transaction_id → wallet_transactions ──────────────────────────
-- Added separately so we can handle the case where the column was just added.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'calls'
          AND constraint_name = 'calls_refund_transaction_id_fk'
    ) THEN
        ALTER TABLE calls
            ADD CONSTRAINT calls_refund_transaction_id_fk
            FOREIGN KEY (refund_transaction_id) REFERENCES wallet_transactions(id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

-- ── Backfill: populate durable state from existing refund ledger rows ─────────
-- For calls that already have a matching refund ledger row (refund:call:X),
-- set refund_status='refunded' and refund_transaction_id from the ledger.
-- ref_id format: 'refund:call:{calls.id}'  → substring starts at position 13.
-- Only sets rows currently at 'none' to avoid overwriting correct data.
UPDATE calls c
SET
    refund_status        = 'refunded',
    refund_transaction_id = wt.id,
    refunded_at          = COALESCE(wt.created_at, now()),
    refund_reason_code   = 'backfill_from_ledger',
    refund_source        = 'migration_026'
FROM wallet_transactions wt
WHERE wt.type = 'refund'
  AND wt.ref_id = 'refund:call:' || c.id::text
  AND c.refund_status = 'none'
  AND c.billing_finalized = TRUE;

-- ── Index for fast refund state lookups ──────────────────────────────────────
CREATE INDEX IF NOT EXISTS calls_refund_status_idx
    ON calls (tenant_id, refund_status)
    WHERE refund_status = 'refunded';

COMMENT ON COLUMN calls.refund_status IS
    'Durable refund state. Values: none (default), refunded. '
    'Set atomically with wallet credit and ledger insert in refund_call_charge(). '
    'Added in migration 026.';

COMMENT ON COLUMN calls.refund_transaction_id IS
    'FK to wallet_transactions.id for the refund credit row. '
    'Required when refund_status=''refunded''. Added in migration 026.';

COMMIT;
