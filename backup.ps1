# C-Call IVR — Local Dev Backup Script (Windows)
#
# Creates a PostgreSQL dump and a source-code archive (allowlist-based).
#
# REQUIRED — set in your environment or a local wrapper script.
# Do NOT hardcode credentials here.
#
#   $env:CCALL_DB_HOST     = "localhost"        # or set DATABASE_URL below
#   $env:CCALL_DB_PORT     = "5432"
#   $env:CCALL_DB_NAME     = "ccall"
#   $env:CCALL_DB_USER     = "ccall"
#   $env:CCALL_DB_PASSWORD = "<your password>"  # never commit this value
#
#   Alternatively, set a full DATABASE_URL and this script will parse it:
#   $env:DATABASE_URL = "postgresql://user:password@host:5432/dbname"
#
# OPTIONAL:
#   $env:CCALL_PROJECT_DIR = "c:\xampp_lite_8_3\www\ccall"  (default: script dir)
#   $env:CCALL_BACKUP_DIR  = "C:\ccall-backups"             (default: user home)
#   $env:CCALL_PGDUMP      = "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe"
#   $env:CCALL_KEEP_LAST   = "10"
#
# Usage:
#   .\backup.ps1                 — full backup (DB dump + source archive)
#   .\backup.ps1 -SourceOnly     — source archive only (no DB credentials needed)
#
# The script fails immediately if required credentials are missing.
# Credentials are never passed as command-line arguments or written to filenames.

param(
    [switch]$SourceOnly   # Skip DB dump; create source archive only
)

$ErrorActionPreference = "Stop"

# ── Resolve project and backup directories ────────────────────────────────────
$PROJECT_DIR = if ($env:CCALL_PROJECT_DIR) { $env:CCALL_PROJECT_DIR } else { $PSScriptRoot }
$BACKUP_ROOT = if ($env:CCALL_BACKUP_DIR)  { $env:CCALL_BACKUP_DIR  } else { Join-Path $HOME "ccall-backups" }
$PGDUMP      = if ($env:CCALL_PGDUMP)      { $env:CCALL_PGDUMP      } else { "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe" }
$KEEP_LAST   = if ($env:CCALL_KEEP_LAST)   { [int]$env:CCALL_KEEP_LAST } else { 10 }

# ── Resolve database credentials ──────────────────────────────────────────────
# Prefer explicit env vars; fall back to parsing DATABASE_URL.
$DB_HOST = $env:CCALL_DB_HOST
$DB_PORT = $env:CCALL_DB_PORT
$DB_NAME = $env:CCALL_DB_NAME
$DB_USER = $env:CCALL_DB_USER
$DB_PASS = $env:CCALL_DB_PASSWORD

if (-not $DB_PASS -and $env:DATABASE_URL) {
    # Parse postgresql://user:pass@host:port/dbname
    if ($env:DATABASE_URL -match '^postgresql://([^:]+):([^@]+)@([^:/]+):?(\d*)/(.+)$') {
        $DB_USER = if ($DB_USER) { $DB_USER } else { $Matches[1] }
        $DB_PASS = $Matches[2]
        $DB_HOST = if ($DB_HOST) { $DB_HOST } else { $Matches[3] }
        $DB_PORT = if ($DB_PORT) { $DB_PORT } else { if ($Matches[4]) { $Matches[4] } else { "5432" } }
        $DB_NAME = if ($DB_NAME) { $DB_NAME } else { $Matches[5] }
    }
}

# ── Fail closed — all required values must be present (full backup only) ─────
if (-not $SourceOnly) {
    $missing = @()
    if (-not $DB_HOST) { $missing += "CCALL_DB_HOST (or DATABASE_URL)" }
    if (-not $DB_NAME) { $missing += "CCALL_DB_NAME (or DATABASE_URL)" }
    if (-not $DB_USER) { $missing += "CCALL_DB_USER (or DATABASE_URL)" }
    if (-not $DB_PASS) { $missing += "CCALL_DB_PASSWORD (or DATABASE_URL)" }
    if ($missing.Count -gt 0) {
        Write-Error "Backup aborted — missing required environment variable(s):`n  $($missing -join "`n  ")"
        exit 1
    }
    if (-not (Test-Path $PGDUMP)) {
        Write-Error "Backup aborted — pg_dump not found at: $PGDUMP`nSet CCALL_PGDUMP env var to override."
        exit 1
    }
}

$DB_PORT = if ($DB_PORT) { $DB_PORT } else { "5432" }

# ── Timestamp and destination ─────────────────────────────────────────────────
$stamp     = Get-Date -Format "yyyy-MM-dd_HH-mm"
$backupDir = Join-Path $BACKUP_ROOT "ccall_$stamp"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

Write-Host "Backup started : $stamp"
Write-Host "Destination    : $backupDir"

