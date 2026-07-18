"""scripts/secret_patterns.py — shared secret detection for all hardening tools.

Provides:
  - Pattern definitions (name, regex, safe_if)
  - Finding dataclass
  - redact()
  - scan_text(text, source) -> list[Finding]
  - scan_file(path) -> list[Finding]

Used by:
  - scripts/secret_scan.py   (staged diff, archive, file scanning)
  - scripts/verify_source_archive.py  (archive content checks)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ── Finding ──────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    source: str      # filename / "staged" / "archive:path"
    line_no: int
    pattern_name: str
    redacted: str


def redact(value: str) -> str:
    """Show first 4 chars + **** so the pattern type is identifiable."""
    if len(value) <= 6:
        return "****"
    return value[:4] + "****" + value[-2:]


# ── Pattern ───────────────────────────────────────────────────────────────────

@dataclass
class Pattern:
    name: str
    regex: re.Pattern
    # If this pattern matches the FULL LINE, the finding is suppressed.
    # Keep rules narrow: exact placeholder values, source-code context markers,
    # test-fixture markers. Never suppress an entire file or category.
    safe_line_if: Optional[re.Pattern] = None


# ── Pattern Definitions ───────────────────────────────────────────────────────

PATTERNS: list[Pattern] = [

    # ── Private key headers ───────────────────────────────────────────────────
    Pattern(
        name="private-key",
        regex=re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY", re.I),
    ),

    # ── AWS IAM access key (real keys start AKIA; AKIAIOSFODNN7EXAMPLE is the
    # canonical AWS documentation example — universally known, never issued) ──
    Pattern(
        name="aws-access-key",
        regex=re.compile(r"(?<![A-Z0-9])(AKIA[0-9A-Z]{16})(?![A-Z0-9])"),
        safe_line_if=re.compile(r"AKIAIOSFODNN7EXAMPLE"),
    ),

    # ── GitHub personal / fine-grained tokens ────────────────────────────────
    Pattern(
        name="github-token",
        regex=re.compile(r"(gh[ps]_[A-Za-z0-9]{36,})"),
    ),

    # ── Stripe live keys ──────────────────────────────────────────────────────
    Pattern(
        name="stripe-live-key",
        regex=re.compile(r"((?:sk|pk|rk)_live_[A-Za-z0-9]{20,})"),
    ),

    # ── PostgreSQL connection URL with embedded password ──────────────────────
    # Safe if:
    #   - password is an obvious placeholder (YOUR_DEV_DB_PASSWORD, REPLACE_*, CHANGE_ME_*)
    #   - value appears in os.environ.get() call (code reading from env)
    #   - line contains a regex metachar sequence (pattern string in scanner code)
    #   - line uses PowerShell -match operator (pattern string)
    Pattern(
        name="pg-url-with-password",
        regex=re.compile(r"(postgresql://[^:]+:[^@]{6,}@[^\s\"'\)]+)", re.I),
        safe_line_if=re.compile(
            r"postgresql://[^:]+:("
            r"YOUR_DEV_DB_PASSWORD"
            r"|YOUR_DEV_[A-Z_]+"
            r"|REPLACE_[A-Z_]+"
            r"|CHANGE_ME_[A-Z_]+"       # new non-connectable placeholder
            r"|ccall_dev_password"       # .env.example documented dev default (allowed in docs/CLAUDE.md)
            r"|staging_pg_password_local_only"
            r")@"
            r"|os\.environ\.get\("      # code reading from env
            r"|postgresql://\["         # regex metachar — pattern string in scanner code
            r"|-match\s+['\"]"          # PowerShell -match (pattern string)
            r"|r\"postgresql"           # Python raw string (pattern string)
            r"|#.*postgresql://"        # commented-out example
            r"|example.*postgresql://"  # documentation example text
            ,
            re.I,
        ),
    ),

    # ── Redis URL with embedded password ─────────────────────────────────────
    Pattern(
        name="redis-url-with-password",
        regex=re.compile(r"(redis://:[^@]{6,}@[^\s\"'\)]+)", re.I),
        safe_line_if=re.compile(
            r"redis://:[^@]*("
            r"YOUR_DEV_REDIS_PASSWORD"
            r"|REPLACE_[A-Z_]+"
            r"|CHANGE_ME_[A-Z_]+"
            r"|redis_dev_password"
            r"|staging_redis_password_local_only"
            r")[^@]*@"
            r"|os\.environ\.get\(",
            re.I,
        ),
    ),

    # ── Fernet / symmetric encryption keys ───────────────────────────────────
    Pattern(
        name="fernet-key",
        regex=re.compile(r'(?i)(?:fernet|encryption[_\s]key)[^=\n]*=\s*([A-Za-z0-9+/]{43}=)'),
        safe_line_if=re.compile(r"REPLACE_|your_fernet|staging-fernet|CHANGE_ME_", re.I),
    ),

    # ── Generic API / access-token assignments ────────────────────────────────
    # Only fires on bare string literals of 32+ chars; suppressed when:
    #   - value is a clear placeholder
    #   - line is a function call (value ends with )
    #   - line reads from os.environ
    #   - it is a test fixture explicitly labelled as such
    Pattern(
        name="api-key-assignment",
        regex=re.compile(
            r'(?i)(?:api[_\-]?key|secret[_\-]?key|auth[_\-]?token|access[_\-]?token)'
            r'\s*[=:]\s*["\']?([A-Za-z0-9\-_]{32,})["\']?'
        ),
        safe_line_if=re.compile(
            r"(?:"
            r"your_[a-z_]+"
            r"|REPLACE_[A-Z_]+"
            r"|CHANGE_ME_[A-Z_]+"
            r"|change-me[^'\"]*"
            r"|STAGING_MOCK_[A-Z_]+"
            r"|staging[-_][a-z\-_]+"
            r"|test-secret-key[-_]"     # TestingConfig static key
            r"|some_value_here"         # test fixture placeholder string content
            r"|should_not_be_here"
            r"|os\.environ\.get\("      # reads from env
            r"|\.get\(['\"]"            # dict / form .get() call
            r"|re\.compile\("           # pattern string in scanner code
            r"|=\s*[a-z_][a-z0-9_]+\(" # RHS is a function call, not a literal
            r")",
            re.I,
        ),
    ),

    # ── Non-placeholder Flask SECRET_KEY ─────────────────────────────────────
    Pattern(
        name="flask-secret-key",
        regex=re.compile(r'(?i)(?:FLASK_)?SECRET_KEY\s*=\s*([^\s\n#]{32,})'),
        safe_line_if=re.compile(
            r"(?:"
            r"change-me"
            r"|REPLACE_WITH_"
            r"|CHANGE_ME_"
            r"|staging-secret-key-"
            r"|test-secret-key-"
            r"|some_value_here"
            r"|should_not_be_here"
            r"|os\.environ\.get\("
            r")",
            re.I,
        ),
    ),

    # ── SIP password assignments ──────────────────────────────────────────────
    # Suppressed when line is reading from form/request (not hardcoding).
    Pattern(
        name="sip-password",
        regex=re.compile(r'(?i)SIP_PASSWORD\s*=\s*([^\s\n#]{8,})'),
        safe_line_if=re.compile(
            r"(?:"
            r"your_sip_password"
            r"|REPLACE_SIP_PASSWORD"
            r"|CHANGE_ME_SIP_PASSWORD"
            r"|mock_sip_password"
            r"|staging[-_]"
            r"|request\.form\.get"      # reading from HTTP request body
            r"|form\.get\("
            r"|\.get\(['\"]sip_password"
            r")",
            re.I,
        ),
    ),

    # ── Hardcoded password string in PowerShell backup scripts ───────────────
    Pattern(
        name="ps1-hardcoded-password",
        regex=re.compile(r'\$(?:DB_PASS|DB_PASSWORD|PGPASSWORD)\s*=\s*"([^"$\{]{4,})"'),
        safe_line_if=re.compile(r'^\s*#|\$env:|env:CCALL|^\s*\$env:PGPASSWORD\s*=\s*\$'),
    ),
]


# ── File extensions to skip (binary / generated) ─────────────────────────────

BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".zip", ".gz", ".bz2", ".whl", ".pdf", ".mp3", ".wav",
    ".ogg", ".flac",
})

# Files whose content consists of pattern strings or intentional fake-secret fixtures,
# not live credentials. Kept intentionally narrow.
SCANNER_SOURCE_FILES: frozenset[str] = frozenset({
    "scripts/secret_scan.py",
    "scripts/secret_patterns.py",
    "scripts/verify_source_archive.py",
    "tests/unit/test_backup_security.py",  # intentional fake-secret fixtures for scanner tests
})

# File extensions treated as documentation (examples in docs are not live creds)
DOCUMENTATION_EXTENSIONS: frozenset[str] = frozenset({".md", ".rst", ".txt"})


# ── Core scan function ────────────────────────────────────────────────────────

def scan_text(text: str, source: str, *, skip_doc_extensions: bool = True) -> list[Finding]:
    """Scan text for likely real secrets. Returns Finding list (never prints values)."""
    findings: list[Finding] = []
    lines = text.splitlines()

    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        for pat in PATTERNS:
            m = pat.regex.search(line)
            if not m:
                continue
            value = m.group(1) if m.lastindex else m.group(0)
            if len(value) < 8:
                continue
            if pat.safe_line_if and pat.safe_line_if.search(line):
                continue
            findings.append(Finding(source, line_no, pat.name, redact(value)))

    return findings


def scan_file(path: Path, *, is_scanner_source: bool = False) -> list[Finding]:
    """Scan a single file. Returns empty list for binary files."""
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return []
    if path.name in {"package-lock.json", "yarn.lock", "poetry.lock"}:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return scan_text(text, str(path))
