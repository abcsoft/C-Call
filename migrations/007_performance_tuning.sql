-- migrations/007_performance_tuning.sql
BEGIN;

-- Covering index for order list queries with status filtering and sorting
CREATE INDEX IF NOT EXISTS idx_orders_tenant_status_created 
ON orders (tenant_id, order_status, created_at DESC);

-- Covering partial index for the retry scheduler query
CREATE INDEX IF NOT EXISTS idx_orders_next_retry_covering 
ON orders (next_retry_at) INCLUDE (id, tenant_id, phone) 
WHERE order_status = 'RETRY_SCHEDULED';

COMMIT;
