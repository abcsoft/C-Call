"""Unit tests for backup/archive security hardening.

Uses only temporary dummy files — no real credentials anywhere in this file.
All "secret-looking" values used are intentionally fake test fixtures
with obvious placeholder names.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest


# ── Path helpers ──────────────────────────────────────────────────────────────
PROJECT_ROOT  = Path(__file__).parents[2]
VERIFIER      = PROJECT_ROOT / "scripts" / "verify_source_archive.py"
SCANNER       = PROJECT_ROOT / "scripts" / "secret_scan.py"
BACKUP_PS1    = PROJECT_ROOT / "backup.ps1"
BACKUP_SH     = PROJECT_ROOT / "scripts" / "backup.sh"


# ── Archive builder helper ────────────────────────────────────────────────────

def make_archive(tmp_path: Path, contents: dict[str, str]) -> Path:
    """Build a minimal ZIP at tmp_path/test.zip with given {entry: content}."""
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, content in contents.items():
            zf.writestr(name, content)
    return archive


def run_verifier(archive: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(archive)],
        capture_output=True, text=True,
    )


def run_scanner(*extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCANNER)] + list(extra_args),
        capture_output=True, text=True,
    )


# ── Test 1: Normal Python source included ─────────────────────────────────────

def test_normal_python_source_included(tmp_path):
    archive = make_archive(tmp_path, {
        "app/__init__.py": "# C-Call app\n",
        "app/services/order_service.py": "def create_order(): pass\n",
        "tests/__init__.py": "",
        "run.py": "from app import create_app\n",
        "requirements.txt": "flask\n",
        "migrations/001_initial_schema.sql": "CREATE TABLE tenants (id SERIAL PRIMARY KEY);\n",
    })
    result = run_verifier(archive)
    assert result.returncode == 0, f"Expected PASS but got:\n{result.stdout}\n{result.stderr}"
    assert "PASS" in result.stdout


# ── Test 2: Migration SQL remains included ────────────────────────────────────

def test_migration_sql_included(tmp_path):
    archive = make_archive(tmp_path, {
        "app/__init__.py": "",
        "tests/test_basic.py": "",
        "requirements.txt": "",
        "run.py": "",
        "migrations/001_initial_schema.sql": "CREATE TABLE tenants (id SERIAL);\n",
        "migrations/002_saas_layer.sql": "ALTER TABLE tenants ADD COLUMN name TEXT;\n",
    })
    result = run_verifier(archive)
    assert result.returncode == 0
    assert "Migration files: 2 found" in result.stdout


# ── Test 3: .env excluded → verifier rejects archive ─────────────────────────

def test_env_file_excluded(tmp_path):
    archive = make_archive(tmp_path, {
        "app/__init__.py": "",
        "run.py": "",
        "requirements.txt": "",
        "migrations/001.sql": "SELECT 1;\n",
        ".env": "SECRET_KEY=some_value_here_should_not_be_here\n",
    })
    result = run_verifier(archive)
    assert result.returncode == 1, "Archive containing .env must be rejected"
    assert ".env" in result.stdout or "Environment file" in result.stdout


# ── Test 4: .env.staging excluded ────────────────────────────────────────────

def test_env_staging_excluded(tmp_path):
    archive = make_archive(tmp_path, {
        "app/__init__.py": "",
        "run.py": "",
        "requirements.txt": "",
        "migrations/001.sql": "SELECT 1;\n",
        ".env.staging": "SECRET_KEY=staging_key_must_not_appear\n",
    })
    result = run_verifier(archive)
    assert result.returncode == 1
    assert "Environment file" in result.stdout or ".env.staging" in result.stdout


# ── Test 5: .env.production.example with placeholders is allowed ──────────────

def test_env_production_example_allowed(tmp_path):
    archive = make_archive(tmp_path, {
        "app/__init__.py": "",
        "tests/__init__.py": "",
        "run.py": "",
        "requirements.txt": "",
        "migrations/001_initial.sql": "SELECT 1;\n",
        ".env.production.example": (
            "# PLACEHOLDER FILE ONLY\n"
            "SECRET_KEY=REPLACE_WITH_128_CHAR_HEX_STRING\n"
            "DATABASE_URL=postgresql://ccall_app:REPLACE_DB_APP_PASSWORD@postgres:5432/ccall\n"
        ),
    })
    result = run_verifier(archive)
    assert result.returncode == 0, f"Placeholder example file should be allowed:\n{result.stdout}"


# ── Test 6: Real-looking secret in example file fails verification ─────────────

def test_real_secret_in_example_fails(tmp_path):
    # Fake AWS key — intentionally fake, pattern-detectable
    fake_aws_key = "AKIAIOSFODNN7EXAMPLE"
    archive = make_archive(tmp_path, {
        "app/__init__.py": "",
        "run.py": "",
        "requirements.txt": "",
        "migrations/001.sql": "SELECT 1;\n",
        ".env.production.example": f"AWS_ACCESS_KEY_ID={fake_aws_key}\n",
    })
    result = run_verifier(archive)
    assert result.returncode == 1, "Archive with real-looking AWS key must be rejected"
    assert "FAIL" in result.stdout


# ── Test 7: *.log excluded ────────────────────────────────────────────────────

def test_log_file_excluded(tmp_path):
    archive = make_archive(tmp_path, {
        "app/__init__.py": "",
        "run.py": "",
        "requirements.txt": "",
        "migrations/001.sql": "SELECT 1;\n",
        "ccall_debug.log": "2026-07-18 INFO: something happened\n",
    })
    result = run_verifier(archive)
    assert result.returncode == 1
    assert "Log file" in result.stdout or ".log" in result.stdout


# ── Test 8: Private key file excluded ────────────────────────────────────────

def test_private_key_excluded(tmp_path):
    archive = make_archive(tmp_path, {
        "app/__init__.py": "",
        "run.py": "",
        "requirements.txt": "",
        "migrations/001.sql": "SELECT 1;\n",
        "ssl/server.key": "FAKE_KEY_CONTENT_FOR_TEST\n",
    })
    result = run_verifier(archive)
    assert result.returncode == 1
    assert "Certificate/key" in result.stdout or ".key" in result.stdout


# ── Test 9: Database dump excluded ───────────────────────────────────────────

def test_database_dump_excluded(tmp_path):
    archive = make_archive(tmp_path, {
        "app/__init__.py": "",
        "run.py": "",
        "requirements.txt": "",
        "migrations/001.sql": "SELECT 1;\n",
        "db_backup.dump": "PGDMP fake dump content\n",
    })
    result = run_verifier(archive)
    assert result.returncode == 1
    assert "dump" in result.stdout.lower() or "Database dump" in result.stdout


# ── Test 10: Generated audio/recording excluded ───────────────────────────────

def test_audio_recording_excluded(tmp_path):
    archive = make_archive(tmp_path, {
        "app/__init__.py": "",
        "run.py": "",
        "requirements.txt": "",
        "migrations/001.sql": "SELECT 1;\n",
        "instance/generated/order_123.mp3": b"\xff\xfb\x90\x00".decode("latin-1"),
    })
    result = run_verifier(archive)
    assert result.returncode == 1
    assert "Audio" in result.stdout or ".mp3" in result.stdout


# ── Test 11: Path traversal entry rejected ────────────────────────────────────

def test_path_traversal_rejected(tmp_path):
    archive_path = tmp_path / "traversal.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        # Manually write a traversal entry
        info = zipfile.ZipInfo("../../../etc/passwd")
        zf.writestr(info, "root:x:0:0:root:/root:/bin/bash\n")
        zf.writestr("app/__init__.py", "")
        zf.writestr("run.py", "")
        zf.writestr("requirements.txt", "")
        zf.writestr("migrations/001.sql", "SELECT 1;\n")
    result = run_verifier(archive_path)
    assert result.returncode == 1
    assert "traversal" in result.stdout.lower() or ".." in result.stdout


# ── Test 12: Missing required env var causes backup failure ───────────────────

def test_missing_backup_env_var_exits_nonzero():
    """backup.ps1 must exit non-zero when credentials are absent."""
    result = subprocess.run(
        ["powershell", "-NonInteractive", "-Command",
         # Clear all credential env vars and run the script
         "$env:CCALL_DB_PASSWORD = ''; $env:DATABASE_URL = ''; "
         "$env:CCALL_DB_HOST = ''; $env:CCALL_DB_USER = ''; $env:CCALL_DB_NAME = ''; "
         f". '{BACKUP_PS1}'"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode != 0, (
        "backup.ps1 must exit non-zero when credentials are missing\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert "missing" in output.lower() or "aborted" in output.lower() or "environment" in output.lower()


# ── Test 13: Hardcoded password absent from backup scripts ────────────────────

def test_no_hardcoded_password_in_backup_ps1():
    """backup.ps1 must not contain any hardcoded credential value."""
    text = BACKUP_PS1.read_text(encoding="utf-8")
    # Scan for literal password assignments — must not match non-variable string values
    bad_pattern = re.compile(
        r'\$DB_PASS\s*=\s*"(?!\$)[^"]{4,}"',
        re.IGNORECASE,
    )
    matches = bad_pattern.findall(text)
    assert not matches, (
        f"Hardcoded password found in backup.ps1: {[m[:20] + '...' for m in matches]}"
    )


def test_no_hardcoded_password_in_backup_sh():
    """scripts/backup.sh must not contain hardcoded credentials."""
    text = BACKUP_SH.read_text(encoding="utf-8")
    # Look for literal password= with a real value (not a variable reference)
    bad_pattern = re.compile(
        r'(?:password|passwd|PGPASSWORD)\s*=\s*["\']?(?!\$\{|\$[A-Z_])[A-Za-z0-9]{8,}',
        re.IGNORECASE,
    )
    matches = bad_pattern.findall(text)
    assert not matches, (
        f"Hardcoded credential found in backup.sh: {matches}"
    )


# ── Test 14: Verifier redacts findings ───────────────────────────────────────

def test_verifier_redacts_secret_value(tmp_path):
    """Verifier output must not print the full AWS key value."""
    fake_aws_key = "AKIAIOSFODNN7EXAMPLE"  # well-known test key from AWS docs
    archive = make_archive(tmp_path, {
        "app/__init__.py": f"AWS_KEY = '{fake_aws_key}'\n",
        "run.py": "",
        "requirements.txt": "",
        "migrations/001.sql": "SELECT 1;\n",
    })
    result = run_verifier(archive)
    # Full key must NOT appear in output
    assert fake_aws_key not in result.stdout, (
        "Verifier must not print the full secret value"
    )
    # But it must still flag the issue
    assert result.returncode == 1


# ── Test 15: Clean archive returns zero ──────────────────────────────────────

def test_clean_archive_returns_zero(tmp_path):
    """A well-formed, secret-free source archive must pass verification."""
    archive = make_archive(tmp_path, {
        "app/__init__.py": "from flask import Flask\ndef create_app(): return Flask(__name__)\n",
        "app/services/order_service.py": "def create_order(tenant_id, data): pass\n",
        "tests/__init__.py": "",
        "tests/unit/test_example.py": "def test_pass(): assert True\n",
        "run.py": "from app import create_app\napp = create_app()\n",
        "requirements.txt": "flask>=3.0\npsycopg[binary]>=3.1\n",
        "migrations/001_initial_schema.sql": "CREATE TABLE tenants (id SERIAL PRIMARY KEY);\n",
        "migrations/002_saas_layer.sql": "CREATE TABLE orders (id SERIAL PRIMARY KEY);\n",
        ".env.example": (
            "# Example only — copy to .env and fill in real values\n"
            "FLASK_SECRET_KEY=change-me-to-a-long-random-secret-key\n"
            "DATABASE_URL=postgresql://ccall:YOUR_DEV_DB_PASSWORD@localhost:5432/ccall\n"
        ),
        ".env.production.example": (
            "# PLACEHOLDER FILE ONLY\n"
            "SECRET_KEY=REPLACE_WITH_128_CHAR_HEX_STRING\n"
            "DATABASE_URL=postgresql://ccall_app:REPLACE_DB_APP_PASSWORD@postgres:5432/ccall\n"
        ),
    })
    result = run_verifier(archive)
    assert result.returncode == 0, (
        f"Clean archive should pass but got:\n{result.stdout}\n{result.stderr}"
    )
    assert "PASS" in result.stdout
