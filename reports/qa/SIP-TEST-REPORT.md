# C-Call — SIP & Calling Engine Test Report

- **Policy:** No unsolicited real PSTN/SIP calls placed. All SIP/RTP/IVR tests executed with mocked SIP/RTP transport and synthetic data.

| Test Area | Scope & Command | Status |
|---|---|---|
| IVR Retry Prompt Barge-In | `pytest tests/unit/test_v2_security_and_reliability.py -k test_ivr_retry_prompt_captures_barge_in_dtmf` | **PASS** |
| IVR Missing Root / Target / Cycle Limits | `pytest tests/unit/test_v2_security_and_reliability.py -k "test_ivr_missing or test_ivr_traversal"` | **PASS** |
| Flow Tenant Validation | `pytest tests/unit/test_flows.py` (3 tests) | **PASS** |
| Durable Call Queue (`FOR UPDATE SKIP LOCKED`, leases, crash recovery, idempotency, 10 `call_state` values, campaign pause/resume) | `pytest tests/integration/test_call_queue_integration.py tests/unit/test_v2_security_and_reliability.py -k call_job` | **PASS** |
| Live Carrier SIP Registration & PSTN Audio | Requires authorized live SIP trunk credentials & test destination | **BLOCKED** |
