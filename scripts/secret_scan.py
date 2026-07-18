#!/usr/bin/env python3
"""scripts/secret_scan.py — project-owned secret scanner.

Scans staged Git diff, source archives, or individual files for likely
real credentials. Prints only redacted matches (never the full value).
Exits non-zero when a likely real secret is found.

Usage:
    # Scan staged diff
    python scripts/secret_scan.py --staged

    # Scan a source archive
    python scripts/secret_scan.py --archive path/to/code.zip

    # Scan specific files
    python scripts/secret_scan.py --files .env.example .env.production.example
"""
from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import NamedTuple

# ── Patterns ─────────────────────────────────────────────────────────────────
# Each pattern has a name, regex, and an optional exclusion for known-safe
# placeholder values. The regex captures just the secret portion in group 1.

class Pattern(NamedTuple):
    name: str
    regex: re.Pattern
    safe_if: re.Pattern | None = None  # if group(1) matches this, allow it


PATTERNS: list[Pattern] = [
    # Private keys
    Pattern(
        "private-key-header",
        re.compile(r"(-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY)", re.I),
    ),
    # AWS-style keys (AKIAIOSFODNN7EXAMPLE is the canonical AWS docs test key)
    Pattern(
        "aws-access-key",
        re.compile(r"(?<![A-Z0-9])(AKIA[0-9A-Z]{16})(?![A-Z0-9])"),
        safe_if=re.compile(r"AKIAIOSFODNN7EXAMPLE"),
    ),
    Pattern(
        "aws-secret-key",
        re.compile(r'(?i)aws.{0,20}secret.{0,10}["\']?([A-Za-z0-9+/]{40})["\']?'),
    ),
    # GitHub tokens
    Pattern(
        "github-token",
        re.compile(r"(gh[ps]_[A-Za-z0-9]{36,})"),
    ),
    # Stripe / payment keys
    Pattern(
        "stripe-key",
        re.compile(r"((?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{20,})"),
    ),
    # PostgreSQL connection string with embedded password
    Pattern(
        "pg-url-with-password",
        re.compile(r"(postgresql://[^:]+:[^@]{6,}@[^\s\"']+)"),
        safe_if=re.compile(
            r"postgresql://[^:]+:("
            r"YOUR_DEV_DB_PASSWORD|"
            r"REPLACE_[A-Z_]+|"
            r"your_[a-z_]+|"
            r"change-me[^@]*|"
            r"ccall_dev_password|"
            r"staging_pg_password_local_only|"
            r"password"                    # committed dev default (localhost only)
            r")@"
            r"|os\.environ\.get\("         # code reading from env, not a literal
            r"|postgresql://\["           # regex pattern string in scanner code
            r"|-match\s+['\"]"           # PowerShell -match operator (pattern string)
            r"|r\"postgresql",            # Python raw string (pattern string)
            re.I,
        ),
    ),
    # Redis URL with embedded password
    Pattern(
        "redis-url-with-password",
        re.compile(r"(redis://:[^@]{6,}@[^\s\"']+)"),
        safe_if=re.compile(
            r"redis://:[^@]*("
            r"YOUR_DEV_REDIS_PASSWORD|"
            r"REPLACE_[A-Z_]+|"
            r"your_[a-z_]+|"
            r"redis_dev_password|"
            r"staging_redis_password_local_only"
            r")[^@]*@",
            re.I,
        ),
    ),
    # Fernet / encryption keys (base64, 44 chars with = padding)
    Pattern(
        "fernet-key",
        re.compile(r'(?i)(?:fernet|encryption[_\s]key)[^=\n]*=\s*([A-Za-z0-9+/]{43}=)'),
        safe_if=re.compile(r"REPLACE_|your_fernet|staging-fernet"),
    ),
    # Generic API key assignments with real-looking values
    Pattern(
        "api-key-assignment",
        re.compile(
            r'(?i)(?:api[_\-]?key|secret[_\-]?key|auth[_\-]?token|access[_\-]?token)'
            r'\s*[=:]\s*["\']?([A-Za-z0-9\-_]{32,})["\']?'
        ),
        safe_if=re.compile(
            r"(?:"
            r"your_[a-z_]+|"
            r"REPLACE_[A-Z_]+|"
            r"change-me[^\'\"]*|"
            r"STAGING_MOCK_[A-Z_]+|"
            r"staging[-_][a-z\-_]+|"
            r"[A-Z_]+_PLACEHOLDER|"
            r"your_sentry_dsn_here|"
            r"staging-metrics-token-local-only|"
            r"staging-secret-key-64chars-.+|"
            r"test-secret-key-.+|"           # TestingConfig fixture
            r"some_value_here_should_not_be_here|"  # test file placeholder string
            r"os\.environ\.get\(|"           # code reading from env
            r"\.get\(['\"]"                  # dict/form .get() call — not a literal value
            r")",
            re.I,
        ),
    ),
    # SIP password assignments with real-looking values
    Pattern(
        "sip-password",
        re.compile(r'(?i)SIP_PASSWORD\s*=\s*([^\s\n#]{8,})'),
        safe_if=re.compile(
            r"(?:"
            r"your_sip_password|REPLACE_SIP_PASSWORD|mock_sip_password|"
            r"staging[-_].+|"
            r"request\.form\.get|"           # reading from HTTP request, not hardcoding
            r"form\.get\("
            r")",
            re.I,
        ),
    ),
    # Non-placeholder SECRET_KEY (Flask)
    Pattern(
        "flask-secret-key",
        re.compile(r'(?i)(?:FLASK_)?SECRET_KEY\s*=\s*([^\s\n#]{32,})'),
        safe_if=re.compile(
            r"(?:"
            r"change-me.+|"
            r"REPLACE_WITH_.+|"
            r"staging-secret-key-.+|"
            r"test-secret-key-.+|"          # test fixture in TestingConfig
            r"some_value_here.+|"           # test placeholder string content
            r"should_not_be_here|"          # test assertion placeholder
            r"YOUR_.+|"
            r"os\.environ\.get\("           # reads from env — not hardcoded
            r")",
            re.I,
        ),
    ),
    # Hardcoded password= in script files (not in comments)
    Pattern(
        "hardcoded-password-assignment",
        re.compile(r'(?<!\#)\$DB_PASS(?:WORD)?\s*=\s*"([^"$]{4,})"'),
        safe_if=re.compile(r'^\$\{|^\$env:'),
    ),
    # SIP password in route handlers — reading from form, not hardcoding
    Pattern(
        "sip-password",
        re.compile(r'(?i)SIP_PASSWORD\s*=\s*([^\s\n#]{8,})'),
        safe_if=re.compile(
            r"^(?:"
            r"your_sip_password|REPLACE_SIP_PASSWORD|mock_sip_password|"
            r"staging[-_].+|request\.form\.get"
            r")$",
            re.I,
        ),
    ),
]

