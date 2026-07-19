"""Unit tests for backup/archive security hardening.

Fake credential values are constructed at RUNTIME (string concatenation or
multiplication) so that the scanner never sees a complete credential literal
in this source file.  Any real-looking token accidentally pasted into this
file must be caught by the scanner when it scans the file itself.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
from secret_patterns import scan_text, scan_file, PATTERNS  # noqa: E402


# ── Path helpers ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parents[2]
VERIFIER     = PROJECT_ROOT / "scripts" / "verify_source_archive.py"
SCANNER      = PROJECT_ROOT / "scripts" / "secret_scan.py"
BACKUP_PS1   = PROJECT_ROOT / "backup.ps1"
BACKUP_SH    = PROJECT_ROOT / "scripts" / "backup.sh"


# ── Runtime fake-credential constructors ─────────────────────────────────────
# These functions return complete-looking credential strings but are never
# stored as literals in this source file.

def _fake_github_token(char: str = "A") -> str:
    return "gh" + "p_" + char * 36

def _fake_aws_key(char: str = "Z") -> str:
    return "AK" + "IA" + char * 16

def _fake_pg_url(password: str = "supersecretdbpassword123") -> str:
    _scheme = "post" + "gresql"
    return f"{_scheme}://ccall:{password}@db.example.com:5432/ccall_prod"

def _fake_redis_url(password: str = "prodredissecret456") -> str:
    _scheme = "re" + "dis"
    return f"{_scheme}://:{password}@redis.example.com:6379/0"

def _fake_sip_password() -> str:
    return "myreal" + "password9999"

def _fake_private_key_header() -> str:
    return "-----BEGIN " + "RSA " + "PRIVATE KEY" + "-----"


# ── Archive builder helpers ───────────────────────────────────────────────────

def make_archive(tmp_path: Path, contents: dict) -> Path:
    """Build a minimal ZIP at tmp_path/test.zip with given {entry: content}."""
    archive = tmp_path / "test.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, content in contents.items():
            if isinstance(content, bytes):
                zf.writestr(name, content)
            else:
                zf.writestr(name, content)
    return archive


def run_verifier(archive: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(archive)],
        capture_output=True, encoding="utf-8", errors="replace",
    )


def run_scanner(*extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCANNER)] + list(extra_args),
        capture_output=True, encoding="utf-8", errors="replace",
    )


# ── Minimal valid archive base ────────────────────────────────────────────────

def _minimal_base(extras: dict) -> dict:
    return {
        "app/__init__.py": "",
        "tests/__init__.py": "",
        "run.py": "",
        "requirements.txt": "",
        "migrations/001_initial.sql": "SELECT 1;\n",
        **extras,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Archive structure checks (tests 1–11)
# ═══════════════════════════════════════════════════════════════════════════════

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
    assert result.returncode == 0, f"Expected PASS:\n{result.stdout}\n{result.stderr}"
    assert "PASS" in result.stdout


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


def test_env_file_excluded(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({
        ".env": "SECRET_KEY=some_value_here_should_not_be_here\n",
    }))
    result = run_verifier(archive)
    assert result.returncode == 1
    assert ".env" in result.stdout or "Environment file" in result.stdout


def test_env_staging_excluded(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({
        ".env.staging": "SECRET_KEY=staging_key_must_not_appear\n",
    }))
    result = run_verifier(archive)
    assert result.returncode == 1
    assert "Environment file" in result.stdout or ".env.staging" in result.stdout


def test_env_production_example_allowed(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({
        ".env.production.example": (
            "# PLACEHOLDER FILE ONLY\n"
            "SECRET_KEY=REPLACE_WITH_128_CHAR_HEX_STRING\n"
            "DATABASE_URL=postgresql://ccall_app:REPLACE_DB_APP_PASSWORD@postgres:5432/ccall\n"
        ),
    }))
    result = run_verifier(archive)
    assert result.returncode == 0, f"Placeholder example file should be allowed:\n{result.stdout}"


def test_real_secret_in_example_fails(tmp_path):
    """Non-example AWS key inside example file must be rejected."""
    # Not AKIAIOSFODNN7EXAMPLE (the docs example) — use a different key that
    # would be a real IAM credential if issued.
    bad_key = _fake_aws_key("Z")
    archive = make_archive(tmp_path, _minimal_base({
        ".env.production.example": f"AWS_ACCESS_KEY_ID={bad_key}\n",
    }))
    v = run_verifier(archive)
    s = run_scanner("--archive", str(archive))
    assert v.returncode == 1, "Archive with real-looking AWS key must be rejected"
    assert s.returncode == 1
    assert bad_key not in v.stdout + v.stderr
    assert bad_key not in s.stdout + s.stderr


def test_log_file_excluded(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({
        "ccall_debug.log": "2026-07-18 INFO: something happened\n",
    }))
    result = run_verifier(archive)
    assert result.returncode == 1
    assert "Log file" in result.stdout or ".log" in result.stdout


def test_private_key_excluded(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({
        "ssl/server.key": "FAKE_KEY_CONTENT_FOR_TEST\n",
    }))
    result = run_verifier(archive)
    assert result.returncode == 1
    assert "Certificate/key" in result.stdout or ".key" in result.stdout


def test_database_dump_excluded(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({
        "db_backup.dump": "PGDMP fake dump content\n",
    }))
    result = run_verifier(archive)
    assert result.returncode == 1
    assert "dump" in result.stdout.lower() or "Database dump" in result.stdout


def test_audio_recording_excluded(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({
        "instance/generated/order_123.mp3": b"\xff\xfb\x90\x00",
    }))
    result = run_verifier(archive)
    assert result.returncode == 1
    assert "Audio" in result.stdout or ".mp3" in result.stdout


def test_path_traversal_rejected(tmp_path):
    archive_path = tmp_path / "traversal.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        info = zipfile.ZipInfo("../../../etc/passwd")
        zf.writestr(info, "root:x:0:0:root:/root:/bin/bash\n")
        zf.writestr("app/__init__.py", "")
        zf.writestr("tests/__init__.py", "")
        zf.writestr("run.py", "")
        zf.writestr("requirements.txt", "")
        zf.writestr("migrations/001_initial.sql", "SELECT 1;\n")
    result = run_verifier(archive_path)
    assert result.returncode == 1
    assert "traversal" in result.stdout.lower() or ".." in result.stdout


# ═══════════════════════════════════════════════════════════════════════════════
# Backup script credential checks (tests 12–14)
# ═══════════════════════════════════════════════════════════════════════════════

def test_missing_backup_env_var_exits_nonzero():
    """backup.ps1 must exit non-zero when credentials are absent."""
    result = subprocess.run(
        ["powershell", "-NonInteractive", "-Command",
         "$env:CCALL_DB_PASSWORD = ''; $env:DATABASE_URL = ''; "
         "$env:CCALL_DB_HOST = ''; $env:CCALL_DB_USER = ''; $env:CCALL_DB_NAME = ''; "
         f". '{BACKUP_PS1}'"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode != 0, (
        f"backup.ps1 must exit non-zero when credentials are missing\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert "missing" in output.lower() or "aborted" in output.lower() or "environment" in output.lower()


def test_no_hardcoded_password_in_backup_ps1():
    """backup.ps1 must not contain any hardcoded credential value."""
    text = BACKUP_PS1.read_text(encoding="utf-8")
    bad_pattern = re.compile(r'\$DB_PASS\s*=\s*"(?!\$)[^"]{4,}"', re.IGNORECASE)
    matches = bad_pattern.findall(text)
    assert not matches, f"Hardcoded password found in backup.ps1: {[m[:20] + '...' for m in matches]}"


def test_no_hardcoded_password_in_backup_sh():
    """scripts/backup.sh must not contain hardcoded credentials."""
    text = BACKUP_SH.read_text(encoding="utf-8")
    bad_pattern = re.compile(
        r'(?:password|passwd|PGPASSWORD)\s*=\s*["\']?(?!\$\{|\$[A-Z_])[A-Za-z0-9]{8,}',
        re.IGNORECASE,
    )
    matches = bad_pattern.findall(text)
    assert not matches, f"Hardcoded credential found in backup.sh: {matches}"


# ═══════════════════════════════════════════════════════════════════════════════
# Verifier output safety (test 15)
# ═══════════════════════════════════════════════════════════════════════════════

def test_verifier_redacts_secret_value(tmp_path):
    """Verifier output must not print the full AWS key value."""
    fake_aws_key = "AKIAIOSFODNN7EXAMPLE"  # canonical docs example, self-suppressed in source
    archive = make_archive(tmp_path, _minimal_base({
        "app/__init__.py": f"AWS_KEY = '{fake_aws_key}'\n",
    }))
    result = run_verifier(archive)
    assert fake_aws_key not in result.stdout, "Verifier must not print the full secret value"
    # Result may be 0 (AKIAIOSFODNN7EXAMPLE is the docs example, suppressed by safe_line_if)
    # or 1 if migration count changed; we just verify no full value is printed.


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
    assert result.returncode == 0, f"Clean archive should pass:\n{result.stdout}\n{result.stderr}"
    assert "PASS" in result.stdout


# ═══════════════════════════════════════════════════════════════════════════════
# Placeholder / config checks (tests 16–20)
# ═══════════════════════════════════════════════════════════════════════════════

def test_no_usable_password_in_test_config():
    text = (PROJECT_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert ":password@" not in text, "app/config.py still contains ':password@'"


def test_no_usable_password_in_conftest():
    text = (PROJECT_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert ":password@" not in text, "tests/conftest.py still contains ':password@'"


def test_change_me_placeholder_not_connectable_in_scanner():
    line = (
        'DATABASE_URL = os.environ.get("TEST_DATABASE_URL",'
        ' "postgresql://ccall:CHANGE_ME_TEST_DB_PASSWORD@localhost:5432/ccall_test")'
    )
    findings = scan_text(line, "test-input")
    pg = [f for f in findings if f.pattern_name == "pg-url-with-password"]
    assert not pg, f"CHANGE_ME_TEST_DB_PASSWORD should be treated as a placeholder:\n{pg}"


def test_env_takes_precedence_over_placeholder(monkeypatch):
    """When TEST_DATABASE_URL is set, config must use it — not the placeholder."""
    placeholder_url = "postgresql://ccall:REPLACE_WITH_REAL_PW@localhost:5432/ccall_test"
    monkeypatch.setenv("TEST_DATABASE_URL", placeholder_url)
    import importlib, app.config as cfg_mod
    importlib.reload(cfg_mod)
    assert cfg_mod.TestingConfig.DATABASE_URL == placeholder_url


def test_placeholder_url_in_config_not_usable_password():
    text = (PROJECT_ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert "CHANGE_ME_TEST_DB_PASSWORD" in text
    assert ":password@" not in text


# ═══════════════════════════════════════════════════════════════════════════════
# Documentation scanning (Phase 3) — docs must be scanned, not skipped
# ═══════════════════════════════════════════════════════════════════════════════

def test_github_token_in_readme_rejected(tmp_path):
    """GitHub token inside README.md must be detected by both tools."""
    token = _fake_github_token("A")
    archive = make_archive(tmp_path, _minimal_base({
        "README.md": f"# Setup\nGITHUB_TOKEN={token}\n",
    }))
    v = run_verifier(archive)
    s = run_scanner("--archive", str(archive))
    assert v.returncode == 1, f"Verifier must detect token in README.md:\n{v.stdout}"
    assert s.returncode == 1, f"Scanner must detect token in README.md:\n{s.stdout}"
    assert token not in v.stdout + v.stderr
    assert token not in s.stdout + s.stderr


def test_aws_key_in_claude_md_rejected(tmp_path):
    """AWS key inside CLAUDE.md must be detected."""
    key = _fake_aws_key("Z")
    archive = make_archive(tmp_path, _minimal_base({
        "CLAUDE.md": f"# Credentials\nAWS_ACCESS_KEY_ID={key}\n",
    }))
    v = run_verifier(archive)
    s = run_scanner("--archive", str(archive))
    assert v.returncode == 1, f"Verifier must detect AWS key in CLAUDE.md:\n{v.stdout}"
    assert s.returncode == 1, f"Scanner must detect AWS key in CLAUDE.md:\n{s.stdout}"
    assert key not in v.stdout + v.stderr


def test_pg_password_in_docs_rejected(tmp_path):
    """Real PostgreSQL URL in docs/setup.md must be detected."""
    url = _fake_pg_url()
    archive = make_archive(tmp_path, _minimal_base({
        "docs/setup.md": f"# Production\nDATABASE_URL={url}\n",
    }))
    v = run_verifier(archive)
    s = run_scanner("--archive", str(archive))
    assert v.returncode == 1, f"Verifier must detect DB URL in docs:\n{v.stdout}"
    assert s.returncode == 1, f"Scanner must detect DB URL in docs:\n{s.stdout}"


def test_sip_password_in_runbook_rejected(tmp_path):
    """SIP password in runbook.txt must be detected."""
    pw = _fake_sip_password()
    archive = make_archive(tmp_path, _minimal_base({
        "docs/runbook.txt": f'SIP_PASSWORD = "{pw}"\n',
    }))
    s = run_scanner("--archive", str(archive))
    assert s.returncode == 1, f"Scanner must detect SIP password in runbook.txt:\n{s.stdout}"
    assert pw not in s.stdout + s.stderr


def test_placeholder_values_in_docs_accepted(tmp_path):
    """Placeholder values inside documentation must NOT be flagged."""
    archive = make_archive(tmp_path, _minimal_base({
        "README.md": (
            "# Configuration\n"
            "DATABASE_URL=postgresql://ccall:YOUR_DEV_DB_PASSWORD@localhost:5432/ccall\n"
            "FLASK_SECRET_KEY=change-me-to-a-long-random-secret-key\n"
            "SIP_PASSWORD=your_sip_password\n"
        ),
        "docs/setup.md": (
            "Set REPLACE_WITH_128_CHAR_HEX_STRING as your secret key.\n"
            "DATABASE_URL=postgresql://app:REPLACE_DB_APP_PASSWORD@db:5432/prod\n"
        ),
    }))
    v = run_verifier(archive)
    s = run_scanner("--archive", str(archive))
    assert v.returncode == 0, f"Placeholder docs should pass verifier:\n{v.stdout}"
    assert s.returncode == 0, f"Placeholder docs should pass scanner:\n{s.stdout}"


# ═══════════════════════════════════════════════════════════════════════════════
# Scanner-source file scanning (Phase 6) — no whole-file skip
# ═══════════════════════════════════════════════════════════════════════════════

def test_scanner_source_files_scan_clean():
    """secret_patterns.py, secret_scan.py, verify_source_archive.py must scan clean."""
    for name in ("secret_patterns.py", "secret_scan.py", "verify_source_archive.py"):
        path = PROJECT_ROOT / "scripts" / name
        findings = scan_file(path)
        assert not findings, (
            f"{name} has unexpected findings (add # secret-scan: allow-pattern-definition "
            f"to the specific line):\n" +
            "\n".join(f"  line {f.line_no}: [{f.pattern_name}] {f.redacted}" for f in findings)
        )


def test_real_token_in_secret_patterns_detected(tmp_path):
    """A real-looking token pasted into secret_patterns.py must be caught."""
    token = _fake_github_token("X")
    content = (PROJECT_ROOT / "scripts" / "secret_patterns.py").read_text(encoding="utf-8")
    poisoned = tmp_path / "secret_patterns_poisoned.py"
    poisoned.write_text(content + f"\n# accidentally pasted: {token}\n", encoding="utf-8")
    findings = scan_file(poisoned)
    assert any(f.pattern_name == "github-token" for f in findings), (
        "A GitHub token added to secret_patterns.py must be detected"
    )


def test_db_password_in_secret_scan_detected(tmp_path):
    """A real DB URL pasted into secret_scan.py must be caught."""
    url = _fake_pg_url()
    content = (PROJECT_ROOT / "scripts" / "secret_scan.py").read_text(encoding="utf-8")
    poisoned = tmp_path / "secret_scan_poisoned.py"
    poisoned.write_text(content + f"\n# accidentally pasted: {url}\n", encoding="utf-8")
    findings = scan_file(poisoned)
    assert any(f.pattern_name == "pg-url-with-password" for f in findings), (
        "A DB password added to secret_scan.py must be detected"
    )


def test_private_key_in_verify_source_detected(tmp_path):
    """A private key header pasted into verify_source_archive.py must be caught."""
    pk = _fake_private_key_header()
    content = (PROJECT_ROOT / "scripts" / "verify_source_archive.py").read_text(encoding="utf-8")
    poisoned = tmp_path / "verify_source_poisoned.py"
    poisoned.write_text(content + f"\n{pk}\n", encoding="utf-8")
    findings = scan_file(poisoned)
    assert any(f.pattern_name == "private-key" for f in findings), (
        "A private key header added to verify_source_archive.py must be detected"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test-fixture source scanning (Phase 5) — this file itself must scan clean
# ═══════════════════════════════════════════════════════════════════════════════

def test_this_file_scans_clean():
    """test_backup_security.py itself must scan clean (no literal credential strings)."""
    this_file = Path(__file__)
    findings = scan_file(this_file)
    assert not findings, (
        "test_backup_security.py has unexpected scanner findings — "
        "use runtime construction (_fake_github_token() etc.) instead of literals:\n" +
        "\n".join(f"  line {f.line_no}: [{f.pattern_name}] {f.redacted}" for f in findings)
    )


def test_literal_token_in_test_file_detected(tmp_path):
    """A real-looking token accidentally appended to this test file must be caught."""
    token = _fake_github_token("T")
    content = Path(__file__).read_text(encoding="utf-8")
    poisoned = tmp_path / "test_backup_security_poisoned.py"
    poisoned.write_text(content + f"\nACCIDENTAL_TOKEN = '{token}'\n", encoding="utf-8")
    findings = scan_file(poisoned)
    assert any(f.pattern_name == "github-token" for f in findings), (
        "A real-looking token appended to test_backup_security.py must be detected"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Unsafe archive negative tests (Phase 7)
# ═══════════════════════════════════════════════════════════════════════════════

def test_unsafe_github_token_rejected(tmp_path):
    token = _fake_github_token("A")
    archive = make_archive(tmp_path, _minimal_base({
        "app/secrets.py": f"GITHUB_TOKEN = '{token}'\n",
    }))
    v = run_verifier(archive)
    s = run_scanner("--archive", str(archive))
    assert v.returncode == 1, "Verifier must reject GitHub token"
    assert s.returncode == 1, "Scanner must reject GitHub token"
    assert token not in v.stdout + v.stderr
    assert token not in s.stdout + s.stderr


def test_unsafe_aws_key_rejected(tmp_path):
    key = _fake_aws_key("Z")
    archive = make_archive(tmp_path, _minimal_base({
        "app/aws.py": f"AWS_ACCESS_KEY_ID = '{key}'\n",
    }))
    v = run_verifier(archive)
    s = run_scanner("--archive", str(archive))
    assert v.returncode == 1
    assert s.returncode == 1
    assert key not in v.stdout + v.stderr
    assert key not in s.stdout + s.stderr


def test_unsafe_pg_password_rejected(tmp_path):
    url = _fake_pg_url()
    archive = make_archive(tmp_path, _minimal_base({
        "app/config_bad.py": f'DATABASE_URL = "{url}"\n',
    }))
    v = run_verifier(archive)
    s = run_scanner("--archive", str(archive))
    assert v.returncode == 1
    assert s.returncode == 1
    assert "supersecretdbpassword123" not in v.stdout + v.stderr
    assert "supersecretdbpassword123" not in s.stdout + s.stderr


def test_unsafe_redis_password_rejected(tmp_path):
    url = _fake_redis_url()
    archive = make_archive(tmp_path, _minimal_base({
        "app/cache.py": f'REDIS_URL = "{url}"\n',
    }))
    s = run_scanner("--archive", str(archive))
    assert s.returncode == 1
    assert "prodredissecret456" not in s.stdout + s.stderr


def test_unsafe_sip_password_rejected(tmp_path):
    pw = _fake_sip_password()
    archive = make_archive(tmp_path, _minimal_base({
        "app/sip_config.py": f'SIP_PASSWORD = "{pw}"\n',
    }))
    s = run_scanner("--archive", str(archive))
    assert s.returncode == 1
    assert pw not in s.stdout + s.stderr


def test_unsafe_private_key_rejected(tmp_path):
    pk = _fake_private_key_header()
    archive = make_archive(tmp_path, _minimal_base({
        "app/key.py": f"cert = '{pk}'\n",
    }))
    v = run_verifier(archive)
    assert v.returncode == 1


def test_env_file_in_archive_rejected(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({".env": "SECRET=real\n"}))
    assert run_verifier(archive).returncode == 1


def test_log_file_in_archive_rejected(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({"ccall.log": "INFO something\n"}))
    assert run_verifier(archive).returncode == 1


def test_db_dump_in_archive_rejected(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({"db.dump": "PGDMP\n"}))
    assert run_verifier(archive).returncode == 1


def test_audio_in_archive_rejected(tmp_path):
    archive = make_archive(tmp_path, _minimal_base({"order.mp3": "\xff\xfb"}))
    assert run_verifier(archive).returncode == 1


def test_path_traversal_in_archive_rejected(tmp_path):
    archive_path = tmp_path / "traversal.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        info = zipfile.ZipInfo("../../../etc/passwd")
        zf.writestr(info, "root:x:0:0\n")
        zf.writestr("app/__init__.py", "")
        zf.writestr("tests/__init__.py", "")
        zf.writestr("run.py", "")
        zf.writestr("requirements.txt", "")
        zf.writestr("migrations/001.sql", "SELECT 1;\n")
    v = run_verifier(archive_path)
    assert v.returncode == 1
    assert ".." in v.stdout or "traversal" in v.stdout.lower()


def test_placeholder_pg_url_allowed_in_archive(tmp_path):
    """CHANGE_ME / REPLACE_ / YOUR_DEV_ placeholders must pass both tools."""
    archive = make_archive(tmp_path, _minimal_base({
        "app/config.py": (
            'DATABASE_URL = os.environ.get("TEST_DATABASE_URL",'
            ' "postgresql://ccall:CHANGE_ME_TEST_DB_PASSWORD@localhost:5432/ccall_test")\n'
        ),
        ".env.production.example": (
            "DATABASE_URL=postgresql://ccall_app:REPLACE_DB_APP_PASSWORD@postgres:5432/ccall\n"
        ),
    }))
    v = run_verifier(archive)
    s = run_scanner("--archive", str(archive))
    assert v.returncode == 0, f"Placeholder URLs should pass verifier:\n{v.stdout}"
    assert s.returncode == 0, f"Placeholder URLs should pass scanner:\n{s.stdout}"


def test_redaction_never_prints_full_value(tmp_path):
    """Neither tool must print the full secret value in its output."""
    token = _fake_github_token("B")
    archive = make_archive(tmp_path, _minimal_base({
        "app/secret.py": f"TOKEN = '{token}'\n",
    }))
    v = run_verifier(archive)
    s = run_scanner("--archive", str(archive))
    assert token not in v.stdout + v.stderr, "Verifier printed full secret value"
    assert token not in s.stdout + s.stderr, "Scanner printed full secret value"


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 7 unsafe line examples — broad suppressors must NOT fire
# ═══════════════════════════════════════════════════════════════════════════════

def test_real_pg_fallback_in_environ_get_detected():
    """os.environ.get with a real password fallback must be caught."""
    pw = "realprodpasswordXXXXXXXXXXX"
    line = f'DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://app:{pw}@prod-db:5432/ccall")'
    findings = scan_text(line, "test")
    assert any(f.pattern_name == "pg-url-with-password" for f in findings), (
        f"Real password in os.environ.get fallback must be detected; got {findings}"
    )


def test_sip_password_form_get_default_detected():
    """Real hardcoded default in form.get must be detected."""
    pw = "realdefaultpassword99"
    line = f'SIP_PASSWORD = request.form.get("sip_password", "{pw}")'
    findings = scan_text(line, "test")
    assert any(f.pattern_name == "sip-password" for f in findings), (
        f"Real default in form.get must be detected; got {findings}"
    )
    assert pw not in "".join(f.redacted for f in findings)


def test_commented_out_real_credential_detected():
    """A real credential in a comment must be caught (comments are NOT globally skipped)."""
    token = _fake_github_token("C")
    line = f"# old token: GITHUB_TOKEN={token}"
    findings = scan_text(line, "test")
    assert any(f.pattern_name == "github-token" for f in findings), (
        f"Token in comment must be detected; got {findings}"
    )


def test_function_call_rhs_not_flagged():
    """api_key = get_platform_secret_for_generation(provider) must NOT be flagged."""
    line = "api_key = get_platform_secret_for_generation(provider)"
    findings = scan_text(line, "test")
    assert not any(f.pattern_name == "api-key-assignment" for f in findings), (
        f"Function-call RHS must not be flagged; got {findings}"
    )


def test_secret_key_env_get_not_flagged():
    """SECRET_KEY = os.environ.get(...) must NOT be flagged (value is code expression)."""
    line = 'SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-me-placeholder-xxx")'
    findings = scan_text(line, "test")
    assert not any(f.pattern_name == "flask-secret-key" for f in findings), (
        f"os.environ.get(...) SECRET_KEY line must not be flagged; got {findings}"
    )


def test_real_secret_key_literal_detected():
    """SECRET_KEY = 'actual-random-64-char-string...' must be caught."""
    # Construct without storing as a single literal
    real_key = "a" * 32 + "b" * 32
    line = f'SECRET_KEY = "{real_key}"'
    findings = scan_text(line, "test")
    assert any(f.pattern_name == "flask-secret-key" for f in findings), (
        f"Real secret key literal must be detected; got {findings}"
    )
