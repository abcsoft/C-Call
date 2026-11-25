from __future__ import annotations

import hashlib
import os
import random
import re
import socket
import time
import uuid
from typing import Any
from cryptography.fernet import Fernet
from app.db import db

# Static fallback Fernet key (dev/testing only; production fails closed)
FALLBACK_KEY = "WKUchdJq2Ur7OjSn9xkt1qfzYSxo1FlsTwtfjS71wwQ="

def _get_fernet() -> Fernet:
    key = os.environ.get("SIP_PASSWORD_ENCRYPTION_KEY", "").strip()
    is_prod = os.environ.get("FLASK_ENV", "production") == "production"
    if not key:
        if is_prod:
            raise RuntimeError("SIP_PASSWORD_ENCRYPTION_KEY is required in production.")
        key = FALLBACK_KEY
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:
        if is_prod:
            raise RuntimeError("Invalid SIP_PASSWORD_ENCRYPTION_KEY in production.") from exc
        return Fernet(FALLBACK_KEY.encode("utf-8"))

def encrypt_password(password: str) -> str:
    f = _get_fernet()
    return f.encrypt(password.encode("utf-8")).decode("utf-8")

def decrypt_password(encrypted_password: str) -> str:
    f = _get_fernet()
    try:
        return f.decrypt(encrypted_password.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""

_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def _validate_provider_fields(
    name: str,
    sip_domain: str,
    sip_port: int | str,
    sip_user: str,
    max_channels: int | str = 10,
) -> tuple[str, str, int, str, int]:
    clean_name = (name or "").strip()
    clean_domain = (sip_domain or "").strip()
    clean_user = (sip_user or "").strip()

    if not clean_name:
        raise ValueError("Provider name is required.")
    if not clean_domain or not _DOMAIN_RE.match(clean_domain) or ".." in clean_domain:
        raise ValueError("Invalid SIP domain or IP address.")
    try:
        port_int = int(sip_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("SIP port must be an integer between 1 and 65535.") from exc
    if not (1 <= port_int <= 65535):
        raise ValueError("SIP port must be between 1 and 65535.")
    if not clean_user:
        raise ValueError("SIP username is required.")
    try:
        channels_int = int(max_channels)
    except (TypeError, ValueError) as exc:
        raise ValueError("Max channels must be an integer between 1 and 5000.") from exc
    if not (1 <= channels_int <= 5000):
        raise ValueError("Max channels must be between 1 and 5000.")

    return clean_name, clean_domain, port_int, clean_user, channels_int


def _ensure_unique_provider(
    name: str,
    sip_domain: str,
    sip_port: int,
    sip_user: str,
    exclude_id: int | None = None,
) -> None:
    if exclude_id is None:
        dup_name = db.query_one(
            "SELECT id FROM sip_providers WHERE LOWER(name) = LOWER(%s) LIMIT 1",
            (name,),
        )
        dup_endpoint = db.query_one(
            """SELECT id FROM sip_providers
               WHERE LOWER(sip_domain) = LOWER(%s) AND sip_port = %s AND sip_user = %s
               LIMIT 1""",
            (sip_domain, sip_port, sip_user),
        )
    else:
        dup_name = db.query_one(
            "SELECT id FROM sip_providers WHERE LOWER(name) = LOWER(%s) AND id <> %s LIMIT 1",
            (name, exclude_id),
        )
        dup_endpoint = db.query_one(
            """SELECT id FROM sip_providers
               WHERE LOWER(sip_domain) = LOWER(%s) AND sip_port = %s AND sip_user = %s AND id <> %s
               LIMIT 1""",
            (sip_domain, sip_port, sip_user, exclude_id),
        )
    if dup_name:
        raise ValueError(f"A SIP provider named '{name}' already exists.")
    if dup_endpoint:
        raise ValueError("A SIP provider with the same domain, port, and username already exists.")


def add_sip_provider(
    name: str,
    sip_domain: str,
    sip_port: int,
    sip_user: str,
    sip_password_plaintext: str,
    max_channels: int = 10,
) -> int:
    clean_name, clean_domain, port_int, clean_user, channels_int = _validate_provider_fields(
        name, sip_domain, sip_port, sip_user, max_channels
    )
    if not (sip_password_plaintext or "").strip():
        raise ValueError("SIP password is required.")
    _ensure_unique_provider(clean_name, clean_domain, port_int, clean_user)
    encrypted_pw = encrypt_password(sip_password_plaintext)
    return db.insert_returning_id(
        """
        INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password, status, max_channels)
        VALUES (%s, %s, %s, %s, %s, 'active', %s)
        RETURNING id
        """,
        (clean_name, clean_domain, port_int, clean_user, encrypted_pw, channels_int),
    )

def get_sip_provider_by_id(provider_id: int) -> dict[str, Any] | None:
    provider = db.query_one("SELECT * FROM sip_providers WHERE id = %s", (provider_id,))
    if provider and provider.get("sip_password"):
        provider["sip_password_decrypted"] = decrypt_password(provider["sip_password"])
    return provider

def get_all_sip_providers() -> list[dict[str, Any]]:
    providers = db.query("SELECT * FROM sip_providers ORDER BY name ASC")
    for provider in providers:
        if provider.get("sip_password"):
            provider["sip_password_decrypted"] = decrypt_password(provider["sip_password"])
    return providers

def update_sip_provider(
    provider_id: int,
    name: str,
    sip_domain: str,
    sip_port: int,
    sip_user: str,
    sip_password_plaintext: str | None = None,
    max_channels: int | None = None,
) -> None:
    existing = db.query_one("SELECT id, max_channels FROM sip_providers WHERE id = %s", (provider_id,))
    if not existing:
        raise ValueError("SIP provider not found.")
    effective_channels = existing.get("max_channels") or 10 if max_channels is None else max_channels
    clean_name, clean_domain, port_int, clean_user, channels_int = _validate_provider_fields(
        name, sip_domain, sip_port, sip_user, effective_channels
    )
    _ensure_unique_provider(clean_name, clean_domain, port_int, clean_user, exclude_id=provider_id)
    if sip_password_plaintext:
        encrypted_pw = encrypt_password(sip_password_plaintext)
        db.execute(
            """
            UPDATE sip_providers
            SET name = %s, sip_domain = %s, sip_port = %s, sip_user = %s, sip_password = %s, max_channels = %s
            WHERE id = %s
            """,
            (clean_name, clean_domain, port_int, clean_user, encrypted_pw, channels_int, provider_id),
        )
    else:
        db.execute(
            """
            UPDATE sip_providers
            SET name = %s, sip_domain = %s, sip_port = %s, sip_user = %s, max_channels = %s
            WHERE id = %s
            """,
            (clean_name, clean_domain, port_int, clean_user, channels_int, provider_id),
        )

def update_sip_provider_status(provider_id: int, status: str) -> None:
    if status not in ("active", "inactive", "maintenance", "disabled"):
        raise ValueError("Invalid provider status.")
    db.execute("UPDATE sip_providers SET status = %s WHERE id = %s", (status, provider_id))

def delete_sip_provider(provider_id: int) -> None:
    linked_numbers = db.query_one(
        "SELECT COUNT(*) AS n FROM phone_numbers WHERE provider_id = %s",
        (provider_id,),
    )
    if linked_numbers and int(linked_numbers["n"]) > 0:
        raise ValueError("Cannot delete provider while phone numbers are linked to it. Reassign or delete those numbers first.")
    db.execute("DELETE FROM sip_providers WHERE id = %s", (provider_id,))


def _live_registry_call_ids() -> list[int]:
    try:
        from call_engine import active_calls as registry
        entries = registry.list_active_calls(tenant_id=None)
        return [int(e["call_id"]) for e in entries if e.get("call_id")]
    except Exception:
        return []


def get_provider_call_counts(provider_id: int, stale_minutes: int = 60) -> dict[str, int]:
    """Return accurate active, stale-reconcilable, and total call counts for a SIP provider."""
    mins = max(1, int(stale_minutes))
    live_ids = _live_registry_call_ids()
    live_filter = "AND NOT (id = ANY(%s))" if live_ids else ""
    stale_params: tuple[Any, ...] = (
        (provider_id, provider_id, str(mins), live_ids)
        if live_ids
        else (provider_id, provider_id, str(mins))
    )

    active_row = db.query_one(
        """SELECT COUNT(*) AS n FROM calls
           WHERE (sip_gateway_id = %s OR caller_id IN (SELECT number FROM phone_numbers WHERE provider_id = %s))
             AND status IN ('queued', 'initiated', 'preparing', 'dialing', 'calling', 'ringing', 'answered')""",
        (provider_id, provider_id),
    )
    stale_row = db.query_one(
        f"""SELECT COUNT(*) AS n FROM calls
            WHERE (sip_gateway_id = %s OR caller_id IN (SELECT number FROM phone_numbers WHERE provider_id = %s))
              AND status IN ('queued', 'initiated', 'preparing', 'dialing', 'calling', 'ringing', 'answered')
              AND started_at IS NOT NULL
              AND started_at < now() - (%s || ' minutes')::interval
              AND (answered_at IS NULL OR answered_at < now() - interval '30 minutes')
              AND NOT EXISTS (
                  SELECT 1 FROM call_jobs cj
                  WHERE cj.call_id = calls.id AND cj.lease_expires_at IS NOT NULL AND cj.lease_expires_at > now()
              )
              {live_filter}""",
        stale_params,
    )
    total_row = db.query_one(
        """SELECT COUNT(*) AS n FROM calls
           WHERE (sip_gateway_id = %s OR caller_id IN (SELECT number FROM phone_numbers WHERE provider_id = %s))""",
        (provider_id, provider_id),
    )
    return {
        "active_calls": int(active_row["n"]) if active_row else 0,
        "stale_calls": int(stale_row["n"]) if stale_row else 0,
        "total_calls": int(total_row["n"]) if total_row else 0,
    }


def reconcile_stale_calls(provider_id: int | None = None, stale_minutes: int = 60) -> int:
    """Close abandoned active calls older than ``stale_minutes`` and sync call_jobs.

    Never releases genuinely active calls tracked in live Redis/in-memory registry,
    calls with an active worker lease (call_jobs.lease_expires_at > now()), or calls
    answered within the last 30 minutes.
    """
    mins = max(1, int(stale_minutes))
    live_ids = _live_registry_call_ids()
    live_filter = "AND NOT (id = ANY(%s))" if live_ids else ""

    with db.transaction() as conn:
        if provider_id is not None:
            params: tuple[Any, ...] = (
                (str(mins), provider_id, provider_id, live_ids)
                if live_ids
                else (str(mins), provider_id, provider_id)
            )
            rows = conn.execute(
                f"""
                UPDATE calls
                SET status = 'failed',
                    ended_at = COALESCE(ended_at, now())
                WHERE status IN ('queued', 'initiated', 'preparing', 'dialing', 'calling', 'ringing', 'answered')
                  AND started_at IS NOT NULL
                  AND started_at < now() - (%s || ' minutes')::interval
                  AND (answered_at IS NULL OR answered_at < now() - interval '30 minutes')
                  AND NOT EXISTS (
                      SELECT 1 FROM call_jobs cj
                      WHERE cj.call_id = calls.id AND cj.lease_expires_at IS NOT NULL AND cj.lease_expires_at > now()
                  )
                  AND (
                    sip_gateway_id = %s
                    OR caller_id IN (SELECT number FROM phone_numbers WHERE provider_id = %s)
                  )
                  {live_filter}
                RETURNING id
                """,
                params,
            ).fetchall()
        else:
            params = (str(mins), live_ids) if live_ids else (str(mins),)
            rows = conn.execute(
                f"""
                UPDATE calls
                SET status = 'failed',
                    ended_at = COALESCE(ended_at, now())
                WHERE status IN ('queued', 'initiated', 'preparing', 'dialing', 'calling', 'ringing', 'answered')
                  AND started_at IS NOT NULL
                  AND started_at < now() - (%s || ' minutes')::interval
                  AND (answered_at IS NULL OR answered_at < now() - interval '30 minutes')
                  AND NOT EXISTS (
                      SELECT 1 FROM call_jobs cj
                      WHERE cj.call_id = calls.id AND cj.lease_expires_at IS NOT NULL AND cj.lease_expires_at > now()
                  )
                  {live_filter}
                RETURNING id
                """,
                params,
            ).fetchall()
        call_ids = [int(r["id"]) for r in (rows or [])]
        if call_ids:
            conn.execute(
                """
                UPDATE call_jobs
                SET call_state = 'failed', updated_at = now()
                WHERE call_id = ANY(%s)
                  AND call_state IN ('queued', 'preparing', 'dialing', 'ringing', 'answered')
                """,
                (call_ids,),
            )
    return len(call_ids)


def get_all_providers() -> list[dict[str, Any]]:
    return get_all_sip_providers()

def create_provider(data: dict[str, Any]) -> int:
    return add_sip_provider(
        name=data["name"],
        sip_domain=data["sip_domain"],
        sip_port=int(data.get("sip_port") or 5060),
        sip_user=data["sip_user"],
        sip_password_plaintext=data["sip_password"],
        max_channels=int(data.get("max_channels") or 10),
    )

def update_provider(provider_id: int, data: dict[str, Any]) -> None:
    raw_ch = data.get("max_channels")
    update_sip_provider(
        provider_id=provider_id,
        name=data["name"],
        sip_domain=data["sip_domain"],
        sip_port=int(data.get("sip_port") or 5060),
        sip_user=data["sip_user"],
        sip_password_plaintext=data.get("sip_password"),
        max_channels=int(raw_ch) if raw_ch not in (None, "") else None,
    )


def _parse_sip_status(response_text: str) -> tuple[int | None, str]:
    first_line = (response_text or "").split("\r\n", 1)[0].strip()
    m = re.match(r"^SIP/2\.0\s+(\d{3})\b(.*)$", first_line)
    if not m:
        return None, first_line
    return int(m.group(1)), first_line


def _parse_digest_challenge(response_text: str) -> tuple[dict[str, str] | None, str]:
    match = re.search(r"(WWW|Proxy)-Authenticate:\s*Digest\s+([^\r\n]+)", response_text or "", re.I)
    if not match:
        return None, "Authorization"
    header_type = "Proxy-Authorization" if match.group(1).lower() == "proxy" else "Authorization"
    params: dict[str, str] = {}
    for item in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', match.group(2)):
        params[item.group(1).lower()] = item.group(2) if item.group(2) is not None else (item.group(3) or "")
    return params, header_type


def _build_digest_header(
    params: dict[str, str],
    username: str,
    password: str,
    method: str,
    uri: str,
) -> str:
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode("utf-8")).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode("utf-8")).hexdigest()
    qop = params.get("qop", "")
    qop_options = [q.strip().lower() for q in qop.split(",") if q.strip()]
    if "auth" in qop_options:
        nc = "00000001"
        cnonce = "".join(random.choices("0123456789abcdef", k=8))
        resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode("utf-8")).hexdigest()
        parts = [
            f'Digest username="{username}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'response="{resp}"',
            "algorithm=MD5",
            f'qop=auth',
            f'nc={nc}',
            f'cnonce="{cnonce}"',
        ]
    else:
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode("utf-8")).hexdigest()
        parts = [
            f'Digest username="{username}"',
            f'realm="{realm}"',
            f'nonce="{nonce}"',
            f'uri="{uri}"',
            f'response="{resp}"',
            "algorithm=MD5",
        ]
    if params.get("opaque"):
        parts.append(f'opaque="{params["opaque"]}"')
    return ", ".join(parts)


