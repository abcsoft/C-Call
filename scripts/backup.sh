#!/usr/bin/env bash
# scripts/backup.sh — C-Call IVR production backup
#
# Creates an encrypted, checksummed PostgreSQL logical backup.
# Optionally uploads to S3-compatible storage.
#
# Required env vars:
#   DATABASE_URL              — PostgreSQL connection string
#   BACKUP_ENCRYPTION_PASSPHRASE — GPG symmetric passphrase
#   BACKUP_RETENTION_DAYS     — local retention (default: 30)
#   BACKUP_DIR                — local backup directory (default: /var/backups/ccall)
#   BACKUP_S3_BUCKET          — S3 bucket (optional)
#   BACKUP_S3_PREFIX          — S3 key prefix (default: ccall-prod)
#
# Non-zero exit codes:
#   1 — missing environment / dependency
#   2 — pg_dump failed
#   3 — encryption failed
#   4 — checksum mismatch
#   5 — S3 upload failed
#
set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
BACKUP_DIR="${BACKUP_DIR:-/var/backups/ccall}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
S3_BUCKET="${BACKUP_S3_BUCKET:-}"
S3_PREFIX="${BACKUP_S3_PREFIX:-ccall-prod}"
TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
LOCK_FILE="/tmp/ccall_backup.lock"
LOG_PREFIX="[BACKUP $TIMESTAMP]"

# ── Dependency check ─────────────────────────────────────────────────────────
for cmd in pg_dump gpg sha256sum; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "$LOG_PREFIX ERROR: $cmd not found." >&2; exit 1; }
done
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "$LOG_PREFIX ERROR: DATABASE_URL not set." >&2; exit 1
fi
if [[ -z "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ]]; then
    echo "$LOG_PREFIX ERROR: BACKUP_ENCRYPTION_PASSPHRASE not set." >&2; exit 1
fi

# ── Exclusive lock ───────────────────────────────────────────────────────────
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "$LOG_PREFIX ERROR: Another backup is running." >&2; exit 1; }

mkdir -p "$BACKUP_DIR"

DUMP_FILE="$BACKUP_DIR/ccall_${TIMESTAMP}.dump"
ENC_FILE="${DUMP_FILE}.gpg"
SUM_FILE="${ENC_FILE}.sha256"

echo "$LOG_PREFIX Starting PostgreSQL dump..."

# ── pg_dump ───────────────────────────────────────────────────────────────────
if ! pg_dump "$DATABASE_URL" \
    --format=custom \
    --compress=9 \
    --no-password \
    --file="$DUMP_FILE" 2>&1; then
    echo "$LOG_PREFIX ERROR: pg_dump failed." >&2; exit 2
fi
DUMP_SIZE=$(du -sh "$DUMP_FILE" | cut -f1)
echo "$LOG_PREFIX Dump complete. Size: $DUMP_SIZE"

# ── Encrypt ──────────────────────────────────────────────────────────────────
echo "$LOG_PREFIX Encrypting backup..."
if ! gpg --batch --yes \
    --passphrase "$BACKUP_ENCRYPTION_PASSPHRASE" \
    --symmetric --cipher-algo AES256 \
    --output "$ENC_FILE" "$DUMP_FILE" 2>&1; then
    echo "$LOG_PREFIX ERROR: Encryption failed." >&2
    rm -f "$DUMP_FILE"
    exit 3
fi
rm -f "$DUMP_FILE"   # remove plaintext immediately after encryption

# ── Checksum ─────────────────────────────────────────────────────────────────
sha256sum "$ENC_FILE" > "$SUM_FILE"
echo "$LOG_PREFIX Checksum: $(cat $SUM_FILE)"

# ── Verify checksum ───────────────────────────────────────────────────────────
if ! sha256sum --check "$SUM_FILE" --quiet 2>&1; then
    echo "$LOG_PREFIX ERROR: Checksum verification failed." >&2; exit 4
fi
echo "$LOG_PREFIX Checksum verified."

# ── S3 upload ─────────────────────────────────────────────────────────────────
if [[ -n "$S3_BUCKET" ]]; then
    command -v aws >/dev/null 2>&1 || { echo "$LOG_PREFIX WARNING: aws CLI not found, skipping S3 upload." >&2; }
    echo "$LOG_PREFIX Uploading to s3://$S3_BUCKET/$S3_PREFIX/ ..."
    if ! aws s3 cp "$ENC_FILE" "s3://$S3_BUCKET/$S3_PREFIX/$(basename $ENC_FILE)" 2>&1 || \
       ! aws s3 cp "$SUM_FILE" "s3://$S3_BUCKET/$S3_PREFIX/$(basename $SUM_FILE)" 2>&1; then
        echo "$LOG_PREFIX ERROR: S3 upload failed." >&2; exit 5
    fi
    echo "$LOG_PREFIX S3 upload complete."
fi

# ── Local retention cleanup ───────────────────────────────────────────────────
echo "$LOG_PREFIX Removing backups older than $RETENTION_DAYS days..."
find "$BACKUP_DIR" -name "ccall_*.gpg" -mtime "+$RETENTION_DAYS" -delete
find "$BACKUP_DIR" -name "ccall_*.sha256" -mtime "+$RETENTION_DAYS" -delete

ENC_SIZE=$(du -sh "$ENC_FILE" | cut -f1)
echo "$LOG_PREFIX Backup complete. Encrypted file: $ENC_FILE ($ENC_SIZE)"

# ── Prometheus success timestamp (atomic write) ───────────────────────────────
# Written after all checks pass. Failures before this line do not update the
# timestamp, so the alert fires if backup.sh exits non-zero.
METRICS_DIR="${BACKUP_METRICS_DIR:-/app/instance/metrics}"
mkdir -p "$METRICS_DIR"
EPOCH=$(date +%s)
TMP_PROM="${METRICS_DIR}/.ccall_backup.prom.tmp"
{
    printf '# HELP ccall_last_backup_success_timestamp Unix timestamp of last successful backup\n'
    printf '# TYPE ccall_last_backup_success_timestamp gauge\n'
    printf 'ccall_last_backup_success_timestamp %s\n' "$EPOCH"
} > "$TMP_PROM"
mv "$TMP_PROM" "${METRICS_DIR}/ccall_backup.prom"
echo "$LOG_PREFIX Wrote success timestamp: $EPOCH"
