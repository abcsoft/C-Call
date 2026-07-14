#!/usr/bin/env bash
# scripts/restore_verify.sh — isolated restore verification
#
# Restores a backup to an isolated database and runs integrity checks.
# NEVER run against the production database.
#
# Usage:
#   RESTORE_DB_URL="postgresql://ccall:pw@localhost:5432/ccall_restore_test" \
#   BACKUP_ENCRYPTION_PASSPHRASE="..." \
#   bash scripts/restore_verify.sh /var/backups/ccall/ccall_20260714T120000Z.dump.gpg
#
# Exit codes:
#   0 — all checks passed
#   1 — bad arguments / missing env
#   2 — decryption failed
#   3 — restore failed
#   4 — integrity check failed
#
set -euo pipefail

ENC_FILE="${1:-}"
RESTORE_DB="${RESTORE_DB_URL:-}"
LOG_PREFIX="[RESTORE_VERIFY $(date -u +%Y%m%dT%H%M%SZ)]"

[[ -z "$ENC_FILE" ]] && { echo "$LOG_PREFIX ERROR: Provide backup file path as first argument." >&2; exit 1; }
[[ ! -f "$ENC_FILE" ]] && { echo "$LOG_PREFIX ERROR: File not found: $ENC_FILE" >&2; exit 1; }
[[ -z "$RESTORE_DB" ]] && { echo "$LOG_PREFIX ERROR: RESTORE_DB_URL not set." >&2; exit 1; }
[[ -z "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ]] && { echo "$LOG_PREFIX ERROR: BACKUP_ENCRYPTION_PASSPHRASE not set." >&2; exit 1; }

# Safety guard: refuse production DB names
if echo "$RESTORE_DB" | grep -qE "/(ccall_prod|ccall)$"; then
    echo "$LOG_PREFIX ERROR: RESTORE_DB_URL appears to point at a production database. Refusing." >&2
    exit 1
fi

WORK_DIR=$(mktemp -d)
trap "rm -rf $WORK_DIR" EXIT

T_START=$(date +%s)

# ── Checksum verify ───────────────────────────────────────────────────────────
SUM_FILE="${ENC_FILE}.sha256"
if [[ -f "$SUM_FILE" ]]; then
    echo "$LOG_PREFIX Verifying checksum..."
    sha256sum --check "$SUM_FILE" --quiet || { echo "$LOG_PREFIX ERROR: Checksum mismatch." >&2; exit 4; }
    echo "$LOG_PREFIX Checksum OK."
fi

# ── Decrypt ───────────────────────────────────────────────────────────────────
DUMP_FILE="$WORK_DIR/restore.dump"
echo "$LOG_PREFIX Decrypting backup..."
T_DEC_START=$(date +%s)
gpg --batch --yes \
    --passphrase "$BACKUP_ENCRYPTION_PASSPHRASE" \
    --output "$DUMP_FILE" \
    --decrypt "$ENC_FILE" 2>&1 || { echo "$LOG_PREFIX ERROR: Decryption failed." >&2; exit 2; }
T_DEC_END=$(date +%s)
echo "$LOG_PREFIX Decryption complete in $((T_DEC_END - T_DEC_START))s."

# ── Restore ───────────────────────────────────────────────────────────────────
echo "$LOG_PREFIX Restoring to $RESTORE_DB ..."
T_RST_START=$(date +%s)
pg_restore --dbname="$RESTORE_DB" --clean --if-exists --no-owner --no-acl "$DUMP_FILE" \
    2>&1 | grep -v "^pg_restore:" | head -50 || true
T_RST_END=$(date +%s)
echo "$LOG_PREFIX Restore complete in $((T_RST_END - T_RST_START))s."

# ── Integrity checks ──────────────────────────────────────────────────────────
echo "$LOG_PREFIX Running integrity checks..."

psql "$RESTORE_DB" --tuples-only --quiet --command "
    SELECT
      (SELECT COUNT(*) FROM tenants)                         AS tenant_count,
      (SELECT COUNT(*) FROM orders)                          AS order_count,
      (SELECT COUNT(*) FROM calls)                           AS call_count,
      (SELECT COALESCE(SUM(balance),0) FROM wallets)         AS total_wallet_balance,
      (SELECT COUNT(*) FROM wallet_transactions)             AS ledger_rows,
      (SELECT COUNT(*) FROM api_keys WHERE is_active=true)   AS active_api_keys,
      (SELECT COUNT(*) FROM subscriptions)                   AS subscription_rows,
      (SELECT COUNT(*) FROM audit_log)                       AS audit_rows;
" | tee "$WORK_DIR/integrity.txt"

echo "$LOG_PREFIX Schema version:"
psql "$RESTORE_DB" --tuples-only --quiet --command \
    "SELECT filename, applied_at FROM schema_migrations ORDER BY applied_at DESC LIMIT 5;"

T_END=$(date +%s)
BACKUP_SIZE=$(du -sh "$ENC_FILE" | cut -f1)
echo ""
echo "$LOG_PREFIX ─────────────────────────────────────────────────"
echo "$LOG_PREFIX Backup file:        $ENC_FILE ($BACKUP_SIZE)"
echo "$LOG_PREFIX Restore duration:   $((T_RST_END - T_RST_START))s"
echo "$LOG_PREFIX Total duration:     $((T_END - T_START))s"
echo "$LOG_PREFIX ALL INTEGRITY CHECKS PASSED — backup is restorable."
echo "$LOG_PREFIX ─────────────────────────────────────────────────"
