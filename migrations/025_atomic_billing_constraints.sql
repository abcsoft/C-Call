-- Migration 025: Billing atomicity constraints
--
-- Adds a partial unique index that prevents duplicate call debits in
-- wallet_transactions even when two processes bypass the application-level
-- idempotency check simultaneously.
--
-- ref_id format for call debits: 'call:{calls.id}'
-- Established by billing_service.finalize_call_billing().
--
-- Safety check: aborts if duplicate call debits already exist so that
-- financial history is never silently repaired by a migration.
-- Run scripts/reconcile_call_billing.py --dry-run first to identify any
-- anomalies, then reconcile before re-running this migration.
--
-- Depends on: 002_saas_layer.sql (wallet_transactions), 013_add_check_constraints.sql

BEGIN;

-- Duplicate detection: abort if any (tenant_id, ref_id) pair already has
-- more than one debit row. Repairs belong in reconciliation, not migration.
DO $$
DECLARE
    dup_count INT;
    dup_info  TEXT;
BEGIN
    SELECT COUNT(*) INTO dup_count
    FROM (
        SELECT ref_id, tenant_id
        FROM wallet_transactions
        WHERE type = 'debit' AND ref_id LIKE 'call:%'
        GROUP BY ref_id, tenant_id
        HAVING COUNT(*) > 1
    ) t;

    IF dup_count > 0 THEN
        SELECT string_agg(ref_id || ' (tenant=' || tenant_id::text || ')', ', ' ORDER BY ref_id)
        INTO dup_info
        FROM (
            SELECT DISTINCT ref_id, tenant_id
            FROM wallet_transactions
            WHERE type = 'debit' AND ref_id LIKE 'call:%'
            GROUP BY ref_id, tenant_id
            HAVING COUNT(*) > 1
            LIMIT 10
        ) t;

        RAISE EXCEPTION
            'Duplicate call debits exist in wallet_transactions (% group(s)): %. '
            'Run: python scripts/reconcile_call_billing.py --dry-run  to identify affected rows. '
            'Do not run --apply without operator review of the dry-run output. '
            'After reconciliation re-run this migration.',
            dup_count, dup_info;
    END IF;
END $$;

-- Partial unique index: one debit row per (tenant_id, call_id) pair.
-- Belt-and-suspenders against the FOR UPDATE idempotency guard:
-- even if two concurrent transactions somehow both pass billing_finalized=FALSE,
-- only one INSERT succeeds; the other rolls back with a unique violation.
CREATE UNIQUE INDEX IF NOT EXISTS wallet_transactions_call_debit_uniq
    ON wallet_transactions (tenant_id, ref_id)
    WHERE type = 'debit' AND ref_id LIKE 'call:%';

COMMENT ON INDEX wallet_transactions_call_debit_uniq IS
    'One debit row per call per tenant. ref_id format: ''call:{calls.id}''. '
    'Added in migration 025 as a belt-and-suspenders guard against double-billing '
    'when the application-level billing_finalized check is bypassed under concurrency.';

-- Partial unique index: one refund row per original debit reference.
-- Prevents duplicate refunds for the same call.
CREATE UNIQUE INDEX IF NOT EXISTS wallet_transactions_call_refund_uniq
    ON wallet_transactions (tenant_id, ref_id)
    WHERE type = 'refund' AND ref_id LIKE 'refund:call:%';

COMMENT ON INDEX wallet_transactions_call_refund_uniq IS
    'One refund row per call per tenant. ref_id format: ''refund:call:{calls.id}''. '
    'Added in migration 025.';

COMMIT;
