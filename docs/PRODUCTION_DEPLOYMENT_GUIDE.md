# C-Call V2 — Production Deployment & Operations Guide

## 1. Architecture Overview

C-Call V2 runs as a multi-service Docker Compose stack:

- **web**: Gunicorn (`gunicorn.conf.py`, `run:app`, non-root user `ccall` UID 1001, read-only rootfs) serving the customer portal, admin console, and `/api/v1/*` REST API.
- **scheduler**: Singleton APScheduler + call-completion monitor (`python -m app.commands.run_scheduler`, `replicas: 1`, protected by PostgreSQL advisory lock).
- **call_worker**: Scalable durable queue workers (`python -m app.commands.run_call_worker`, `FOR UPDATE SKIP LOCKED`, configurable `CALL_MAX_PROVIDER_CHANNELS`).
- **postgres**: PostgreSQL 16 (`internal` network only, not exposed to host).
- **redis**: Redis 7 (`internal` network only, password-protected, AOF persistence).
- **migrate**: One-off schema migration runner (`python migrations/run_migrations.py`).

---

## 2. Coolify Deployment Instructions

1. **Create a Docker Compose resource in Coolify** pointing to `docker-compose.production.yml`.
2. **Configure required environment variables** (never commit `.env`):
   - `FLASK_ENV=production`
   - `FLASK_SECRET_KEY` (64+ hex chars: `python -c "import secrets; print(secrets.token_hex(32))"`)
   - `SIP_PASSWORD_ENCRYPTION_KEY` (Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
   - `DATABASE_URL=postgresql://ccall_app:CHANGE_ME_DB_PASSWORD@postgres:5432/ccall_prod`
   - `DB_PASSWORD`, `REDIS_PASSWORD`, `REDIS_URL=redis://:CHANGE_ME_REDIS_PASSWORD@redis:6379/0`
   - `ALLOW_PRIVATE_WEBHOOKS_FOR_DEV=false`
   - `ENABLE_SCHEDULER=false`, `ENABLE_CALL_WORKER=false`, `ENABLE_CALL_MONITOR=false` (overridden per service in compose).
3. **Reverse Proxy / TLS**:
   - Point Coolify's Traefik proxy (or host Nginx using `nginx/nginx.conf`) at `web:5000` with HTTPS enforced.
   - Healthcheck endpoints: `/health/live` (liveness) and `/health/ready` (DB + Redis + queue readiness).
4. **Run database migrations before starting app services**:
   ```bash
   docker compose -f docker-compose.production.yml run --rm migrate
   ```
5. **Start services in order**:
   ```bash
   docker compose -f docker-compose.production.yml up -d postgres redis
   docker compose -f docker-compose.production.yml run --rm migrate
   docker compose -f docker-compose.production.yml up -d scheduler call_worker web
   ```

---

## 3. SIP & RTP Networking Instructions

- **SIP Signaling**: Outbound UDP/5060 (or configured `SIP_PORT`) from `call_worker` to your SIP trunk provider.
- **RTP Media**: Bidirectional UDP media sockets bound dynamically by `call_engine/ivr_engine.py`.
- **NAT / Firewall**:
  - Allow outbound UDP `5060` and UDP `10000-20000` (RTP) from the host to your SIP trunk IPs.
  - For servers behind NAT or using symmetric RTP, `call_worker` can use `network_mode: host` (or expose the UDP RTP port range) so the SDP `c=`/`m=` media IP/port matches the host's reachable interface.
  - `nat64_to_ipv4()` in `call_engine/ivr_engine.py` automatically decodes NAT64 `64:ff9b::/96` media addresses to IPv4.

---

## 4. Backups, Restoration & Rollback

### Encrypted Backups
Run via the `backup` profile or `scripts/backup.sh`:
```bash
BACKUP_ENCRYPTION_PASSPHRASE="<passphrase>" BACKUP_DIR=/var/backups/ccall bash scripts/backup.sh
```
### Restoration Verification
Test backups regularly on a non-production database using `scripts/restore_verify.sh`.
### Rollback Procedure
1. Pin `VERSION=<previous-tag>` and redeploy `web`, `call_worker`, and `scheduler`:
   ```bash
   docker compose -f docker-compose.production.yml up -d web call_worker scheduler
   ```
2. Because all migrations (`001`–`028`) are additive/expand-first, code rollback works without schema contraction. If a full DB restore is required, stop `web`, `call_worker`, `scheduler`, restore from the latest encrypted `pg_dump`, and restart services.
3. Verify `/health/ready` returns `200` and run `scripts/smoke_test.sh`.
