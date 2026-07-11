-- Migration 013: CHECK constraints on status/type columns + billing_finalized column
-- Prevents invalid state values from entering the database.

-- orders.order_status
ALTER TABLE orders
    ADD CONSTRAINT chk_order_status CHECK (order_status IN (
        'NEW', 'CALL_QUEUED', 'CALLING', 'CONFIRMED', 'CANCELLED',
        'NO_ANSWER', 'UNREACHABLE', 'FAILED', 'RETRY_SCHEDULED'
    ));

-- orders.webhook_status
ALTER TABLE orders
    ADD CONSTRAINT chk_order_webhook_status CHECK (webhook_status IN (
        'pending', 'sent', 'delivered', 'failed', 'skipped'
    ));

-- calls.status
ALTER TABLE calls
    ADD CONSTRAINT chk_call_status CHECK (status IN (
        'queued', 'calling', 'initiated', 'answered', 'confirmed',
        'cancelled', 'canceled', 'no_answer', 'busy', 'failed',
        'unreachable', 'timeout', 'completed'
    ));

-- wallet_transactions.type
ALTER TABLE wallet_transactions
    ADD CONSTRAINT chk_wallet_txn_type CHECK (type IN (
        'credit', 'debit', 'refund', 'adjustment'
    ));

-- billing_finalized idempotency sentinel (replaces wallet_transaction_id IS NOT NULL check
-- which was always NULL for zero-charge calls, causing the billing loop to run forever).
ALTER TABLE calls
    ADD COLUMN IF NOT EXISTS billing_finalized BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_calls_billing_unfinalized
    ON calls (id) WHERE billing_finalized = FALSE;

-- 3.8: Partial index for monitor loop hot query
-- "find order_call_attempts rows that haven't completed yet"
-- NOTE: In production with live traffic, run this manually with CONCURRENTLY outside a transaction.
CREATE INDEX IF NOT EXISTS idx_oca_result_null
    ON order_call_attempts (id) WHERE result IS NULL;
