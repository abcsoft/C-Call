from __future__ import annotations

import os
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

def add_sip_provider(name: str, sip_domain: str, sip_port: int, sip_user: str, sip_password_plaintext: str) -> int:
    encrypted_pw = encrypt_password(sip_password_plaintext)
    return db.insert_returning_id(
        """
        INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password, status)
        VALUES (%s, %s, %s, %s, %s, 'active')
        RETURNING id
        """,
        (name, sip_domain, sip_port, sip_user, encrypted_pw)
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
    sip_password_plaintext: str | None = None
) -> None:
    if sip_password_plaintext:
        encrypted_pw = encrypt_password(sip_password_plaintext)
        db.execute(
            """
            UPDATE sip_providers
            SET name = %s, sip_domain = %s, sip_port = %s, sip_user = %s, sip_password = %s
            WHERE id = %s
            """,
            (name, sip_domain, sip_port, sip_user, encrypted_pw, provider_id)
        )
    else:
        db.execute(
            """
            UPDATE sip_providers
            SET name = %s, sip_domain = %s, sip_port = %s, sip_user = %s
            WHERE id = %s
            """,
            (name, sip_domain, sip_port, sip_user, provider_id)
        )

def update_sip_provider_status(provider_id: int, status: str) -> None:
    db.execute("UPDATE sip_providers SET status = %s WHERE id = %s", (status, provider_id))

def delete_sip_provider(provider_id: int) -> None:
    db.execute("DELETE FROM sip_providers WHERE id = %s", (provider_id,))

def get_all_providers() -> list[dict[str, Any]]:
    return get_all_sip_providers()

def create_provider(data: dict[str, Any]) -> int:
    return add_sip_provider(
        name=data["name"],
        sip_domain=data["sip_domain"],
        sip_port=int(data.get("sip_port") or 5060),
        sip_user=data["sip_user"],
        sip_password_plaintext=data["sip_password"]
    )

def update_provider(provider_id: int, data: dict[str, Any]) -> None:
    update_sip_provider(
        provider_id=provider_id,
        name=data["name"],
        sip_domain=data["sip_domain"],
        sip_port=int(data.get("sip_port") or 5060),
        sip_user=data["sip_user"],
        sip_password_plaintext=data.get("sip_password")
    )

def test_connection(provider_id: int) -> dict[str, Any]:
    import socket
    import time
    import random
    import uuid
    
    provider = get_sip_provider_by_id(provider_id)
    if not provider:
        return {"success": False, "message": "Provider not found", "latency_ms": 0}
        
    domain = provider["sip_domain"]
    port = provider.get("sip_port") or 5060
    
    # 1. Address resolution test
    start_time = time.perf_counter()
    try:
        ip = socket.gethostbyname(domain)
    except Exception as exc:
        return {
            "success": False,
            "message": f"DNS resolution failed for {domain}: {exc}",
            "latency_ms": 0
        }
        
    # 2. UDP SIP OPTIONS ping
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)  # 3 seconds timeout

    call_id = str(uuid.uuid4())
    branch = "z9hG4bK" + "".join(random.choices("0123456789abcdef", k=10))
    tag = "".join(random.choices("0123456789abcdef", k=8))

    try:
        # Dummy connection to get local IP
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        temp_sock.connect((ip, port))
        local_ip = temp_sock.getsockname()[0]
        temp_sock.close()
    except Exception:
        local_ip = "127.0.0.1"

    # Bind sock to the SAME port we advertise in the Via/Contact headers.
    # Without this, sendto() auto-binds to a random ephemeral port, but the
    # SIP server replies to whatever port the Via header claims (no ;rport
    # param here) — so the response lands on a port nothing is listening
    # on, and recvfrom() times out even though the server answered fine.
    advertised_ip = (
        os.environ.get("SIP_ADVERTISED_IP")
        or os.environ.get("PUBLIC_IP")
        or os.environ.get("EC2_PUBLIC_IP")
        or local_ip
    ).strip()
    bind_ip = os.environ.get("SIP_BIND_IP", "0.0.0.0").strip()
    local_ip = advertised_ip
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

    # Standard SIP OPTIONS request format
    sip_request = (
        f"OPTIONS sip:{domain}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_ip}:{local_port};rport;branch={branch}\r\n"
        f"Max-Forwards: 70\r\n"
        f"To: <sip:{domain}:{port}>\r\n"
        f"From: <sip:ccall@{domain}>;tag={tag}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Contact: <sip:ccall@{local_ip}:{local_port}>\r\n"
        f"Accept: application/sdp\r\n"
        f"Content-Length: 0\r\n\r\n"
    )
    
    try:
        # Send
        sock.sendto(sip_request.encode("utf-8"), (ip, port))
        # Receive
        data, addr = sock.recvfrom(2048)
        latency = int((time.perf_counter() - start_time) * 1000)
        response_text = data.decode("utf-8", errors="ignore")
        
        if response_text.startswith("SIP/2.0"):
            status_line = response_text.split("\r\n")[0]
            return {
                "success": True,
                "message": f"SIP Response: {status_line}",
                "latency_ms": latency
            }
        else:
            return {
                "success": True,
                "message": "Received response from server (non-SIP format).",
                "latency_ms": latency
            }
    except socket.timeout:
        return {
            "success": False,
            "message": "Request timed out (no UDP response from port 5060).",
            "latency_ms": 0
        }
    except Exception as exc:
        return {
            "success": False,
            "message": f"Network error: {exc}",
            "latency_ms": 0
        }
    finally:
        sock.close()