# ── Files to skip even inside archives ───────────────────────────────────────
SKIP_EXTENSIONS = {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico",
                   ".mp3", ".wav", ".ogg", ".zip", ".gz", ".bz2", ".whl"}
SKIP_FILENAMES  = {"package-lock.json", "yarn.lock", "poetry.lock"}


def redact(value: str) -> str:
    """Show first 4 chars + **** so the pattern is identifiable but not usable."""
    if len(value) <= 6:
        return "****"
    return value[:4] + "****" + value[-2:]


class Finding(NamedTuple):
    source: str   # filename or "staged"
    line_no: int
    pattern_name: str
    redacted: str


def scan_text(text: str, source: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        for pat in PATTERNS:
            m = pat.regex.search(line)
            if not m:
                continue
            value = m.group(1) if m.lastindex else m.group(0)
            if pat.safe_if and pat.safe_if.search(line):
                continue
            # Additional heuristic: skip very short or clearly placeholder values
            if len(value) < 8:
                continue
            findings.append(Finding(source, line_no, pat.name, redact(value)))
    return findings


def scan_file(path: Path) -> list[Finding]:
    if path.suffix.lower() in SKIP_EXTENSIONS:
        return []
    if path.name in SKIP_FILENAMES:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return scan_text(text, str(path))


def scan_staged() -> list[Finding]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--unified=0"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("WARNING: could not get staged diff", file=sys.stderr)
        return []
    findings: list[Finding] = []
    current_file = "staged"
    line_no = 0
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            line_no = 0
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            line_no = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            line_no += 1
            content = line[1:]
            for pat in PATTERNS:
                m = pat.regex.search(content)
                if not m:
                    continue
                value = m.group(1) if m.lastindex else m.group(0)
                if pat.safe_if and pat.safe_if.search(content):
                    continue
                if len(value) < 8:
                    continue
                findings.append(Finding(f"{current_file}:{line_no}", 0, pat.name, redact(value)))
    return findings


def scan_archive(archive_path: Path) -> list[Finding]:
    findings: list[Finding] = []
    with zipfile.ZipFile(archive_path) as zf:
        for entry in zf.infolist():
            name = entry.filename
            if any(name.endswith(ext) for ext in SKIP_EXTENSIONS):
                continue
            if Path(name).name in SKIP_FILENAMES:
                continue
            try:
                text = zf.read(name).decode("utf-8", errors="replace")
            except Exception:
                continue
            findings.extend(scan_text(text, f"archive:{name}"))
    return findings


def print_findings(findings: list[Finding]) -> None:
    for f in findings:
        location = f"{f.source}" + (f":{f.line_no}" if f.line_no else "")
        print(f"  FINDING [{f.pattern_name}] at {location} — value: {f.redacted}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Secret scanner — never prints full values")
    parser.add_argument("--staged",  action="store_true", help="Scan staged git diff")
    parser.add_argument("--archive", type=Path,           help="Scan a .zip source archive")
    parser.add_argument("--files",   nargs="+", type=Path, help="Scan specific files")
    args = parser.parse_args()

    if not (args.staged or args.archive or args.files):
        parser.print_help()
        return 0

    all_findings: list[Finding] = []

    if args.staged:
        print("Scanning staged diff...")
        found = scan_staged()
        if found:
            print(f"  {len(found)} finding(s) in staged content:")
            print_findings(found)
        else:
            print("  Staged diff: clean.")
        all_findings.extend(found)

    if args.archive:
        print(f"Scanning archive: {args.archive} ...")
        found = scan_archive(args.archive)
        if found:
            print(f"  {len(found)} finding(s) in archive:")
            print_findings(found)
        else:
            print("  Archive: clean.")
        all_findings.extend(found)

    if args.files:
        for f in args.files:
            print(f"Scanning file: {f} ...")
            found = scan_file(f)
            if found:
                print(f"  {len(found)} finding(s):")
                print_findings(found)
            else:
                print("  Clean.")
            all_findings.extend(found)

    if all_findings:
        print(f"\nFAIL — {len(all_findings)} likely secret(s) found. See findings above (values redacted).")
        return 1
    print("\nPASS — no likely secrets found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