# ── 1. PostgreSQL dump (skipped when -SourceOnly) ────────────────────────────
if (-not $SourceOnly) {
    # Password passed via PGPASSWORD env var (never on command line / in filenames).
    $sqlFile = Join-Path $backupDir "db_ccall_$stamp.sql"
    Write-Host "`n[1/2] Dumping database '$DB_NAME'..."

    $env:PGPASSWORD = $DB_PASS
    try {
        & $PGDUMP -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME -F plain -f $sqlFile
        if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }
        $sizeMB = [math]::Round((Get-Item $sqlFile).Length / 1MB, 2)
        Write-Host "    Saved: db_ccall_$stamp.sql ($sizeMB MB)"
    } finally {
        # Always clear the password from the environment, even on failure.
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

# ── 2. Source-code archive (allowlist, not blacklist) ─────────────────────────
#
# INCLUDED paths — only known-safe source directories are zipped.
# Sensitive files (.env, logs, certs, dumps, audio) are excluded by design
# because they are simply never added to this allowlist.
#
# NOTE: migrations/*.sql is APPLICATION SOURCE CODE, not a DB dump.
#       It is explicitly included below.
#
$INCLUDE_PATHS = @(
    "app",
    "call_engine",
    "migrations",
    "tests",
    "static",
    "scripts",
    "nginx",
    "monitoring",
    "woocommerce-plugin",
    "docs"
)
$INCLUDE_FILES = @(
    "run.py",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "docker-compose.staging.yml",
    "gunicorn.conf.py",
    ".env.example",
    ".env.production.example",
    "CLAUDE.md",
    "README.md",
    "Makefile",
    "prd.md"
)

# Patterns to NEVER include even if found inside an included directory.
# This is a safety net; the allowlist approach is the primary protection.
$EXCLUDE_PATTERNS = @(
    ".env", ".env.*",
    "*.log",
    "*.pem", "*.key", "*.crt", "*.p12", "*.pfx",
    "*.dump", "*.backup", "*.bak", "*.sql.gz",
    "*.mp3", "*.wav", "*.ogg",
    "backups", "recordings", "instance",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "*.pyc", "*.pyo",
    ".git", ".venv", "venv", "node_modules",
    "htmlcov", ".coverage"
)

function ShouldExclude([string]$relativePath) {
    $parts = $relativePath -split "[/\\]"
    foreach ($pattern in $EXCLUDE_PATTERNS) {
        foreach ($part in $parts) {
            if ($part -like $pattern) { return $true }
        }
        # Also check full relative path
        if ($relativePath -like "*$pattern*" -and $pattern -like "*.env*") { return $true }
    }
    return $false
}

$zipFile = Join-Path $backupDir "code_ccall_$stamp.zip"
$stepLabel = if ($SourceOnly) { "[1/1]" } else { "[2/2]" }
Write-Host "`n$stepLabel Creating source archive (allowlist)..."

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($zipFile, 'Create')
$fileCount = 0

# Add directories from allowlist
foreach ($relDir in $INCLUDE_PATHS) {
    $absDir = Join-Path $PROJECT_DIR $relDir
    if (-not (Test-Path $absDir)) { continue }
    $files = Get-ChildItem -Path $absDir -Recurse -File
    foreach ($file in $files) {
        $entryName = $file.FullName.Substring($PROJECT_DIR.Length + 1) -replace "\\", "/"
        if (ShouldExclude $entryName) { continue }
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file.FullName, $entryName) | Out-Null
        $fileCount++
    }
}

# Add top-level files from allowlist
foreach ($relFile in $INCLUDE_FILES) {
    $absFile = Join-Path $PROJECT_DIR $relFile
    if (-not (Test-Path $absFile)) { continue }
    $entryName = $relFile -replace "\\", "/"
    if (ShouldExclude $entryName) { continue }
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $absFile, $entryName) | Out-Null
    $fileCount++
}

$zip.Dispose()

$sizeMB = [math]::Round((Get-Item $zipFile).Length / 1MB, 2)
Write-Host "    Saved: code_ccall_$stamp.zip ($sizeMB MB, $fileCount files)"

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host "`n=== Backup complete ==="
Write-Host "Folder: $backupDir"
Get-ChildItem $backupDir | Format-Table Name, @{L="Size (MB)";E={[math]::Round($_.Length/1MB,2)}} -AutoSize

# ── Rotate old backups ────────────────────────────────────────────────────────
$allBackups = Get-ChildItem $BACKUP_ROOT -Directory -ErrorAction SilentlyContinue | Sort-Object Name
if ($allBackups.Count -gt $KEEP_LAST) {
    $toDelete = $allBackups | Select-Object -First ($allBackups.Count - $KEEP_LAST)
    foreach ($old in $toDelete) {
        Write-Host "Removing old backup: $($old.Name)"
        Remove-Item $old.FullName -Recurse -Force
    }
}

Write-Host "Done."
