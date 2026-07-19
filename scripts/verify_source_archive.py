#!/usr/bin/env python3
"""scripts/verify_source_archive.py — verify a source-code archive is safe to share.

Uses the shared secret-detection module (scripts/secret_patterns.py) so that
this verifier and secret_scan.py cannot produce conflicting results on the same
archive — they use identical pattern definitions and safe_line_if rules.

Checks:
  1. Archive opens successfully
  2. No path traversal entries (..)
  3. No absolute paths
  4. No .env or local environment files
  5. No log files
  6. No private key / certificate files
  7. No database dumps
  8. No recordings / generated audio
  9. No browser / session secret artifacts
 10. No file content matches high-confidence secret patterns (shared module)
 11. Required source directories present (app/, migrations/, tests/)
 12. Migration SQL files present
 13. Exit non-zero on any failure

Usage:
    python scripts/verify_source_archive.py <path-to-archive.zip>
    echo $?   # 0 = clean, 1 = failure
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

# Force UTF-8 stdout/stderr so Unicode symbols (✗, ✓, emoji) print correctly on
# Windows where the default console encoding is cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Shared patterns — must match secret_scan.py exactly.
from secret_patterns import (
    BINARY_EXTENSIONS,
    scan_text,
)


# ── Filename / path rules ─────────────────────────────────────────────────────

ENV_PATTERN = re.compile(r"(^|/)\.env(\..+)?$", re.I)

# Allow committed example files (must contain only placeholders — checked separately)
ENV_ALLOWED_NAMES = frozenset({".env.example", ".env.production.example"})

LOG_PATTERN      = re.compile(r"\.(log|log\.\d+)$",                          re.I)
CERT_KEY_PATTERN = re.compile(r"\.(pem|key|crt|cer|p12|pfx|der)$",          re.I)
DUMP_PATTERN     = re.compile(r"\.(dump|backup|bak|sql\.gz)$",               re.I)
AUDIO_PATTERN    = re.compile(r"\.(mp3|wav|ogg|flac|aac|m4a)$",             re.I)
SESSION_PATTERN  = re.compile(
    r"(playwright-report|test-results|\.playwright|browser-session)", re.I,
)

# migration SQL is application source code — explicitly allowed.
MIGRATION_GLOB   = re.compile(r"^migrations/\d+_.+\.sql$", re.I)


def is_non_migration_sql(name: str) -> bool:
    """True when name ends in .sql but is NOT under migrations/."""
    if not name.lower().endswith(".sql"):
        return False
    return "migrations" not in PurePosixPath(name).parts


# ── Required content ──────────────────────────────────────────────────────────

REQUIRED_DIR_PREFIXES = {"app/", "migrations/", "tests/"}
REQUIRED_TOP_FILES    = {"requirements.txt", "run.py"}


# ── Verifier ──────────────────────────────────────────────────────────────────

def check_archive(archive_path: Path) -> int:
    """Return 0 if clean, 1 on any failure."""
    failures: list[str] = []
    warnings: list[str] = []

    print(f"Verifying: {archive_path}")

    # 1. Opens successfully
    try:
        zf = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        print(f"FAIL: Archive could not be opened: {exc}")
        return 1

    entries = zf.infolist()
    names   = [e.filename for e in entries]

    # 2. No path traversal
    for name in names:
        if ".." in PurePosixPath(name).parts:
            failures.append(f"Path traversal entry: {name}")

    # 3. No absolute paths
    for name in names:
        if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
            failures.append(f"Absolute path entry: {name}")

    # 4–9. Filename checks
    for name in names:
        fname = PurePosixPath(name).name

        # 4. No .env files (allow specific committed example files only)
        if ENV_PATTERN.search(name):
            if fname not in ENV_ALLOWED_NAMES:
                failures.append(f"Environment file in archive: {name}")

        # 5. Log files
        if LOG_PATTERN.search(name):
            failures.append(f"Log file in archive: {name}")

        # 6. Private keys / certificates
        if CERT_KEY_PATTERN.search(name):
            failures.append(f"Certificate/key file in archive: {name}")

        # 7. Database dumps (not migration .sql)
        if DUMP_PATTERN.search(name):
            failures.append(f"Database dump in archive: {name}")
        if is_non_migration_sql(name):
            warnings.append(f"WARNING: .sql outside migrations/ — verify it is source: {name}")

        # 8. Audio / recordings
        if AUDIO_PATTERN.search(name):
            failures.append(f"Audio/recording file in archive: {name}")

        # 9. Browser session artifacts
        if SESSION_PATTERN.search(name):
            failures.append(f"Browser/session artifact in archive: {name}")

    # 10. Content scan — uses shared patterns (same as secret_scan.py).
    # Documentation files (.md, .rst, .txt) ARE scanned — they can contain
    # real credentials.  No whole-file or whole-extension skips.
    for entry in entries:
        name = entry.filename
        ext  = Path(name).suffix.lower()
        if ext in BINARY_EXTENSIONS:
            continue
        try:
            text = zf.read(name).decode("utf-8", errors="replace")
        except Exception:
            continue
        for finding in scan_text(text, name):
            failures.append(
                f"Likely secret [{finding.pattern_name}] in {name}:{finding.line_no}"
                f" — {finding.redacted}"
            )

    # 11. Required directories
    present_prefixes = {n.split("/")[0] + "/" for n in names if "/" in n}
    for req in REQUIRED_DIR_PREFIXES:
        if req not in present_prefixes and req.rstrip("/") not in names:
            failures.append(f"Required directory missing from archive: {req}")

    # 12. Migration SQL
    migration_files = [n for n in names if MIGRATION_GLOB.match(n)]
    if not migration_files:
        failures.append("No migration SQL files found (migrations/*.sql must be present)")
    else:
        print(f"  Migration files: {len(migration_files)} found.")

    zf.close()

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
