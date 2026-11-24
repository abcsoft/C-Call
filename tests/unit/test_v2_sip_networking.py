"""Tests for V2 SIP/RTP networking: advertised vs bind IPs, transport check, RTP port pool, source validation, mock UDP SIP."""
from __future__ import annotations

import socket
import struct
import threading
from unittest.mock import patch

import pytest

from call_engine.ivr_engine import Flow, IvrCall, Node, RtpPortAllocator, _rtp_port_allocator


class TestV2SipNetworking:
    def _sip_cfg(self, **extra):
        base = {
            "SIP_DOMAIN": "127.0.0.1",
            "SIP_PORT": "5060",
            "SIP_USER": "testuser",
            "SIP_PASSWORD": "secret",
            "SIP_BIND_IP": "127.0.0.1",
            "RTP_BIND_IP": "127.0.0.1",
        }
        base.update(extra)
        return base

    def _flow(self):
        return Flow(id=1, root_node_id=1, nodes={1: Node(id=1, name="End", node_type="hangup")})

    def test_advertised_public_ip_in_via_contact_sdp(self):
        ivr = IvrCall(
            target="01711223344",
            flow=self._flow(),
            sip=self._sip_cfg(SIP_ADVERTISED_IP="203.0.113.10", RTP_ADVERTISED_IP="203.0.113.11"),
            on_event=lambda e: None,
        )
        try:
            assert ivr.advertised_ip == "203.0.113.10"
            assert ivr.rtp_advertised_ip == "203.0.113.11"
            assert "203.0.113.10" in ivr.call_id
            msg = ivr._invite_message(1)
            assert "Via: SIP/2.0/UDP 203.0.113.10:" in msg
            assert "Contact: <sip:testuser@203.0.113.10:" in msg
            assert "o=- " in msg and "IN IP4 203.0.113.11" in msg
            assert "c=IN IP4 203.0.113.11" in msg
        finally:
            ivr.sock.close()
            ivr.rtp.close()
            _rtp_port_allocator.release(ivr.rtp_port)

    def test_unsupported_sip_transport_rejected(self):
        with pytest.raises(ValueError, match="Unsupported SIP transport"):
            IvrCall(
                target="01711223344",
                flow=self._flow(),
                sip=self._sip_cfg(SIP_TRANSPORT="tls"),
                on_event=lambda e: None,
            )

    def test_rtp_port_allocator_concurrent_even_and_cleanup(self, monkeypatch):
        monkeypatch.setenv("RTP_PORT_MIN", "19200")
        monkeypatch.setenv("RTP_PORT_MAX", "19210")
        alloc = RtpPortAllocator()
        s1, p1 = alloc.allocate_socket("127.0.0.1")
        s2, p2 = alloc.allocate_socket("127.0.0.1")
        try:
            assert p1 % 2 == 0 and p2 % 2 == 0
            assert 19200 <= p1 <= 19210 and 19200 <= p2 <= 19210
            assert p1 != p2
        finally:
            s1.close()
            s2.close()
            alloc.release(p1)
            alloc.release(p2)
        # Port can be re-allocated after release
        s3, p3 = alloc.allocate_socket("127.0.0.1")
        s3.close()
        alloc.release(p3)
        assert p3 == p1

    def test_incoming_rtp_source_validation(self):
        ivr = IvrCall(
            target="01711223344",
            flow=self._flow(),
            sip=self._sip_cfg(),
            on_event=lambda e: None,
        )
        try:
            # Build an RFC 2833 DTMF packet for digit '1'
            dtmf_pkt = struct.pack("!BBHII", 0x80, 101, 1, 160, 999) + bytes([1, 0x0A, 0x00, 0xA0])
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(dtmf_pkt, ("127.0.0.1", ivr.rtp_port))
            sender.close()

            # Drain expecting a DIFFERENT remote_ip -> packet must be rejected
            assert ivr._drain_rtp("198.51.100.99", 5000) is None

            # Send again and drain expecting 127.0.0.1 -> accepted
            sender2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender2.sendto(dtmf_pkt, ("127.0.0.1", ivr.rtp_port))
            sender2.close()
            assert ivr._drain_rtp("127.0.0.1", 5000) == "1"
        finally:
            ivr.sock.close()
            ivr.rtp.close()
            _rtp_port_allocator.release(ivr.rtp_port)

    def test_mock_udp_sip_register_challenge_and_200(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        srv_port = srv.getsockname()[1]
        srv.settimeout(3.0)

        def mock_registrar():
            for expected_cseq, code in [(1, "401 Unauthorized"), (2, "200 OK")]:
                data, addr = srv.recvfrom(4096)
                txt = data.decode()
                via = [l for l in txt.splitlines() if l.lower().startswith("via:")][0]
                cid = [l for l in txt.splitlines() if l.lower().startswith("call-id:")][0]
                cseq = [l for l in txt.splitlines() if l.lower().startswith("cseq:")][0]
                extra = 'WWW-Authenticate: Digest realm="ccall", nonce="abc123"\r\n' if expected_cseq == 1 else ""
                resp = f"SIP/2.0 {code}\r\n{via}\r\n{cid}\r\n{cseq}\r\n{extra}Content-Length: 0\r\n\r\n"
                srv.sendto(resp.encode(), addr)

        t = threading.Thread(target=mock_registrar, daemon=True)
        t.start()
        events = []
        ivr = IvrCall(
            target="01711223344",
            flow=self._flow(),
            sip=self._sip_cfg(SIP_PORT=str(srv_port)),
            on_event=events.append,
        )
        try:
            ivr._register()
            assert any(e.type == "registered" for e in events)
        finally:
            ivr.sock.close()
            ivr.rtp.close()
            _rtp_port_allocator.release(ivr.rtp_port)
            srv.close()
