#!/usr/bin/env python3
"""Authorized IP-PBX Acceptance Test Harness (C-Call V2).

Modes:
  diagnostic      DNS lookup + UDP SIP REGISTER check only (zero calls placed).
  mock-acceptance Self-contained localhost UDP mock IP-PBX rehearsal covering
                  registration, outbound INVITE, RTP audio, DTMF, recording,
                  transfer (REFER), and hangup (BYE).
  full-call       Live IP-PBX verification; strictly gated behind BOTH
                  --confirm-authorized-test AND --target <approved_number>.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

from call_engine.audio import ensure_dirs
from call_engine.ivr_engine import Flow, IvrCall, Node, _header, _status


def _send_dtmf_and_audio(rtp_ip: str, rtp_port: int, digit_code: int, stop_event: threading.Event) -> None:
    """Emit minimal PCMA/PCMU RTP frames + RFC 2833 DTMF event to IvrCall's RTP socket."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        seq = 1
        ts = 160
        ssrc = 0x12345678
        # 1. Send a few voice frames so caller recording is populated
        voice_payload = bytes([0x7F] * 160)
        for _ in range(5):
            pkt = struct.pack("!BBHII", 0x80, 0x00, seq, ts, ssrc) + voice_payload
            sock.sendto(pkt, (rtp_ip, rtp_port))
            seq += 1
            ts += 160
            time.sleep(0.02)
        # 2. Send RFC 2833 DTMF packet (payload type 101)
        dtmf_payload = bytes([digit_code, 0x0A, 0x00, 0xA0])
        pkt = struct.pack("!BBHII", 0x80, 101, seq, ts, ssrc) + dtmf_payload
        sock.sendto(pkt, (rtp_ip, rtp_port))
        stop_event.wait(timeout=0.5)
    finally:
        sock.close()


def _extract_sdp(msg: str) -> tuple[str, int]:
    body = msg.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in msg else ""
    ip_m = re.search(r"^c=IN IP[46]\s+([^\r\n]+)", body, re.M)
    pt_m = re.search(r"^m=audio\s+(\d+)", body, re.M)
    return (ip_m.group(1) if ip_m else "127.0.0.1", int(pt_m.group(1)) if pt_m else 0)


def _reply(sock: socket.socket, addr: tuple[str, int], req: str, status_line: str, extra: str = "", body: str = "") -> None:
    via = _header(req, "Via")
    frm = _header(req, "From")
    to = _header(req, "To")
    if ";tag=" not in to.lower():
        to = f"{to};tag=pbxmock1"
    cid = _header(req, "Call-ID")
    cseq = _header(req, "CSeq")
    msg = (
        f"SIP/2.0 {status_line}\r\n"
        f"Via: {via}\r\n"
        f"From: {frm}\r\n"
        f"To: {to}\r\n"
        f"Call-ID: {cid}\r\n"
        f"CSeq: {cseq}\r\n"
        f"{extra}"
        f"Content-Length: {len(body)}\r\n\r\n{body}"
    )
    sock.sendto(msg.encode(), addr)


def run_diagnostic(sip_cfg: dict[str, str]) -> int:
    domain = sip_cfg["SIP_DOMAIN"]
    resolved_ip = socket.gethostbyname(domain)
    events: list[str] = []
    flow = Flow(id=0, root_node_id=1, nodes={1: Node(id=1, name="Diag", node_type="hangup")})
    ivr = IvrCall(target="diagnostic", flow=flow, sip=sip_cfg, on_event=lambda e: events.append(e.type))
    try:
        ivr._register()
        report = {
            "mode": "diagnostic",
            "domain": domain,
            "resolved_ip": resolved_ip,
            "sip_port": int(sip_cfg["SIP_PORT"]),
            "transport": ivr.transport,
            "advertised_ip": ivr.advertised_ip,
            "local_port": ivr.local_port,
            "rtp_advertised_ip": ivr.rtp_advertised_ip,
            "rtp_port": ivr.rtp_port,
            "registered": "registered" in events,
        }
        print(json.dumps(report, indent=2))
        return 0
    finally:
        ivr.close()


def _build_acceptance_flow(prompt_audio: str, transfer_to: str | None) -> Flow:
    nodes = {
        1: Node(
            id=1,
            name="Acceptance Menu",
            node_type="menu",
            prompt_audio=prompt_audio,
            gather_timeout_seconds=10,
            max_retries=1,
            transitions={"1": 2, "2": 2, "3": 3 if transfer_to else 2},
        ),
        2: Node(id=2, name="Done", node_type="hangup"),
    }
    if transfer_to:
        nodes[3] = Node(id=3, name="Transfer", node_type="transfer", transfer_number=transfer_to)
    return Flow(id=0, root_node_id=1, nodes=nodes)


