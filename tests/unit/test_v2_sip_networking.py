"""Tests for V2 SIP/RTP networking: advertised vs bind IPs, transport check, RTP port pool, source validation, mock UDP SIP."""
from __future__ import annotations

import os
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

    def test_multiprocess_rtp_port_allocation_exclusivity(self):
        """Two separate OS processes allocating concurrently from RTP_PORT_MIN..MAX get distinct even ports."""
        import subprocess, sys
        code = (
            "import os, sys; "
            "os.environ['RTP_PORT_MIN']='19300'; os.environ['RTP_PORT_MAX']='19310'; "
            "from call_engine.ivr_engine import RtpPortAllocator; "
            "a = RtpPortAllocator(); s, p = a.allocate_socket('127.0.0.1'); "
            "print(p, flush=True); sys.stdin.readline(); s.close(); a.release(p)"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = "."
        p1 = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        try:
            port1 = int(p1.stdout.readline().strip())
            p2 = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            try:
                port2 = int(p2.stdout.readline().strip())
                assert port1 % 2 == 0 and port2 % 2 == 0
                assert 19300 <= port1 <= 19310 and 19300 <= port2 <= 19310
                assert port1 != port2
            finally:
                p2.communicate("quit\n", timeout=5)
        finally:
            p1.communicate("quit\n", timeout=5)

    def test_sip_cancel_while_ringing_and_port_cleanup(self):
        """Setting cancel_requested during 180 Ringing sends CANCEL, returns canceled, and frees RTP port."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        srv_port = srv.getsockname()[1]
        srv.settimeout(4.0)
        seen_methods = []

        def mock_pbx():
            # 1. REGISTER -> 200 OK
            d, addr = srv.recvfrom(4096)
            t = d.decode()
            seen_methods.append(t.split()[0])
            via = [l for l in t.splitlines() if l.lower().startswith("via:")][0]
            cid = [l for l in t.splitlines() if l.lower().startswith("call-id:")][0]
            cseq = [l for l in t.splitlines() if l.lower().startswith("cseq:")][0]
            srv.sendto(f"SIP/2.0 200 OK\r\n{via}\r\n{cid}\r\n{cseq}\r\nContent-Length: 0\r\n\r\n".encode(), addr)
            # 2. INVITE -> 180 Ringing
            d, addr = srv.recvfrom(4096)
            t = d.decode()
            seen_methods.append(t.split()[0])
            via = [l for l in t.splitlines() if l.lower().startswith("via:")][0]
            cid = [l for l in t.splitlines() if l.lower().startswith("call-id:")][0]
            cseq = [l for l in t.splitlines() if l.lower().startswith("cseq:")][0]
            srv.sendto(f"SIP/2.0 180 Ringing\r\n{via}\r\n{cid}\r\n{cseq}\r\nTo: <sip:01711223344@127.0.0.1>;tag=t1\r\nContent-Length: 0\r\n\r\n".encode(), addr)
            # 3. Expect CANCEL (read until CANCEL or timeout)
            try:
                d, addr = srv.recvfrom(4096)
                seen_methods.append(d.decode().split()[0])
            except OSError:
                pass

        t = threading.Thread(target=mock_pbx, daemon=True)
        t.start()
        ivr = IvrCall(
            target="01711223344",
            flow=self._flow(),
            sip=self._sip_cfg(SIP_PORT=str(srv_port)),
            on_event=lambda e: ivr.request_cancel() if e.type == "ringing" else None,
            invite_timeout=4,
        )
        try:
            res = ivr.run()
            t.join(timeout=3)
            assert res.status == "canceled", f"error={res.error} events={seen_methods}"
            assert "CANCEL" in seen_methods
            assert ivr.rtp_port not in _rtp_port_allocator._in_use
        finally:
            srv.close()

    def test_bridge_leg_uses_advertised_ip_and_releases_port_on_failure(self, monkeypatch):
        monkeypatch.setenv("RTP_PORT_MIN", "19320")
        monkeypatch.setenv("RTP_PORT_MAX", "19330")
        ivr = IvrCall(
            target="01711223344",
            flow=self._flow(),
            sip=self._sip_cfg(SIP_PORT="59999", SIP_ADVERTISED_IP="203.0.113.20", RTP_ADVERTISED_IP="203.0.113.21"),
            on_event=lambda e: None,
        )
        sent_msgs = []
        monkeypatch.setattr(ivr, "_send", lambda msg, addr=None: sent_msgs.append(msg))
        monkeypatch.setattr(ivr, "_recv_until", lambda pred, timeout: None)
        try:
            with pytest.raises(RuntimeError, match="agent call timed out"):
                ivr._invite_bridge_leg("1001", timeout=0.2)
            assert len(sent_msgs) == 1
            assert "Via: SIP/2.0/UDP 203.0.113.20:" in sent_msgs[0]
            assert "c=IN IP4 203.0.113.21" in sent_msgs[0]
            # Only the main call's rtp_port should remain in _in_use within 19320..19330; bridge port was released
            range_ports = {p for p in _rtp_port_allocator._in_use if 19320 <= p <= 19330}
            assert range_ports == {ivr.rtp_port}
        finally:
            ivr.sock.close()
            ivr.rtp.close()
            _rtp_port_allocator.release(ivr.rtp_port)

    def test_refer_uses_advertised_ip(self, monkeypatch):
        ivr = IvrCall(
            target="01711223344",
            flow=self._flow(),
            sip=self._sip_cfg(SIP_PORT="59999", SIP_ADVERTISED_IP="203.0.113.30"),
            on_event=lambda e: None,
        )
        sent_msgs = []
        monkeypatch.setattr(ivr, "_send", lambda msg, addr=None: sent_msgs.append(msg))
        monkeypatch.setattr(ivr, "_recv_until", lambda pred, timeout: "SIP/2.0 202 Accepted\r\nCSeq: 2 REFER\r\n\r\n")
        try:
            ok, detail, _ = ivr._refer(2, "<sip:01711223344@127.0.0.1>;tag=t1", "1001")
            assert ok is True
            assert "202 Accepted" in detail
            assert "Via: SIP/2.0/UDP 203.0.113.30:" in sent_msgs[0]
            assert "Contact: <sip:testuser@203.0.113.30:" in sent_msgs[0]
        finally:
            ivr.close()

    def test_bridge_leg_ack_uses_advertised_ip(self, monkeypatch):
        ivr = IvrCall(
            target="01711223344",
            flow=self._flow(),
            sip=self._sip_cfg(SIP_PORT="59999", SIP_ADVERTISED_IP="203.0.113.40", RTP_ADVERTISED_IP="203.0.113.41"),
            on_event=lambda e: None,
        )
        sent_msgs = []
        monkeypatch.setattr(ivr, "_send", lambda msg, addr=None: sent_msgs.append(msg))
        ok_200 = (
            "SIP/2.0 200 OK\r\nVia: SIP/2.0/UDP 203.0.113.40:5060\r\nTo: <sip:1001@127.0.0.1>;tag=ag1\r\n"
            "Contact: <sip:1001@127.0.0.1:5060>\r\nContent-Type: application/sdp\r\n\r\n"
            "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\nc=IN IP4 127.0.0.1\r\nm=audio 40000 RTP/AVP 0\r\n"
        )
        monkeypatch.setattr(ivr, "_recv_until", lambda pred, timeout: ok_200)
        leg = None
        try:
            leg = ivr._invite_bridge_leg("1001", timeout=1)
            assert len(sent_msgs) == 2
            assert "ACK sip:1001@127.0.0.1:5060 SIP/2.0" in sent_msgs[1]
            assert "Via: SIP/2.0/UDP 203.0.113.40:" in sent_msgs[1]
        finally:
            if leg:
                leg.rtp.close()
                _rtp_port_allocator.release(leg.rtp_port)
            ivr.close()

    def test_ippbx_acceptance_mock_rehearsal(self):
        from scripts.ippbx_acceptance import run_mock_acceptance
        assert run_mock_acceptance(transfer_to="1001") == 0