def _recv_matching_sip(sock: socket.socket, call_id: str, cseq_num: int, method: str, timeout: float) -> str | None:
    deadline = time.perf_counter() + timeout
    pattern = re.compile(rf"^CSeq:\s*{cseq_num}\s+{re.escape(method)}\b", re.I | re.M)
    cid_pattern = re.compile(rf"^Call-ID:\s*{re.escape(call_id)}\b", re.I | re.M)
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            return None
        text = data.decode("utf-8", errors="ignore")
        if text.startswith("SIP/2.0") and cid_pattern.search(text) and pattern.search(text):
            return text


def _send_and_recv_sip(
    sock: socket.socket,
    addr: tuple[str, int],
    payload: str,
    call_id: str,
    cseq_num: int,
    method: str,
    timeout: float,
    attempts: int | None = None,
) -> tuple[str | None, int | None]:
    """Send a UDP SIP diagnostic datagram with limited retransmit on timeout."""
    max_attempts = attempts if attempts is not None else max(1, int(os.environ.get("SIP_DIAGNOSTIC_UDP_RETRIES", "2")))
    encoded = payload.encode("utf-8")
    for _ in range(max_attempts):
        t0 = time.perf_counter()
        sock.sendto(encoded, addr)
        reply = _recv_matching_sip(sock, call_id, cseq_num, method, timeout)
        if reply is not None:
            return reply, int((time.perf_counter() - t0) * 1000)
    return None, None


