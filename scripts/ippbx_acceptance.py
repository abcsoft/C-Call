#!/usr/bin/env python3
"""Authorized IP-PBX Acceptance Test Harness (C-Call V2).

Default mode (`--mode diagnostic`) performs ONLY DNS resolution, UDP SIP OPTIONS,
and REGISTER authentication — zero calls initiated.

Live call verification (`--mode full-call`) is strictly gated behind BOTH
`--confirm-authorized-test` AND `--target <approved_number>`.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from call_engine.ivr_engine import Flow, IvrCall, Node


def run_diagnostic(sip_cfg: dict[str, str]) -> int:
    events: list[str] = []
    flow = Flow(id=0, root_node_id=1, nodes={1: Node(id=1, name="Diag", node_type="hangup")})
    ivr = IvrCall(target="diagnostic", flow=flow, sip=sip_cfg, on_event=lambda e: events.append(e.type))
    try:
        ivr._register()
        print(f"PASS: SIP REGISTER succeeded via {sip_cfg['SIP_DOMAIN']}:{sip_cfg['SIP_PORT']} "
              f"(advertised={ivr.advertised_ip}:{ivr.local_port})")
        return 0
    finally:
        ivr.sock.close()
        ivr.rtp.close()


def run_full_call(sip_cfg: dict[str, str], target: str, prompt_audio: str, transfer_to: str | None) -> int:
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

    events = []
    ivr = IvrCall(
        target=target,
        flow=Flow(id=0, root_node_id=1, nodes=nodes),
        sip=sip_cfg,
        on_event=lambda e: events.append((e.type, e.digit, e.detail)),
        record=True,
    )
    res = ivr.run()
    print(f"STATUS={res.status} SIP={res.sip_final_status} RING={res.ring_seconds}s TALK={res.talk_seconds}s "
          f"TERMINAL={res.reached_terminal} DIGITS={res.digits_pressed} EVENTS={events}")
    return 0 if res.status in ("completed", "canceled") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="C-Call V2 IP-PBX Acceptance Gate")
    parser.add_argument("--mode", choices=["diagnostic", "full-call"], default="diagnostic")
    parser.add_argument("--confirm-authorized-test", action="store_true",
                        help="Explicitly confirm authorization before placing any live call")
    parser.add_argument("--target", help="Approved test phone number (required for full-call)")
    parser.add_argument("--prompt-audio", default="", help="Prompt .ulaw filename in instance/sounds")
    parser.add_argument("--transfer-to", default=None, help="Optional agent extension for DTMF 3 transfer")
    args = parser.parse_args()

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

    if args.mode == "diagnostic":
        return run_diagnostic(sip_cfg)

    if not args.confirm_authorized_test or not args.target:
        print("BLOCKED: --mode full-call requires both --confirm-authorized-test and --target <approved_number>")
        return 2

    return run_full_call(sip_cfg, args.target, args.prompt_audio, args.transfer_to)


if __name__ == "__main__":
    sys.exit(main())
