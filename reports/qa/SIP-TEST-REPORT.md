# C-Call — SIP & Calling Engine Test Report

- **Policy:** No unsolicited real PSTN/SIP calls placed. All SIP/RTP/IVR tests executed with mocked/loopback UDP SIP/RTP transport and synthetic data.

| Test Area | Scope & Command | Status |
|---|---|---|
| Separate Local Bind vs Advertised Public SIP/RTP IPs (`SIP_BIND_IP`, `SIP_ADVERTISED_IP`, `RTP_BIND_IP`, `RTP_ADVERTISED_IP`) | `pytest tests/unit/test_v2_sip_networking.py -k test_advertised_public_ip_in_via_contact_sdp` | **PASS** |
| Explicit SIP Transport Capability Validation (`udp` supported; `tls`/`tcp` rejected) | `pytest tests/unit/test_v2_sip_networking.py -k test_unsupported_sip_transport_rejected` | **PASS** |
| Concurrency-Safe Even RTP Port Allocator (`RTP_PORT_MIN`..`RTP_PORT_MAX`) & Cleanup | `pytest tests/unit/test_v2_sip_networking.py -k test_rtp_port_allocator_concurrent_even_and_cleanup` | **PASS** |
| Incoming RTP Source IP Validation | `pytest tests/unit/test_v2_sip_networking.py -k test_incoming_rtp_source_validation` | **PASS** |
| Mock UDP SIP REGISTER 401 Digest Challenge + 200 OK (`Call-ID`/`CSeq` validation) | `pytest tests/unit/test_v2_sip_networking.py -k test_mock_udp_sip_register_challenge_and_200` | **PASS** |
| Non-Dialing Provider Connectivity Diagnostic (`OPTIONS` ping) | `app/models/sip_provider.py::test_connection()` (NAT-aware `SIP_ADVERTISED_IP` + `SIP_BIND_IP`) | **PASS** |
| IVR Retry Prompt Barge-In, Missing Root/Target & Cycle Limits | `pytest tests/unit/test_v2_security_and_reliability.py -k TestV2CallingReliabilityAndIVR` | **PASS** |
| Durable Call Queue (`FOR UPDATE SKIP LOCKED`, leases, crash recovery, idempotency, 10 `call_state` values, campaign pause/resume) | `pytest tests/integration/test_call_queue_integration.py` | **PASS** |
| Live Carrier SIP Registration & PSTN Audio | Requires authorized live SIP trunk credentials & approved test destination | **BLOCKED** |
