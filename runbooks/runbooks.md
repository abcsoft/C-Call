# C-Call IVR — Operational Runbooks

All commands assume Docker Compose production deployment.
Run as the `ccall` service user unless stated otherwise.

---

## RB-01: Deploy Release

**Symptoms:** Scheduled release or hotfix ready.

```bash
# 1. Verify commit hash matches expected release tag
git fetch origin && git log origin/main --oneline -3

# 2. Backup BEFORE any change
BACKUP_ENCRYPTION_PASSPHRASE="$BACKUP_ENCRYPTION_PASSPHRASE" \
BACKUP_DIR=/var/backups/ccall \
bash scripts/backup.sh

# 3. Pull verified image
docker pull ghcr.io/ccall/app:${VERSION}

# 4. Run migrations (one-off, not web service)
docker compose -f docker-compose.production.yml run --rm migrate

# 5. Restart services in order
docker compose -f docker-compose.production.yml up -d scheduler
sleep 10
docker compose -f docker-compose.production.yml up -d call_worker
docker compose -f docker-compose.production.yml up -d web

# 6. Verify health
curl -f https://app.ccall.com.bd/health/ready

# 7. Run smoke tests
SMOKE_BASE_URL=https://app.ccall.com.bd bash scripts/smoke_test.sh

# Rollback: see RB-02
```

**Verification:** /health/ready returns 200. Smoke tests all PASS.
**Data safety:** Backup verified before migration runs.

---

## RB-02: Rollback Release

**Symptoms:** New deployment broken; metrics showing errors.

```bash
# 1. Switch web and workers to previous image tag
export VERSION=<previous-tag>
docker compose -f docker-compose.production.yml up -d web call_worker scheduler

# 2. If migration rollback needed (expand-first rule applies):
#    - For additive-only migrations: code rollback alone is sufficient
#    - For schema contraction: restore DB from pre-deploy backup (RB-16)

# 3. Verify
curl -f https://app.ccall.com.bd/health/ready
bash scripts/smoke_test.sh
```

**Escalation:** If rollback does not resolve within 15 min → restore DB from backup (RB-16).

---

## RB-03: Restart Web Service

```bash
docker compose -f docker-compose.production.yml restart web
# Verify
curl -f https://app.ccall.com.bd/health/live
```

**Impact:** ~5s downtime during restart. Nginx buffers in-flight requests.

---

## RB-04: Restart Call Worker

```bash
docker compose -f docker-compose.production.yml restart call_worker
# In-flight calls continue — call_runner threads are daemon, survive worker restart
# New jobs leased after restart
docker compose -f docker-compose.production.yml logs --tail=20 call_worker
```

---

## RB-05: Restart Scheduler

```bash
docker compose -f docker-compose.production.yml restart scheduler
# Advisory locks released on restart — next tick picks up immediately
```

---

## RB-06: Recover Stuck Call

**Symptoms:** Order stuck in CALLING status > 5 minutes; no outcome recorded.

```sql
-- Find it
SELECT id, external_order_id, order_status, updated_at FROM orders
WHERE order_status = 'CALLING' AND updated_at < now() - interval '5 minutes';

-- Check call_jobs
SELECT id, job_status, leased_by, lease_expires_at FROM call_jobs
WHERE order_id = <ORDER_ID>;

-- Force-release lease (worker will retry or mark failed)
UPDATE call_jobs SET job_status='queued', leased_by=NULL, lease_expires_at=NULL
WHERE order_id = <ORDER_ID> AND job_status='leased';
```

**Verification:** Order status transitions within 2 minutes of lease release.
**Data safety:** Do not manually update wallet or call records.

---

## RB-07: Cancel Duplicate Call

**Symptoms:** Monitoring alert — duplicate call processing detected.

```sql
-- Find duplicate leases (should not happen with SKIP LOCKED)
SELECT order_id, COUNT(*) AS workers, array_agg(leased_by) AS workers_list
FROM call_jobs WHERE job_status='leased'
GROUP BY order_id HAVING COUNT(*) > 1;

-- If found: cancel all but one
UPDATE call_jobs SET job_status='cancelled' WHERE id = <DUPLICATE_JOB_ID>;
```

**Post-incident:** Review advisory lock effectiveness. File incident report.

---

## RB-08: Replay Webhook Safely

**Symptoms:** Merchant reports missing webhook for confirmed order.

```sql
-- Find failed webhook
SELECT id, order_id, url, response_code, attempt_count
FROM webhook_logs WHERE order_id = <ORDER_ID> ORDER BY created_at DESC LIMIT 5;
```

```bash
# Trigger retry via scheduler (runs every 5 min automatically)
# For immediate manual retry:
docker compose -f docker-compose.production.yml exec scheduler \
    python -c "
from app import create_app
app = create_app('production')
with app.app_context():
    from app.services.webhook_service import retry_failed_webhooks
    retry_failed_webhooks()
"
```

**Data safety:** Webhook payload is idempotent (contains order final status). Safe to replay.

---

## RB-09: Resolve Wallet Mismatch

**Symptoms:** Tenant reports balance discrepancy; ledger_check alert fires.

```sql
-- Compute expected balance from ledger
SELECT
  w.balance AS current_balance,
  SUM(CASE WHEN wt.type='credit' THEN wt.amount ELSE -wt.amount END) AS ledger_sum
FROM wallets w
JOIN wallet_transactions wt ON wt.tenant_id = w.tenant_id
WHERE w.tenant_id = <TENANT_ID>
GROUP BY w.balance;
```

**Resolution:** Never manually UPDATE wallets.balance. File a corrective wallet_transaction with type='credit'/'debit' and description='Ledger correction YYYY-MM-DD'. Notify tenant.