def test_connection(provider_id: int) -> dict[str, Any]:
    """Run non-calling, non-disruptive SIP trunk diagnostics.

    Uses DNS lookup, UDP OPTIONS probe, and an RFC 3261 binding-query REGISTER
    (omitting Contact/Expires so active trunk registrations and active calls are
    never modified or disrupted). Never places INVITE calls or exposes secrets.
    """
    caveat = "Diagnostic only (DNS + OPTIONS + non-modifying RFC 3261 REGISTER query). No outbound call placed; existing active calls and SIP bindings left untouched."
    provider = get_sip_provider_by_id(provider_id)
    if not provider:
        return {
            "success": False,
            "message": "SIP provider not found.",
            "latency_ms": 0,
            "provider_id": provider_id,
            "dns_ok": False,
            "resolved_ip": None,
            "udp_reachable": False,
            "options_status": "skipped",
            "options_status_code": None,
            "options_status_line": "",
            "auth_challenge": False,
            "auth_realm": None,
            "register_status": "skipped",
            "register_status_code": None,
            "register_status_line": "",
            "provider_readiness": "not_found",
            "readiness_label": "Provider Missing",
            "outbound_call_verified": False,
            "caveat": caveat,
            "explanation": "The requested SIP provider record does not exist.",
            "suggested_action": "Refresh the providers page and verify the trunk ID.",
            "steps": [{"key": "lookup", "label": "Provider Record", "status": "fail", "detail": "Not found"}],
        }

    domain = (provider.get("sip_domain") or "").strip()
    port = int(provider.get("sip_port") or 5060)
    sip_user = (provider.get("sip_user") or "").strip()
    sip_password = provider.get("sip_password_decrypted") or ""
    timeout_s = float(os.environ.get("SIP_DIAGNOSTIC_TIMEOUT_SECONDS", "2.5"))

    steps: list[dict[str, str]] = []
    start_time = time.perf_counter()

    # 1. DNS Resolution
    try:
        ip = socket.gethostbyname(domain)
        steps.append({"key": "dns", "label": "DNS / Host Resolution", "status": "pass", "detail": f"{domain} -> {ip}"})
    except Exception as exc:
        return {
            "success": False,
            "message": f"DNS resolution failed for {domain}: {exc}",
            "latency_ms": int((time.perf_counter() - start_time) * 1000),
            "provider_id": provider_id,
            "provider_name": provider.get("name"),
            "domain": domain,
            "port": port,
            "dns_ok": False,
            "resolved_ip": None,
            "udp_reachable": False,
            "options_status": "skipped",
            "options_status_code": None,
            "options_status_line": "",
            "auth_challenge": False,
            "auth_realm": None,
            "register_status": "skipped",
            "register_status_code": None,
            "register_status_line": "",
            "provider_readiness": "dns_failed",
            "readiness_label": "DNS Failed",
            "outbound_call_verified": False,
            "caveat": caveat,
            "explanation": f"Could not resolve SIP host '{domain}'.",
            "suggested_action": "Check spelling of SIP domain/IP or container DNS settings.",
            "steps": [{"key": "dns", "label": "DNS / Host Resolution", "status": "fail", "detail": str(exc)}],
        }

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            temp_sock.connect((ip, port))
            local_ip = temp_sock.getsockname()[0]
            temp_sock.close()
        except Exception:
            local_ip = "127.0.0.1"

        advertised_ip = (
            os.environ.get("SIP_ADVERTISED_IP")
            or os.environ.get("PUBLIC_IP")
            or os.environ.get("EC2_PUBLIC_IP")
            or local_ip
        ).strip()
        bind_ip = os.environ.get("SIP_BIND_IP", "0.0.0.0").strip()
        local_port = None
        for _ in range(5):
            candidate = random.randint(10000, 20000)
            try:
                sock.bind((bind_ip, candidate))
                local_port = candidate
                break
            except OSError:
                continue
        if local_port is None:
            sock.bind((bind_ip, 0))
            local_port = sock.getsockname()[1]

        udp_reachable = False
        options_status = "timeout"
        options_code: int | None = None
        options_line = ""
        first_rtt_ms: int | None = None

        # 2. SIP OPTIONS Probe
        opt_call_id = str(uuid.uuid4())
        opt_branch = "z9hG4bK" + "".join(random.choices("0123456789abcdef", k=10))
        opt_tag = "".join(random.choices("0123456789abcdef", k=8))
        options_req = (
            f"OPTIONS sip:{domain}:{port} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {advertised_ip}:{local_port};rport;branch={opt_branch}\r\n"
            "Max-Forwards: 70\r\n"
            f"To: <sip:{domain}:{port}>\r\n"
            f"From: <sip:{sip_user or 'ccall'}@{domain}>;tag={opt_tag}\r\n"
            f"Call-ID: {opt_call_id}\r\n"
            "CSeq: 1 OPTIONS\r\n"
            f"Contact: <sip:{sip_user or 'ccall'}@{advertised_ip}:{local_port}>\r\n"
            "Accept: application/sdp\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        opt_reply, opt_rtt_ms = _send_and_recv_sip(sock, (ip, port), options_req, opt_call_id, 1, "OPTIONS", timeout_s)
        if opt_reply:
            udp_reachable = True
            first_rtt_ms = opt_rtt_ms
            options_code, options_line = _parse_sip_status(opt_reply)
            if options_code == 200:
                options_status = "ok"
                steps.append({"key": "options", "label": "SIP OPTIONS Ping", "status": "pass", "detail": options_line})
            elif options_code in (401, 407):
                options_status = "auth_challenge"
                steps.append({"key": "options", "label": "SIP OPTIONS Ping", "status": "warn", "detail": f"{options_line} (auth required)"})
            elif options_code == 405:
                options_status = "method_not_allowed"
                steps.append({"key": "options", "label": "SIP OPTIONS Ping", "status": "warn", "detail": f"{options_line} (server alive, OPTIONS disabled)"})
            else:
                options_status = "rejected"
                steps.append({"key": "options", "label": "SIP OPTIONS Ping", "status": "fail", "detail": options_line or "Unexpected SIP response"})
        else:
            steps.append({"key": "options", "label": "SIP OPTIONS Ping", "status": "warn", "detail": f"No UDP reply within {timeout_s:.1f}s on port {port}"})

        # 3. SIP REGISTER + Digest Auth Challenge Probe (zero calls placed)
        reg_call_id = f"diagreg-{uuid.uuid4().hex[:16]}@{advertised_ip}"
        reg_tag = "".join(random.choices("0123456789abcdef", k=8))
        reg_uri = f"sip:{domain}"

        def _build_register(cseq_num: int, auth_hdr_name: str | None = None, auth_value: str | None = None) -> str:
            br = "z9hG4bK" + "".join(random.choices("0123456789abcdef", k=10))
            # RFC 3261 binding-query REGISTER: omit Contact & Expires so existing active trunk registrations are untouched.
            msg = (
                f"REGISTER {reg_uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {advertised_ip}:{local_port};rport;branch={br}\r\n"
                "Max-Forwards: 70\r\n"
                f"From: <sip:{sip_user}@{domain}>;tag={reg_tag}\r\n"
                f"To: <sip:{sip_user}@{domain}>\r\n"
                f"Call-ID: {reg_call_id}\r\n"
                f"CSeq: {cseq_num} REGISTER\r\n"
                "User-Agent: C-Call/2.0\r\n"
            )
            if auth_hdr_name and auth_value:
                msg += f"{auth_hdr_name}: {auth_value}\r\n"
            return msg + "Content-Length: 0\r\n\r\n"

        auth_challenge = False
        auth_realm: str | None = None
        register_status = "timeout"
        register_code: int | None = None
        register_line = ""
        active_bindings_count = 0

        reg_reply, reg_rtt_ms = _send_and_recv_sip(sock, (ip, port), _build_register(1), reg_call_id, 1, "REGISTER", timeout_s)
        final_reg_reply = reg_reply
        if reg_reply:
            udp_reachable = True
            if first_rtt_ms is None:
                first_rtt_ms = reg_rtt_ms
            register_code, register_line = _parse_sip_status(reg_reply)

            if register_code in (401, 407):
                challenge_params, hdr_name = _parse_digest_challenge(reg_reply)
                if challenge_params:
                    auth_challenge = True
                    auth_realm = challenge_params.get("realm")
                    steps.append({
                        "key": "challenge",
                        "label": "SIP Digest Challenge",
                        "status": "pass",
                        "detail": f"{register_line} (realm={auth_realm or 'default'})",
                    })
                    auth_val = _build_digest_header(challenge_params, sip_user, sip_password, "REGISTER", reg_uri)
                    reg_reply_2, _ = _send_and_recv_sip(
                        sock,
                        (ip, port),
                        _build_register(2, hdr_name, auth_val),
                        reg_call_id,
                        2,
                        "REGISTER",
                        timeout_s,
                    )
                    final_reg_reply = reg_reply_2
                    if reg_reply_2:
                        register_code, register_line = _parse_sip_status(reg_reply_2)
                    else:
                        register_code = None
                        register_line = ""
                else:
                    steps.append({
                        "key": "challenge",
                        "label": "SIP Digest Challenge",
                        "status": "fail",
                        "detail": f"{register_line} missing Digest challenge parameters",
                    })

        if register_code == 200 and final_reg_reply:
            active_bindings_count = sum(
                1 for ln in final_reg_reply.split("\r\n") if ln.lower().startswith("contact:")
            )

        if register_code == 200:
            register_status = "registered"
            steps.append({"key": "register", "label": "SIP REGISTER Auth", "status": "pass", "detail": register_line})
        elif register_code in (401, 407):
            register_status = "auth_failed"
            steps.append({"key": "register", "label": "SIP REGISTER Auth", "status": "fail", "detail": register_line})
        elif register_code == 403:
            register_status = "forbidden"
            steps.append({"key": "register", "label": "SIP REGISTER Auth", "status": "fail", "detail": register_line})
        elif register_code in (404, 484):
            register_status = "not_found"
            steps.append({"key": "register", "label": "SIP REGISTER Auth", "status": "fail", "detail": register_line})
        elif register_code is not None and register_code >= 500:
            register_status = "server_error"
            steps.append({"key": "register", "label": "SIP REGISTER Auth", "status": "fail", "detail": register_line})
        elif register_code is not None:
            register_status = "rejected"
            steps.append({"key": "register", "label": "SIP REGISTER Auth", "status": "fail", "detail": register_line})
        else:
            register_status = "timeout"
            steps.append({"key": "register", "label": "SIP REGISTER Auth", "status": "fail", "detail": f"REGISTER timed out ({timeout_s:.1f}s)"})

        steps.insert(1, {
            "key": "udp",
            "label": "UDP Socket Reachability",
            "status": "pass" if udp_reachable else "fail",
            "detail": f"UDP packet response received from {ip}:{port}" if udp_reachable else f"No UDP response from {ip}:{port}",
        })

        latency_ms = first_rtt_ms if first_rtt_ms is not None else int((time.perf_counter() - start_time) * 1000)

        if register_code == 200:
            success = True
            readiness = "registered"
            if options_code and options_code != 200:
                readiness_label = f"REGISTER 200 OK (OPTIONS {options_code})"
            else:
                readiness_label = "SIP Trunk Registered"
            explanation = (
                f"DNS resolved to {ip}, UDP signaling is reachable on port {port}, and SIP REGISTER query succeeded ({register_line})."
            )
            if options_code and options_code != 200:
                explanation += f" Note: OPTIONS probe returned {options_line}, which many IP-PBX trunks return when no extension is targeted, while REGISTER works."
            suggested_action = "Assign a pool number to this trunk and place an authorized test call before live traffic."
            summary_msg = f"Registered OK ({register_line})"
        elif not udp_reachable:
            success = False
            readiness = "unreachable"
            readiness_label = "Unreachable (Timeout)"
            explanation = f"DNS resolved {domain} to {ip}, but no UDP SIP response arrived from {ip}:{port}."
            suggested_action = "Verify firewall/UDP port forwarding, NAT SIP_ADVERTISED_IP, and provider SIP port."
            summary_msg = f"No UDP SIP response from {ip}:{port}."
        elif register_code in (401, 403, 407):
            success = False
            readiness = "auth_failed"
            readiness_label = "Authentication Rejected"
            explanation = f"SIP server responded ({register_line}), rejecting credentials or IP authorization for user '{sip_user}'."
            suggested_action = "Verify SIP username, password, domain/realm, and provider IP allowlist."
            summary_msg = f"SIP authentication failed: {register_line}"
        elif options_code == 404 or register_code in (404, 484):
            success = False
            readiness = "sip_not_found"
            readiness_label = "SIP 404 Not Found"
            failed_line = register_line or options_line
            explanation = f"SIP server answered at UDP level, but rejected the SIP URI/account with {failed_line}."
            suggested_action = "Verify SIP domain, outbound proxy port, trunk username, and registrar host."
            summary_msg = f"SIP rejected probe: {failed_line}"
        elif (options_code and options_code >= 500) or (register_code and register_code >= 500):
            success = False
            readiness = "server_error"
            readiness_label = "SIP Server Error"
            failed_line = register_line or options_line
            explanation = f"Upstream SIP server returned a server error ({failed_line})."
            suggested_action = "Inspect carrier trunk health or retry shortly."
            summary_msg = f"SIP server error: {failed_line}"
        elif options_code == 200 and register_code is None:
            success = False
            readiness = "options_only_no_register"
            readiness_label = "OPTIONS OK / No REGISTER Reply"
            explanation = f"OPTIONS returned {options_line}, but REGISTER received no reply (IP-authenticated trunk or REGISTER blocked)."
            suggested_action = "Confirm whether this trunk uses IP authentication without SIP REGISTER before sending live calls."
            summary_msg = f"OPTIONS {options_line}, REGISTER timed out"
        else:
            success = False
            readiness = "rejected"
            readiness_label = "Diagnostic Failed"
            detail_line = register_line or options_line or "Unknown SIP response"
            explanation = f"SIP diagnostic did not complete registration ({detail_line})."
            suggested_action = "Check trunk host, port, and SIP server logs."
            summary_msg = f"SIP diagnostic warning: {detail_line}"

        return {
            "success": success,
            "message": summary_msg,
            "latency_ms": latency_ms,
            "provider_id": provider_id,
            "provider_name": provider.get("name"),
            "domain": domain,
            "port": port,
            "dns_ok": True,
            "resolved_ip": ip,
            "udp_reachable": udp_reachable,
            "options_status": options_status,
            "options_status_code": options_code,
            "options_status_line": options_line,
            "auth_challenge": auth_challenge,
            "auth_realm": auth_realm,
            "register_status": register_status,
            "register_status_code": register_code,
            "register_status_line": register_line,
            "provider_readiness": readiness,
            "readiness_label": readiness_label,
            "outbound_call_verified": False,
            "registration_non_disruptive": True,
            "active_bindings_count": active_bindings_count,
            "caveat": caveat,
            "explanation": explanation,
            "suggested_action": suggested_action,
            "steps": steps,
        }
    except Exception as exc:
        return {
            "success": False,
            "message": f"Network error: {exc}",
            "latency_ms": 0,
            "provider_id": provider_id,
            "provider_name": provider.get("name"),
            "domain": domain,
            "port": port,
            "dns_ok": True,
            "resolved_ip": ip,
            "udp_reachable": False,
            "options_status": "error",
            "options_status_code": None,
            "options_status_line": "",
            "auth_challenge": False,
            "auth_realm": None,
            "register_status": "error",
            "register_status_code": None,
            "register_status_line": "",
            "provider_readiness": "network_error",
            "readiness_label": "Network Error",
            "outbound_call_verified": False,
            "caveat": caveat,
            "explanation": f"Socket diagnostic failed: {exc}",
            "suggested_action": "Verify network routing and SIP_BIND_IP configuration.",
            "steps": steps + [{"key": "network", "label": "Socket Probe", "status": "fail", "detail": str(exc)}],
        }
    finally:
        sock.close()