def run_full_call(
    sip_cfg: dict[str, str],
    target: str,
    prompt_audio: str,
    transfer_to: str | None,
    *,
    require_dtmf: bool = False,
    require_recording: bool = False,
) -> int:
    events: list[tuple[str, str, str]] = []
    ivr = IvrCall(
        target=target,
        flow=_build_acceptance_flow(prompt_audio, transfer_to),
        sip=sip_cfg,
        on_event=lambda e: events.append((e.type, e.digit, e.detail)),
        record=True,
    )
    res = ivr.run()
    event_types = {et for et, _, _ in events}
    rec_path = Path(res.recording_path) if res.recording_path else None
    rec_ok = bool(rec_path and rec_path.exists() and rec_path.stat().st_size > 0)
    dtmf_ok = res.digits_pressed > 0
    transfer_ok = (not transfer_to) or ("transfer" in event_types and ("transfer_accepted" in event_types or "bridge_completed" in event_types))
    checks = {
        "registration": "registered" in event_types,
        "outbound_call": "ringing" in event_types and "answered" in event_types,
        "audio_prompt": "prompt_played" in event_types,
        "dtmf_capture": dtmf_ok,
        "recording_saved": rec_ok,
        "hangup": "hangup" in event_types,
        "transfer": transfer_ok,
    }
    report = {
        "status": res.status,
        "sip_final_status": res.sip_final_status,
        "ring_seconds": res.ring_seconds,
        "talk_seconds": res.talk_seconds,
        "reached_terminal": res.reached_terminal,
        "digits_pressed": res.digits_pressed,
        "recording_path": res.recording_path,
        "checks": checks,
        "events": events,
        "error": res.error,
    }
    print(json.dumps(report, indent=2))
    passed = (
        res.status in ("completed", "canceled")
        and checks["registration"]
        and checks["outbound_call"]
        and checks["audio_prompt"]
        and checks["hangup"]
        and transfer_ok
        and (dtmf_ok or not require_dtmf)
        and (rec_ok or not require_recording)
    )
    return 0 if passed else 1


