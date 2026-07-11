-- Migration 011 — Live Call Monitor support fields
-- calls.dtmf_digit: last/confirmed DTMF digit pressed during the call
-- (order_call_attempts already has its own dtmf_digit; this is the
-- call-level live value for the active-call registry / UI).
-- calls.sip_gateway_id: which sip_providers trunk handled this call's
-- caller ID, for the live monitor "Gateway" column.

BEGIN;

ALTER TABLE calls ADD COLUMN IF NOT EXISTS dtmf_digit TEXT;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS sip_gateway_id INT REFERENCES sip_providers(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_calls_tenant_status ON calls(tenant_id, status);

COMMIT;