---

## RB-10: Rotate API Key

```bash
# Tenant self-service: Dashboard → API Keys → Regenerate
# Admin force-rotate:
docker compose -f docker-compose.production.yml exec web \
    python -c "
from app import create_app
app = create_app('production')
with app.app_context():
    from app.services.auth_service import generate_api_key
    from app.db import db
    db.execute('UPDATE api_keys SET is_active=false WHERE tenant_id=%s', (<TENANT_ID>,))
    key = generate_api_key(<TENANT_ID>, name='rotated')
    print('New key (show to tenant once, then discard):', key)
"
```

---

## RB-11: Rotate SIP Credentials

1. Update credentials in SIP provider portal.
2. Update `.env` `SIP_PASSWORD` and `SIP_DOMAIN` on production server.
3. Update encrypted values in `sip_providers` table via admin panel.
4. Restart call_worker: `docker compose restart call_worker`.
5. Place approved test call to `01615717176` to verify.
6. Revoke old credentials in SIP provider portal.

---

## RB-13: Redis Outage

**Symptoms:** /health/ready returns 503. Sessions fail. Rate limiter down.

**Immediate containment:**
- Sessions fail — users see login page. No data loss.
- Rate limiter in fail-open mode (check RATELIMIT_STORAGE_URI config).
- Calls in progress continue (not Redis-dependent).

```bash
# Restart Redis
docker compose -f docker-compose.production.yml restart redis

# Verify
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" ping

# Sessions reconnect automatically after restart
```

**If Redis data lost:** Sessions are ephemeral — users re-login. No business data loss.

---

## RB-14: PostgreSQL Outage

**Symptoms:** /health/ready 503. All writes fail. Calls cannot complete billing.

**Immediate containment:**
```bash
docker compose -f docker-compose.production.yml restart postgres
# Check disk space first: df -h /var/lib/docker/volumes/
# Check PostgreSQL logs
docker compose logs postgres --tail=50
```

**If data corruption suspected:** Stop all services immediately. Restore from backup. Do NOT run auto-vacuum or auto-analyze during active recovery.

---

## RB-15: Backup Failure

```bash
# Check backup log
cat /var/log/ccall/backup.log | tail -30

# Run manual backup
BACKUP_ENCRYPTION_PASSPHRASE="$BACKUP_ENCRYPTION_PASSPHRASE" bash scripts/backup.sh

# Verify backup file
ls -lh /var/backups/ccall/
bash scripts/restore_verify.sh /var/backups/ccall/ccall_<TIMESTAMP>.dump.gpg
```

**Escalation:** If 3 consecutive backups fail → page on-call engineer.

---

## RB-16: Restore Database

**CRITICAL — Never restore over production without explicit authorization.**

```bash
# 1. Stop all application services (keep postgres running)
docker compose -f docker-compose.production.yml stop web scheduler call_worker

# 2. Create point-in-time snapshot of current DB before restore
BACKUP_ENCRYPTION_PASSPHRASE="$BACKUP_ENCRYPTION_PASSPHRASE" bash scripts/backup.sh

# 3. Restore to isolated DB first to verify (REQUIRED)
RESTORE_DB_URL="postgresql://ccall_app:$DB_PASSWORD@localhost:5432/ccall_restore_verify" \
BACKUP_ENCRYPTION_PASSPHRASE="$BACKUP_ENCRYPTION_PASSPHRASE" \
bash scripts/restore_verify.sh /var/backups/ccall/ccall_<TIMESTAMP>.dump.gpg

# 4. After human review and authorization:
docker compose exec postgres psql -U ccall_app -c "DROP DATABASE ccall_prod;" postgres
docker compose exec postgres psql -U ccall_app -c "CREATE DATABASE ccall_prod;" postgres
gpg --batch --passphrase "$BACKUP_ENCRYPTION_PASSPHRASE" --decrypt ccall_<TIMESTAMP>.dump.gpg | \
    pg_restore --dbname="$DATABASE_URL" --no-owner --no-acl

# 5. Restart services
docker compose -f docker-compose.production.yml up -d

# 6. Verify
curl -f https://app.ccall.com.bd/health/ready
bash scripts/smoke_test.sh
```

---

## RB-17: Disk Full

```bash
# Find large files
du -sh /var/lib/docker/volumes/ccall*/* | sort -rh | head -20
du -sh /var/backups/ccall/* | sort -rh | head -10

# Rotate old backups (keep last 7 days emergency)
find /var/backups/ccall -name "*.gpg" -mtime +7 -delete

# Clear old Docker images
docker image prune -f
docker system prune -f --volumes  # CAUTION: removes stopped containers
```

---

## RB-23: Security Incident

**Symptoms:** Unauthorized access suspected; API key compromise; data breach.

**Immediate:**
1. Rotate ALL secrets: `FLASK_SECRET_KEY`, `REDIS_PASSWORD`, `DB_PASSWORD`, all API keys.
2. Invalidate all sessions: `docker compose exec redis redis-cli -a "$REDIS_PASSWORD" FLUSHDB`.
3. Disable affected tenant API keys: `UPDATE api_keys SET is_active=false WHERE tenant_id=<ID>`.
4. Pull server access logs: `/var/log/nginx/ccall_access.log`.
5. Preserve evidence before any cleanup.
6. Contact affected tenants.

---

## RB-25: Emergency Maintenance Mode

```bash
# Return 503 for all traffic via Nginx (edit ccall.conf):
# In server{} block add:
#   return 503 "Scheduled maintenance — back shortly.";

# Restart Nginx
nginx -s reload

# Restore: remove the return 503 line, reload
nginx -s reload
```
