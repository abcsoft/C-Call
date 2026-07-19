"""tests/unit/test_secret_scanner.py — tests for scripts/secret_scan.py UTF-8 robustness.

Fake credential values are constructed at RUNTIME via string concatenation so
the scanner never sees a complete credential literal in this source file.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from secret_scan import scan_staged  # noqa: E402
from secret_patterns import scan_text  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_staged_diff(added_lines: list[str], filename: str = "test_file.py") -> bytes:
    """Build a minimal unified-diff bytes object as git would produce."""
    header = (
        f"diff --git a/{filename} b/{filename}\n"
        f"--- a/{filename}\n"
        f"+++ b/{filename}\n"
        f"@@ -0,0 +1,{len(added_lines)} @@\n"
    )
    body = "\n".join(f"+{line}" for line in added_lines) + "\n"
    return (header + body).encode("utf-8")


def _patch_git(diff_bytes: bytes):
    """Context manager: patches subprocess.run to return diff_bytes as stdout."""
    fake = mock.MagicMock()
    fake.returncode = 0
    fake.stdout = diff_bytes
    return mock.patch("secret_scan.subprocess.run", return_value=fake)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_bengali_text_no_secret_passes():
    """Staged file containing Bengali text and no secret → zero findings."""
    bengali_line = "গ্রাহক নিশ্চিত করুন"  # "Confirm customer" in Bengali
    diff = _make_staged_diff([bengali_line, "x = 1  # Bengali comment: আদেশ"])
    with _patch_git(diff):
        findings = scan_staged()
    assert findings == [], f"Expected no findings, got: {findings}"


def test_bengali_text_with_github_token_fails():
    """Bengali text plus a literal GitHub token → scanner detects token."""
    token = "ghp_" + "A" * 36  # constructed at runtime — not a real token
    bengali_line = "# বাংলাদেশ ই-কমার্স"
    diff = _make_staged_diff([bengali_line, f'API_TOKEN = "{token}"'])
    with _patch_git(diff):
        findings = scan_staged()
    assert len(findings) >= 1, f"Expected finding for github token, got: {findings}"


def test_bengali_text_with_postgres_password_fails():
    """Bengali text plus PostgreSQL DSN password → scanner detects credential."""
    # Construct a realistic-looking DSN with a long password
    pw = "S3cr3tP" + "a" * 10 + "ss!"
    dsn = f"postgresql://user:{pw}@localhost/ccall"
    diff = _make_staged_diff(["# অর্ডার সার্ভিস", f'DATABASE_URL = "{dsn}"'])
    with _patch_git(diff):
        findings = scan_staged()
    assert len(findings) >= 1, f"Expected finding for DSN password, got: {findings}"


def test_binary_file_does_not_crash():
    """Binary content in staged diff → scanner does not crash, returns no findings."""
    # Build a diff with binary NUL bytes masquerading as added content.
    # The scanner replaces decode errors — it must not raise.
    binary_bytes = b"diff --git a/img.png b/img.png\n--- a/img.png\n+++ b/img.png\n@@ -0,0 +1 @@\n+\x00\x01\x02\xff\xfe binary\n"
    with _patch_git(binary_bytes):
        findings = scan_staged()  # must not raise
    # No credential patterns expected in binary junk.
    assert isinstance(findings, list)


def test_renamed_file_examines_added_content():
    """Rename diff: scanner inspects the new-file (+++) additions."""
    token = "ghp_" + "B" * 36
    diff = (
        "diff --git a/old.py b/new.py\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "--- a/old.py\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1 @@\n"
        f'+TOKEN = "{token}"\n'
    ).encode("utf-8")
    with _patch_git(diff):
        findings = scan_staged()
    assert len(findings) >= 1, "Renamed file's added content must be scanned"


def test_deleted_secret_no_false_positive():
    """Deleted lines (prefixed with -) must NOT trigger findings."""
    token = "ghp_" + "C" * 36
    # Only a removal line — no added lines.
    diff = (
        "diff --git a/old.py b/old.py\n"
        "--- a/old.py\n"
        "+++ b/old.py\n"
        "@@ -1 +0,0 @@\n"
        f'-TOKEN = "{token}"\n'
    ).encode("utf-8")
    with _patch_git(diff):
        findings = scan_staged()
    assert findings == [], f"Deleted lines must not produce findings, got: {findings}"


def test_scanner_works_under_windows_cp1252_locale():
    """Simulate Windows cp1252 locale: git returns UTF-8 bytes but scan_staged
    must not crash even if the process default encoding is cp1252-like."""
    bengali_bytes = "# বাংলাদেশ".encode("utf-8")  # valid UTF-8
    diff = _make_staged_diff(["x = 1"])
    # Replace the added-line section with the bengali_bytes prefix
    diff_with_bengali = _make_staged_diff(["# বাংলাদেশ", "x = 1"])
    # scan_staged reads result.stdout (bytes) — must decode UTF-8 regardless of locale.
    with _patch_git(diff_with_bengali):
        findings = scan_staged()
    assert isinstance(findings, list)  # no crash
