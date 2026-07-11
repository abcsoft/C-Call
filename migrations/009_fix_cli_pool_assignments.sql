-- migrations/009_fix_cli_pool_assignments.sql
-- Replace the table-level unique constraint with a partial unique index

-- 1. Drop existing unique constraint if it exists
ALTER TABLE customer_number_assignments 
DROP CONSTRAINT IF EXISTS customer_number_assignments_phone_number_id_tenant_id_key;

-- 2. Add partial unique index on phone_number_id where unassigned_at is NULL
-- This ensures a phone number can only have AT MOST ONE active assignment at any time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_phone_assignment
ON customer_number_assignments(phone_number_id)
WHERE unassigned_at IS NULL;
