#!/usr/bin/env python3
"""
Staging-only mock webhook receiver for Alertmanager notifications.

Records all incoming alerts (name, status, timestamps, labels, annotations)
to /tmp/alerts.jsonl (newline-delimited JSON) for persistence.
No secrets, no PII, no real notification forwarding.

GET /alerts   - list all received alert notifications
GET /health   - liveness check
POST /webhook - Alertmanager webhook target
DELETE /reset - clear received alerts (truncates the log file)
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

ALERTS_FILE = "/tmp/alerts.jsonl"
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_alerts(records: list[dict]) -> None:
    with _lock:
        with open(ALERTS_FILE, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")


def _read_alerts() -> list[dict]:
    if not os.path.exists(ALERTS_FILE):
        return []
    with _lock:
        alerts = []
        with open(ALERTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        alerts.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return alerts


def _clear_alerts() -> int:
    with _lock:
        count = 0
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r", encoding="utf-8") as f:
                count = sum(1 for line in f if line.strip())
            open(ALERTS_FILE, "w").close()
        return count


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[mock_receiver] {fmt % args}", flush=True)

    def _send_json(self, status: int, body: object) -> None:
        payload = json.dumps(body, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            alerts = _read_alerts()
            self._send_json(200, {"status": "ok", "received_count": len(alerts)})
        elif self.path == "/alerts":
            self._send_json(200, {"alerts": _read_alerts()})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/webhook":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return

        received_at = _now()
        records = []
        for alert in payload.get("alerts", []):
            record = {
                "received_at": received_at,
                "alert_name": alert.get("labels", {}).get("alertname", "unknown"),
                "status": alert.get("status", "unknown"),
                "starts_at": alert.get("startsAt"),
                "ends_at": alert.get("endsAt"),
                "labels": alert.get("labels", {}),
                "annotations": alert.get("annotations", {}),
            }
            records.append(record)

        _append_alerts(records)

        print(
            f"[mock_receiver] webhook received {len(records)} alert(s): "
            + ", ".join(f"{r['alert_name']}={r['status']}" for r in records),
            flush=True,
        )
        self._send_json(200, {"accepted": len(records)})

    def do_DELETE(self):
        if self.path != "/reset":
            self._send_json(404, {"error": "not found"})
            return
        count = _clear_alerts()
        self._send_json(200, {"cleared": count})


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    # Ensure alerts file exists
    if not os.path.exists(ALERTS_FILE):
        open(ALERTS_FILE, "w").close()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    print("[mock_receiver] listening on :8080 (file-backed, threading)", flush=True)
    server.serve_forever()
