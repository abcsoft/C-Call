"""scripts/secret_patterns.py — shared secret detection for all hardening tools.

Provides:
  - Pattern definitions (name, regex, safe_line_if, safe_value_if)
  - Finding dataclass
  - redact()
  - scan_text(text, source) -> list[Finding]
  - scan_file(path) -> list[Finding]

Used by:
  - scripts/secret_scan.py   (staged diff, archive, file scanning)
  - scripts/verify_source_archive.py  (archive content checks)

Line-level allow marker
-----------------------
A line containing exactly the string below is individually exempted.
Use ONLY on regex-definition source lines that visually resemble credentials
but are not live values.  Must NOT be used to suppress entire files, entire
extensions, or entire credential categories.

    # secret-scan: allow-pattern-definition

The exemption covers only the single line it appears on.  Real secrets pasted
on neighboring lines are still detected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ── Allow marker ─────────────────────────────────────────────────────────────
# Only this exact string exempts a line.  Any other reason is not permitted.
_ALLOW_LINE_MARKER = "# secret-scan: allow-pattern-definition"


# ── Finding ───────────────────────────────────────────────────────────────────

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
    # safe_line_if: applied to the FULL LINE.
    #   Use ONLY for checking that a placeholder keyword is present in a URL
    #   or assignment (e.g. YOUR_DEV_DB_PASSWORD inside a postgresql:// string).
    #   Do NOT use for broad context like "os.environ.get appears somewhere
    #   on the line" — that suppresses real secrets hidden in fallback args.
    safe_line_if: Optional[re.Pattern] = None
    # safe_value_if: applied to the MATCHED VALUE (group 1 or full match).
    #   Use to detect that the captured token is itself a code expression
    #   (e.g. it contains "." or "("), a placeholder name, or an env-var key.
    safe_value_if: Optional[re.Pattern] = None


# ── Pattern Definitions ───────────────────────────────────────────────────────

PATTERNS: list[Pattern] = [

    # ── Private key headers ───────────────────────────────────────────────────
    Pattern(
        name="private-key",
        regex=re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY", re.I),  # secret-scan: allow-pattern-definition
    ),

    # ── AWS IAM access key ────────────────────────────────────────────────────
    # AKIAIOSFODNN7EXAMPLE is the canonical AWS documentation example key —
    # universally known, never issued as a real IAM credential.
    Pattern(
        name="aws-access-key",
        regex=re.compile(r"(?<![A-Z0-9])(AKIA[0-9A-Z]{16})(?![A-Z0-9])"),  # secret-scan: allow-pattern-definition
        safe_line_if=re.compile(r"AKIAIOSFODNN7EXAMPLE"),
    ),

    # ── GitHub personal / fine-grained tokens ────────────────────────────────
    Pattern(
        name="github-token",
        regex=re.compile(r"(gh[ps]_[A-Za-z0-9]{36,})"),  # secret-scan: allow-pattern-definition
    ),

    # ── Stripe live keys ──────────────────────────────────────────────────────
    Pattern(
        name="stripe-live-key",
        regex=re.compile(r"((?:sk|pk|rk)_live_[A-Za-z0-9]{20,})"),  # secret-scan: allow-pattern-definition
    ),

    # ── PostgreSQL connection URL with embedded password ──────────────────────
    # safe_line_if: suppress only when the password field in the URL is an
    # obvious placeholder.  Does NOT suppress merely because os.environ.get
    # appears on the line — that would hide real secrets in fallback args.
    Pattern(
        name="pg-url-with-password",
        regex=re.compile(r"(postgresql://[^:]+:[^@]{6,}@[^\s\"'\)]+)", re.I),  # secret-scan: allow-pattern-definition
        safe_line_if=re.compile(
            r"postgresql://[^:]+:("
            r"YOUR_DEV_DB_PASSWORD"
            r"|YOUR_DEV_[A-Z_]+"
            r"|REPLACE_[A-Z_]+"
            r"|CHANGE_ME_[A-Z_]+"
            r"|ccall_dev_password"
            r"|staging_pg_password_local_only"
            r")@",
            re.I,
        ),
    ),

    # ── Redis URL with embedded password ─────────────────────────────────────
    Pattern(
        name="redis-url-with-password",
        regex=re.compile(r"(redis://:[^@]{6,}@[^\s\"'\)]+)", re.I),  # secret-scan: allow-pattern-definition
        safe_line_if=re.compile(
            r"redis://:[^@]*("
            r"YOUR_DEV_REDIS_PASSWORD"
            r"|REPLACE_[A-Z_]+"
            r"|CHANGE_ME_[A-Z_]+"
            r"|redis_dev_password"
            r"|staging_redis_password_local_only"
            r")[^@]*@",
            re.I,
        ),
    ),

    # ── Fernet / symmetric encryption keys ───────────────────────────────────
    Pattern(
        name="fernet-key",
        regex=re.compile(r'(?i)(?:fernet|encryption[_\s]key)[^=\n]*=\s*([A-Za-z0-9+/]{43}=)'),  # secret-scan: allow-pattern-definition
        safe_value_if=re.compile(r"(?i)REPLACE_|your_fernet|staging-fernet|CHANGE_ME_"),
    ),

    # ── Generic API / access-token assignments ────────────────────────────────
    # Fires on bare string literals of 32+ chars assigned to api_key / secret_key
    # / auth_token / access_token variables.
    #
    # safe_line_if: placeholder KEYWORD present on the line (env-var fallback
    # args that happen to be placeholders).  Narrow: never the presence of
    # os.environ.get, .get(, or function-call syntax.
    #
    # safe_value_if: matched VALUE is itself a placeholder or test marker.
    #
    # Function-call detection is handled generically in scan_text() by checking
    # whether the matched token is immediately followed by '(' in the source line.
    Pattern(
        name="api-key-assignment",
        regex=re.compile(
            r'(?i)(?:api[_\-]?key|secret[_\-]?key|auth[_\-]?token|access[_\-]?token)'
            r'\s*[=:]\s*["\']?([A-Za-z0-9\-_]{32,})["\']?'
        ),
        safe_line_if=re.compile(
            r"(?i)(?:"
            r"your_[a-z_]+"
            r"|REPLACE_[A-Z_]+"
            r"|CHANGE_ME_[A-Z_]+"
            r"|change-me[^'\"]*"
            r"|STAGING_MOCK_[A-Z_]+"
            r"|staging[-_][a-z\-_]+"
            r"|test-secret-key[-_]"
            r"|some_value_here"
            r"|should_not_be_here"
            r")",
        ),
        safe_value_if=re.compile(
            r"(?i)(?:"
            r"your_[a-z_]+"
            r"|REPLACE_[A-Z_]+"
            r"|CHANGE_ME_[A-Z_]+"
            r"|change-me"
            r"|STAGING_MOCK_"
            r"|staging[-_]"
            r"|test-secret-key"
            r"|some_value_here"
            r"|should_not_be_here"
            r")",
        ),
    ),

    # ── Non-placeholder Flask SECRET_KEY ─────────────────────────────────────
    # safe_value_if: value is a code expression (contains . or () or is a
    # placeholder string.  Removes the need to suppress based on os.environ.get
    # appearing anywhere on the line.
    Pattern(
        name="flask-secret-key",
        regex=re.compile(r'(?i)(?:FLASK_)?SECRET_KEY\s*=\s*([^\s\n#]{32,})'),
        safe_value_if=re.compile(
            r"(?i)(?:"
            r"[.(]"                 # value is a code expression, not a bare string
            r"|change-me"
            r"|REPLACE_WITH_"
            r"|CHANGE_ME_"
            r"|staging-secret-key-"
            r"|test-secret-key-"
            r"|some_value_here"
            r"|should_not_be_here"
            r")",
        ),
    ),

    # ── SIP password assignments ──────────────────────────────────────────────
    # Captures the last quoted string literal on the line after SIP_PASSWORD =.
    # Using a repeated group means defaults like request.form.get("key", "real")
    # are captured as the last (innermost) quoted value, not the function call.
    #
    # safe_value_if: suppresses when the captured value is itself a placeholder,
    # a form-field key name, or an ALL_CAPS env-var name (not a real password).
    Pattern(
        name="sip-password",
        regex=re.compile(  # secret-scan: allow-pattern-definition
            r'(?i)SIP_PASSWORD[^=\n]*=(?:[^"\'#\n]*["\']([^"\']{8,})["\'])+'
        ),
        safe_value_if=re.compile(
            # Case-SENSITIVE — no re.I flag — a real password like "myrealpassword9999" must not match.
            r"^(?:"
            r"your_sip_password"    # lowercase placeholder
            r"|REPLACE_SIP_PASSWORD"
            r"|CHANGE_ME_SIP_PASSWORD"
            r"|mock_sip_password"
            r"|FAKE_[A-Z_]+"        # ALL_CAPS test/fake marker
            r"|staging[-_]"         # staging prefix
            r"|sip_password"        # lowercase form-field key
            r"|SIP_PASSWORD"        # ALL_CAPS env-var key name
            r"|[A-Z][A-Z_]{7,}"     # other ALL_CAPS env-var names
            r"|[^a-zA-Z0-9]"        # starts with non-alphanumeric (e.g. Alpine !editing, CSS spaces)
            r"|.*\s"                # contains whitespace (CSS class string, not a real password)
            r")",
        ),
    ),

    # ── Hardcoded password string in PowerShell backup scripts ───────────────
    Pattern(
        name="ps1-hardcoded-password",
        regex=re.compile(r'\$(?:DB_PASS|DB_PASSWORD|PGPASSWORD)\s*=\s*"([^"$\{]{4,})"'),  # secret-scan: allow-pattern-definition
        safe_value_if=re.compile(r'(?i)(?:REPLACE_|CHANGE_ME_|your_|staging[-_]|env:)'),
    ),
]


# ── File extensions to skip (binary / generated) ─────────────────────────────

BINARY_EXTENSIONS: frozenset[str] = frozenset({
    ".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".zip", ".gz", ".bz2", ".whl", ".pdf", ".mp3", ".wav",
    ".ogg", ".flac",
})

# Kept for backwards compatibility; no longer used by scan_text or scan_file.
# Callers that previously used these to skip entire file categories should
# instead use the allow-marker mechanism for individual lines.
SCANNER_SOURCE_FILES: frozenset[str] = frozenset()
DOCUMENTATION_EXTENSIONS: frozenset[str] = frozenset()


# ── Core scan function ────────────────────────────────────────────────────────

def scan_text(text: str, source: str) -> list[Finding]:
    """Scan text for likely real secrets. Returns Finding list (never prints values).

    Lines are NOT skipped based on file extension, file path, or comment marker.
    A commented-out real credential must be caught.

    Exemptions:
      - Lines containing the exact marker  # secret-scan: allow-pattern-definition
        are skipped (for regex-definition source lines only).
      - Per-pattern safe_value_if: checked against the matched value only.
      - Per-pattern safe_line_if:  checked against the full line (narrow use).
      - Function-call detection:   matched token immediately followed by '(' in
        the source line is treated as a function call, not a literal value.
    """
    findings: list[Finding] = []
    lines = text.splitlines()

    for line_no, line in enumerate(lines, start=1):
        # Honor explicit allow marker for individual pattern-definition lines.
        # This must NOT be used for blanket file or extension suppression.
        if _ALLOW_LINE_MARKER in line:
            continue

        # NOTE: Lines starting with '#' are NOT globally skipped.
        # A comment containing a real credential must be caught.

        for pat in PATTERNS:
            m = pat.regex.search(line)
            if not m:
                continue
            value = m.group(1) if m.lastindex else m.group(0)
            if len(value) < 8:
                continue

            # Function-call detection: if the matched token is immediately
            # followed by '(' it is a function name, not a credential literal.
            end_pos = m.end(1) if m.lastindex else m.end(0)
            if end_pos < len(line) and line[end_pos] == "(":
                continue

            # Value-level safe rule — checks ONLY the matched value.
            if pat.safe_value_if and pat.safe_value_if.search(value):
                continue

            # Line-level safe rule — narrow: placeholder keyword in URL, etc.
            # Must not be used to suppress on the basis of surrounding context
            # like os.environ.get, .get(, or comment markers.
            if pat.safe_line_if and pat.safe_line_if.search(line):
                continue

            findings.append(Finding(source, line_no, pat.name, redact(value)))

    return findings


def scan_file(path: Path) -> list[Finding]:
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
