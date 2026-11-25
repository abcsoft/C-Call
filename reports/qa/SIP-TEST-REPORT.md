# C-Call — SIP & Calling Engine Test Report

- **Policy:** No unsolicited real PSTN/SIP calls placed. All SIP/RTP/IVR tests executed with mocked/loopback UDP SIP/RTP transport and synthetic data.
- **Covered modules:** `call_engine/ivr_engine.py`, `call_engine/direct_sip_call.py`, `rtp_probe.py`, `app/models/sip_provider.py`, `scripts/ippbx_acceptance.py`.

| Test Area | Scope & Command | Status |
|---|---|---|
| Separate Local Bind vs Advertised Public SIP/RTP IPs (`SIP_BIND_IP`, `SIP_ADVERTISED_IP`, `RTP_BIND_IP`, `RTP_ADVERTISED_IP`) | `pytest tests/unit/test_v2_sip_networking.py -k test_advertised_public_ip_in_via_contact_sdp` | **PASS** |
| Bridge-Leg & `REFER` Advertised IP, `ACK` `Via` & Port Cleanup on Failure | `pytest tests/unit/test_v2_sip_networking.py -k "bridge_leg or test_refer"` | **PASS** |
| Explicit SIP Transport Capability Validation (`udp` supported; `tls`/`tcp` rejected) | `pytest tests/unit/test_v2_sip_networking.py -k test_unsupported_sip_transport_rejected` | **PASS** |
| Thread-Safe & Multi-Process Even RTP Port Allocator (`RTP_PORT_MIN`..`RTP_PORT_MAX`, `SO_EXCLUSIVEADDRUSE`) | `pytest tests/unit/test_v2_sip_networking.py -k "test_rtp_port_allocator or test_multiprocess_rtp"` | **PASS** |
| Incoming RTP Source IP Validation & RFC 3550 Jitter / Packet-Loss / Latency Telemetry | `pytest tests/unit/test_v2_sip_networking.py` (`test_incoming_rtp_source_validation`, `test_ivr_call_telemetry_latency_jitter_and_loss`) | **PASS** |
| Mock UDP SIP REGISTER 401 Digest Challenge + 200 OK (`Call-ID`/`CSeq` validation) | `pytest tests/unit/test_v2_sip_networking.py -k test_mock_udp_sip_register_challenge_and_200` | **PASS** |
| SIP `CANCEL` During 180 Ringing & Port Cleanup | `pytest tests/unit/test_v2_sip_networking.py -k test_sip_cancel_while_ringing_and_port_cleanup` | **PASS** |
| Direct SIP Startup ACK Protocol | `pytest tests/unit/test_direct_sip_ack.py` (24 tests) | **PASS** |
| Admin SIP Diagnostics: `SIP/2.0 404` Reject, Digest `401 -> 200` Non-Calling REGISTER, Wrong Auth `403`, `max_channels`/Port/Duplicate Validation & Stale Call Reconciliation | `pytest tests/unit/test_v2_admin_lifecycle_and_sip_diag.py` + Playwright (`:5001`) | **PASS** |
| End-to-End Local UDP Mock IP-PBX Rehearsal (REGISTER, INVITE, RTP audio, DTMF, recording, REFER transfer, BYE) | `scripts/ippbx_acceptance.py --mode mock-acceptance --transfer-to 1001` & `test_ippbx_acceptance_mock_rehearsal` | **PASS** |
| Live Carrier IP-PBX Call, Audio, DTMF, Hangup, Transfer | Requires explicit authorization + live SIP credentials (`scripts/ippbx_acceptance.py --mode full-call --confirm-authorized-test --target ...`) | **BLOCKED** |