def run_mock_acceptance(transfer_to: str = "1001") -> int:
    """Self-contained localhost UDP IP-PBX mock rehearsal across all 7 acceptance criteria."""
    ensure_dirs()
    sip_srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sip_srv.bind(("127.0.0.1", 0))
    sip_port = sip_srv.getsockname()[1]
    sip_srv.settimeout(6.0)

    rtp_sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp_sink.bind(("127.0.0.1", 0))
    pbx_rtp_port = rtp_sink.getsockname()[1]
    rtp_sink.settimeout(0.5)

    stop_event = threading.Event()
    received_rtp_packets = [0]

    # Temporary valid 160-byte ulaw prompt for audio playback verification
    with tempfile.NamedTemporaryFile(dir="instance/sounds", suffix=".ulaw", delete=False) as tf:
        tf.write(b"\xff" * 480)
        prompt_name = Path(tf.name).stem

    def rtp_drain():
        while not stop_event.is_set():
            try:
                data, _ = rtp_sink.recvfrom(2048)
                if len(data) >= 12:
                    received_rtp_packets[0] += 1
            except OSError:
                pass

    def pbx_loop():
        caller_rtp_port = None
        dtmf_sent = False
        while not stop_event.is_set():
            try:
                data, addr = sip_srv.recvfrom(8192)
            except OSError:
                break
            req = data.decode(errors="replace")
            method = req.split(None, 1)[0].upper()
            if method == "REGISTER":
                if "Authorization:" not in req:
                    _reply(sip_srv, addr, req, "401 Unauthorized",
                           'WWW-Authenticate: Digest realm="ccall-mock", nonce="mocknonce1"\r\n')
                else:
                    _reply(sip_srv, addr, req, "200 OK")
            elif method == "INVITE":
                _, caller_rtp_port = _extract_sdp(req)
                _reply(sip_srv, addr, req, "180 Ringing")
                sdp = (
                    "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=MockPBX\r\n"
                    f"c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {pbx_rtp_port} RTP/AVP 0 101\r\n"
                    "a=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\n"
                )
                _reply(sip_srv, addr, req, "200 OK",
                       f'Contact: <sip:mock@127.0.0.1:{sip_port}>\r\nContent-Type: application/sdp\r\n',
                       body=sdp)
            elif method == "ACK" and caller_rtp_port and not dtmf_sent:
                dtmf_sent = True
                # Send DTMF digit 3 -> triggers transfer_to path
                threading.Thread(
                    target=_send_dtmf_and_audio,
                    args=("127.0.0.1", caller_rtp_port, 3, stop_event),
                    daemon=True,
                ).start()
            elif method == "REFER":
                _reply(sip_srv, addr, req, "202 Accepted")
            elif method == "BYE":
                _reply(sip_srv, addr, req, "200 OK")
                break

    t_rtp = threading.Thread(target=rtp_drain, daemon=True)
    t_pbx = threading.Thread(target=pbx_loop, daemon=True)
    t_rtp.start()
    t_pbx.start()

    existing_recs = set(Path("instance/recordings").glob("call-*.wav")) | set(Path("instance/recordings").glob("call-*.ulaw"))
    try:
        sip_cfg = {
            "SIP_DOMAIN": "127.0.0.1",
            "SIP_PORT": str(sip_port),
            "SIP_USER": "mock_user",
            "SIP_PASSWORD": "mock_password",
            "SIP_TRANSPORT": "udp",
            "SIP_BIND_IP": "127.0.0.1",
            "RTP_BIND_IP": "127.0.0.1",
            "SIP_ADVERTISED_IP": "127.0.0.1",
            "RTP_ADVERTISED_IP": "127.0.0.1",
        }
        rc = run_full_call(
            sip_cfg,
            target="01700000099",
            prompt_audio=prompt_name,
            transfer_to=transfer_to,
            require_dtmf=True,
            require_recording=True,
        )
        print(f"MOCK_PBX_RTP_PACKETS_RECEIVED={received_rtp_packets[0]}")
        return 0 if (rc == 0 and received_rtp_packets[0] > 0) else 1
    finally:
        stop_event.set()
        sip_srv.close()
        rtp_sink.close()
        t_pbx.join(timeout=2)
        t_rtp.join(timeout=2)
        try:
            (Path("instance/sounds") / f"{prompt_name}.ulaw").unlink(missing_ok=True)
        except OSError:
            pass
        for rec in (set(Path("instance/recordings").glob("call-*.wav")) | set(Path("instance/recordings").glob("call-*.ulaw"))) - existing_recs:
            try:
                rec.unlink(missing_ok=True)
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="C-Call V2 IP-PBX Acceptance Gate")
    parser.add_argument("--mode", choices=["diagnostic", "full-call", "mock-acceptance"], default="diagnostic")
    parser.add_argument("--confirm-authorized-test", action="store_true",
                        help="Explicitly confirm authorization before placing any live call")
    parser.add_argument("--target", help="Approved test phone number (required for full-call)")
    parser.add_argument("--prompt-audio", default="", help="Prompt .ulaw filename in instance/sounds")
    parser.add_argument("--transfer-to", default=None, help="Optional agent extension for DTMF 3 transfer")
    parser.add_argument("--require-dtmf", action="store_true", help="Fail if no DTMF digit captured")
    parser.add_argument("--require-recording", action="store_true", help="Fail if recording file missing/empty")
    args = parser.parse_args()

    if args.mode == "mock-acceptance":
        return run_mock_acceptance(args.transfer_to or "1001")

    required = ["SIP_DOMAIN", "SIP_USER", "SIP_PASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"BLOCKED: Missing SIP credentials in environment: {', '.join(missing)}")
        return 2

    sip_cfg = {
        "SIP_DOMAIN": os.environ["SIP_DOMAIN"],
        "SIP_PORT": os.environ.get("SIP_PORT", "5060"),
        "SIP_USER": os.environ["SIP_USER"],
        "SIP_PASSWORD": os.environ["SIP_PASSWORD"],
        "SIP_TRANSPORT": os.environ.get("SIP_TRANSPORT", "udp"),
    }
    for nat_key in ("SIP_BIND_IP", "RTP_BIND_IP", "SIP_ADVERTISED_IP", "RTP_ADVERTISED_IP"):
        if os.environ.get(nat_key):
            sip_cfg[nat_key] = os.environ[nat_key]

    if args.mode == "diagnostic":
        return run_diagnostic(sip_cfg)

    if not args.confirm_authorized_test or not args.target:
        print("BLOCKED: --mode full-call requires both --confirm-authorized-test and --target <approved_number>")
        return 2

    return run_full_call(
        sip_cfg,
        args.target,
        args.prompt_audio,
        args.transfer_to,
        require_dtmf=args.require_dtmf,
        require_recording=args.require_recording,
    )


if __name__ == "__main__":
    sys.exit(main())
