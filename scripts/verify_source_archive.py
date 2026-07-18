#!/usr/bin/env python3
"""scripts/verify_source_archive.py — verify a source-code archive is safe to share.

Checks:
  1. Archive opens successfully
  2. No path traversal entries (../)
  3. No absolute paths
  4. No .env or local environment files
  5. No log files
  6. No private key / certificate files
  7. No database dumps
  8. No recordings / generated audio
  9. No browser / session secret artifacts
 10. No file content matches high-confidence secret patterns
 11. Required source directories present
 12. Migration SQL files present
 13. Exit non-zero on any failure

Usage:
    python scripts/verify_source_archive.py <path-to-archive.zip>
    echo $?  # 0 = clean, 1 = failure
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

# ── Sensitive filename / extension rules ──────────────────────────────────────

ENV_PATTERNS = re.compile(
    r"(^|/)\.env(\..+)?$",
    re.IGNORECASE,
)

LOG_PATTERN = re.compile(r"\.(log|log\.\d+)$", re.IGNORECASE)

CERT_KEY_PATTERN = re.compile(
    r"\.(pem|key|crt|cer|p12|pfx|der)$",
    re.IGNORECASE,
)

DB_DUMP_PATTERN = re.compile(
    r"\.(dump|backup|bak|sql\.gz)$",
    re.IGNORECASE,
)

# Matches *.sql that is NOT under a migrations/ directory
def is_non_migration_sql(name: str) -> bool:
    if not name.lower().endswith(".sql"):
        return False
    p = PurePosixPath(name)
    return "migrations" not in p.parts

AUDIO_PATTERN = re.compile(r"\.(mp3|wav|ogg|flac|aac|m4a)$", re.IGNORECASE)

SESSION_PATTERN = re.compile(
    r"(playwright-report|test-results|\.playwright|browser-session)",
    re.IGNORECASE,
)

# ── Secret content patterns (high-confidence only) ────────────────────────────

SECRET_CONTENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key",       re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY", re.I)),
    # Real AWS IAM access keys start AKIA; AKIAIOSFODNN7EXAMPLE is the canonical docs example
    ("aws-access-key",    re.compile(r"(?<![A-Z0-9])(AKIA[0-9A-Z]{16})(?![A-Z0-9])")),
    ("github-token",      re.compile(r"gh[ps]_[A-Za-z0-9]{36,}")),
    ("stripe-live-key",   re.compile(r"(?:sk|pk|rk)_live_[A-Za-z0-9]{20,}")),
    ("pg-url-with-pass",  re.compile(r"postgresql://[^:]+:[^@]{8,}@[^\s\"']+", re.I)),
]

# Patterns whose full line is safe (placeholder, docs example, test fixture, or code pattern)
SAFE_LINE_PATTERNS: list[re.Pattern] = [
    # Obvious placeholder passwords in the URL
    re.compile(
        r"postgresql://[^:]+:("
        r"YOUR_DEV_DB_PASSWORD|REPLACE_[A-Z_]+|your_[a-z_]+|"
        r"ccall_dev_password|staging_pg_password_local_only|"
        r"password"              # committed dev default in config.py / conftest.py (localhost only)
        r")@",
        re.I,
    ),
    # Pattern strings inside scanner/verifier code: url contains [^:] or similar regex chars
    re.compile(r"postgresql://\[", re.I),
    # Python code reading from os.environ.get — not a hardcoded value
    re.compile(r"os\.environ\.get\("),
    # Well-known AWS documentation example key (never issued as real credential)
    re.compile(r"AKIAIOSFODNN7EXAMPLE"),
]

# ── Required content ──────────────────────────────────────────────────────────

REQUIRED_DIRS   = {"app/", "migrations/", "tests/"}
REQUIRED_FILES  = {"requirements.txt", "run.py"}
MIGRATION_GLOB  = re.compile(r"^migrations/\d+_.+\.sql$", re.IGNORECASE)

# ── Extensions safe to skip for content scanning ──────────────────────────────
BINARY_EXTENSIONS = {
    ".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".zip", ".gz", ".bz2", ".whl", ".pdf",
}


def redact(m: re.Match) -> str:
    v = m.group(0)
    return v[:4] + "****" + v[-2:] if len(v) > 6 else "****"


def check_archive(archive_path: Path) -> int:
    """Return 0 if clean, 1 on failure."""
    failures: list[str] = []
    warnings: list[str] = []

    print(f"Verifying: {archive_path}")

    # ── 1. Opens successfully ─────────────────────────────────────────────────
    try:
        zf = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        print(f"FAIL: Archive could not be opened: {exc}")
        return 1

    entries = zf.infolist()
    names   = [e.filename for e in entries]

    # ── 2. No path traversal ─────────────────────────────────────────────────
    for name in names:
        if ".." in PurePosixPath(name).parts:
            failures.append(f"Path traversal entry: {name}")

    # ── 3. No absolute paths ─────────────────────────────────────────────────
    for name in names:
        if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
            failures.append(f"Absolute path entry: {name}")

    # ── 4–9. Filename checks ──────────────────────────────────────────────────
    for name in names:
        lower = name.lower()

        # 4. No .env files (allow .env.example and .env.production.example)
        if ENV_PATTERNS.search(name):
            safe_examples = {".env.example", ".env.production.example"}
            fname = PurePosixPath(name).name
            if fname not in safe_examples:
                failures.append(f"Environment file in archive: {name}")

        # 5. Log files
        if LOG_PATTERN.search(name):
            failures.append(f"Log file in archive: {name}")

        # 6. Private keys / certificates
        if CERT_KEY_PATTERN.search(name):
            failures.append(f"Certificate/key file in archive: {name}")

        # 7. Database dumps (not migration .sql)
        if re.search(r"\.(dump|backup|bak|sql\.gz)$", lower):
            failures.append(f"Database dump in archive: {name}")

        if is_non_migration_sql(name):
            warnings.append(f"WARNING: .sql file outside migrations/ — verify it is source: {name}")

        # 8. Audio / recordings
        if AUDIO_PATTERN.search(name):
            failures.append(f"Audio/recording file in archive: {name}")

        # 9. Browser session artifacts
        if SESSION_PATTERN.search(name):
            failures.append(f"Browser/session artifact in archive: {name}")

    # ── 10. Content scan for secrets ──────────────────────────────────────────
    # Skip: documentation (.md), scanner/verifier scripts themselves (contain
    # patterns as code), and binary formats.
    CONTENT_SKIP_NAMES = {
        "scripts/secret_scan.py",
        "scripts/verify_source_archive.py",
    }
    for entry in entries:
        name = entry.filename
        if Path(name).suffix.lower() in BINARY_EXTENSIONS:
            continue
        if name.lower().endswith(".md"):
            continue  # documentation examples are not live credentials
        if name in CONTENT_SKIP_NAMES:
            continue  # pattern strings inside scanner code are not credentials
        try:
            text = zf.read(name).decode("utf-8", errors="replace")
        except Exception:
            continue

        lines = text.splitlines()
        for pat_name, pat in SECRET_CONTENT_PATTERNS:
            for m in pat.finditer(text):
                value = m.group(1) if m.lastindex else m.group(0)
                line_no = text[:m.start()].count("\n") + 1
                # Check the whole line against safe patterns
                line = lines[line_no - 1] if line_no <= len(lines) else ""
                if any(sp.search(line) for sp in SAFE_LINE_PATTERNS):
                    continue
                redacted_val = value[:4] + "****" if len(value) > 4 else "****"
                failures.append(
                    f"Likely secret [{pat_name}] in {name}:{line_no} — {redacted_val}"
                )

    # ── 11. Required source directories ──────────────────────────────────────
    present_prefixes = {n.split("/")[0] + "/" for n in names if "/" in n}
    for req in REQUIRED_DIRS:
        if req not in present_prefixes and req.rstrip("/") not in names:
            failures.append(f"Required directory missing from archive: {req}")

    # ── 12. Migration SQL present ─────────────────────────────────────────────
    migration_files = [n for n in names if MIGRATION_GLOB.match(n)]
    if not migration_files:
        failures.append("No migration SQL files found in archive (migrations/*.sql must be present)")
    else:
        print(f"  Migration files: {len(migration_files)} found.")

    zf.close()

    # ── Print results ─────────────────────────────────────────────────────────
    for w in warnings:
        print(f"  {w}")

    if failures:
        print(f"\nFAIL — {len(failures)} issue(s) found:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1

    print(f"  Entries checked: {len(names)}")
    print("\nPASS — archive is clean.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <archive.zip>")
        sys.exit(1)
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)
    sys.exit(check_archive(path))
