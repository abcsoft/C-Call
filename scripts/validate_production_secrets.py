"""
validate_production_secrets.py

Run on the production server AFTER .env.production is populated.
Validates secret quality without printing any secret values.

Usage:
    python scripts/validate_production_secrets.py --env .env.production
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import sys
from pathlib import Path


def _load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


CHECKS: list[tuple[str, str, callable]] = []


def check(name: str, label: str):
    def decorator(fn):
        CHECKS.append((name, label, fn))
        return fn
    return decorator


# ── Presence checks ───────────────────────────────────────────────────────────

REQUIRED_KEYS = [
    "FLASK_SECRET_KEY",
    "API_KEY_PEPPER",
    "SIP_PASSWORD_ENCRYPTION_KEY",
    "REDIS_PASSWORD",
    "DATABASE_URL",
    "MIGRATION_DATABASE_URL",
    "METRICS_AUTH_TOKEN",
    "BACKUP_ENCRYPTION_PASSPHRASE",
    "PAYMENT_WEBHOOK_SECRET",
    "WOOCOMMERCE_WEBHOOK_SECRET",
    "SENTRY_DSN",
    "SENDGRID_API_KEY",
    "SIP_USER",
    "SIP_PASSWORD",
    "SSLCOMMERZ_STORE_ID",
    "SSLCOMMERZ_STORE_PASSWORD",
    "BKASH_APP_KEY",
    "BKASH_APP_SECRET",
]

PLACEHOLDER_PATTERNS = re.compile(
    r"^(REPLACE_|PLACEHOLDER|changeme|todo|example|test|staging|localhost|127\.0\.0\.1)",
    re.IGNORECASE,
)


def _present_and_not_placeholder(env: dict, key: str) -> tuple[bool, str]:
    val = env.get(key, "")
    if not val:
        return False, "MISSING"
    if PLACEHOLDER_PATTERNS.match(val):
        return False, "PLACEHOLDER"
    return True, "PRESENT"


# ── Specific quality checks ────────────────────────────────────────────────────

def _check_hex_entropy(val: str, min_bytes: int) -> tuple[bool, str]:
    """Validate hex string with minimum byte entropy."""
    try:
        raw = bytes.fromhex(val)
    except ValueError:
        return False, f"NOT_HEX"
    if len(raw) < min_bytes:
        return False, f"TOO_SHORT ({len(raw)} bytes, need {min_bytes})"
    return True, f"VALID ({len(raw)} bytes hex)"


def _check_urlsafe_entropy(val: str, min_bytes: int) -> tuple[bool, str]:
    try:
        # token_urlsafe produces base64url without padding; add padding for decode
        padded = val + "=" * (-len(val) % 4)
        raw = base64.urlsafe_b64decode(padded)
    except Exception:
        # Not base64 — treat as raw string
        raw = val.encode()
    if len(raw) < min_bytes:
        return False, f"TOO_SHORT ({len(raw)} bytes, need {min_bytes})"
    return True, f"VALID ({len(raw)} bytes)"


def _check_fernet(val: str) -> tuple[bool, str]:
    try:
        from cryptography.fernet import Fernet
        Fernet(val.encode())
        return True, "VALID Fernet key"
    except Exception as e:
        return False, f"INVALID Fernet: {e}"


def _check_db_url(val: str, must_contain: str) -> tuple[bool, str]:
    if not val.startswith("postgresql://"):
        return False, "NOT postgresql:// URL"
    if must_contain not in val:
        return False, f"DB name must contain '{must_contain}'"
    if "staging" in val or "test" in val.lower():
        return False, "DB URL contains staging/test — use production DB name"
    if "REPLACE" in val:
        return False, "PLACEHOLDER in URL"
    return True, "VALID"


def _check_redis_url(val: str) -> tuple[bool, str]:
    if "REPLACE" in val:
        return False, "PLACEHOLDER"
    if not val.startswith("redis://"):
        return False, "NOT redis:// URL"
    if "@" not in val:
        return False, "No password in Redis URL"
    return True, "VALID"


def _check_boolean_false(val: str, key: str) -> tuple[bool, str]:
    if val.lower() not in ("false", "0", "no"):
        return False, f"{key}={val!r} — must be false in production"
    return True, "false ✓"


# ── Runner ────────────────────────────────────────────────────────────────────

def run(env_path: str) -> int:
    env = _load_env(env_path)
    failures = 0

    print(f"\n{'='*60}")
    print(f"Secret quality validation: {env_path}")
    print(f"{'='*60}\n")

    # 1. Presence sweep
    print("── Presence checks ──────────────────────────────────────")
    for key in REQUIRED_KEYS:
        ok, status = _present_and_not_placeholder(env, key)
        tag = "✓" if ok else "✗"
        print(f"  {tag} {key}: {status}")
        if not ok:
            failures += 1

    # 2. Quality checks
    print("\n── Quality checks ───────────────────────────────────────")

    checks = [
        ("FLASK_SECRET_KEY", "hex entropy >= 64 bytes",
         lambda v: _check_hex_entropy(v, 64)),
        ("API_KEY_PEPPER", "hex entropy >= 32 bytes",
         lambda v: _check_hex_entropy(v, 32)),
        ("SIP_PASSWORD_ENCRYPTION_KEY", "valid Fernet key",
         lambda v: _check_fernet(v)),
        ("METRICS_AUTH_TOKEN", "urlsafe entropy >= 32 bytes",
         lambda v: _check_urlsafe_entropy(v, 32)),
        ("BACKUP_ENCRYPTION_PASSPHRASE", "urlsafe entropy >= 32 bytes",
         lambda v: _check_urlsafe_entropy(v, 32)),
        ("DATABASE_URL", "production DB URL",
         lambda v: _check_db_url(v, "ccall_production")),
        ("MIGRATION_DATABASE_URL", "production DB URL",
         lambda v: _check_db_url(v, "ccall_production")),
        ("REDIS_URL", "password-authenticated Redis URL",
         lambda v: _check_redis_url(v)),
    ]

    for key, label, check_fn in checks:
        val = env.get(key, "")
        if not val:
            print(f"  ✗ {key}: MISSING (skip quality check)")
            failures += 1
            continue
        ok, detail = check_fn(val)
        tag = "✓" if ok else "✗"
        print(f"  {tag} {key} ({label}): {detail}")
        if not ok:
            failures += 1

    # 3. Mock provider flags must be false
    print("\n── Mock provider flags (must all be false) ──────────────")
    for key in ("MOCK_SIP_PROVIDER", "MOCK_TTS_PROVIDER", "MOCK_PAYMENT_PROVIDER"):
        val = env.get(key, "false")
        ok, detail = _check_boolean_false(val, key)
        tag = "✓" if ok else "✗"
        print(f"  {tag} {key}: {detail}")
        if not ok:
            failures += 1

    # 4. Sandbox flags must be false
    print("\n── Sandbox flags (must all be false in production) ──────")
    for key in ("SSLCOMMERZ_SANDBOX", "BKASH_SANDBOX"):
        val = env.get(key, "true")
        ok, detail = _check_boolean_false(val, key)
        tag = "✓" if ok else "✗"
        print(f"  {tag} {key}: {detail}")
        if not ok:
            failures += 1

    # 5. Flask/app env checks
    print("\n── Application flags ────────────────────────────────────")
    for key, expected in [
        ("FLASK_ENV", "production"),
        ("DEBUG", "false"),
        ("TESTING", "false"),
    ]:
        val = env.get(key, "")
        ok = val.lower() == expected
        tag = "✓" if ok else "✗"
        print(f"  {tag} {key}={val!r} (expected '{expected}')")
        if not ok:
            failures += 1

    # 6. Service role isolation
    print("\n── Service role isolation (base .env must have all false) ─")
    for key in ("ENABLE_SCHEDULER", "ENABLE_CALL_WORKER", "ENABLE_CALL_MONITOR"):
        val = env.get(key, "")
        ok = val.lower() in ("false", "0", "no")
        tag = "✓" if ok else "!"
        print(f"  {tag} {key}={val!r} ({'false in base env ✓' if ok else 'true — verify this is the correct service env'})")

    # Summary
    print(f"\n{'='*60}")
    if failures:
        print(f"RESULT: {failures} check(s) FAILED — do not deploy")
    else:
        print("RESULT: all checks PASSED — secrets meet quality requirements")
    print(f"{'='*60}\n")

    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env.production")
    args = parser.parse_args()
    sys.exit(run(args.env))
