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

---

## 5. Multi-Region Support (Singapore, USA, Bangladesh)

C-Call V2 is region-agnostic with no hardcoded ISP, carrier, or data-center dependencies:
- **Singapore / USA / Bangladesh VPS or EC2**: Set `SIP_ADVERTISED_IP` and `RTP_ADVERTISED_IP` to the host's public IPv4 / Elastic IP, set `SIP_DOMAIN` and `SIP_PORT` in `.env` or Admin → Providers, and select the carrier-compliant `ASTERISK_DIAL_FORMAT` (`local_bd`, `e164`, `plus_e164`, or `raw`).
- **UDP-Only SIP Transport**: Only `SIP_TRANSPORT=udp` is supported; non-UDP values fail fast at startup.

---

## 6. Alternative Deployment Modes

### Standalone Ubuntu (22.04 / 24.04 LTS)
1. Provision host hardening, Docker, Nginx, Certbot, UFW, Fail2ban, and `ccall` user:
   `sudo bash scripts/provision_production_server.sh`
2. Configure `.env.production` (`chmod 600`) and validate secrets:
   `python3 scripts/validate_production_secrets.py --env .env.production`
3. Run `sudo bash scripts/deploy_production.sh` (ensures UDP `10000:20000` is permitted for RTP media if behind strict firewall).

### Existing Windows Local Installation
Use [run.ps1](file:///c:/xampp_lite_8_3/www/ccall/run.ps1) / [run.bat](file:///c:/xampp_lite_8_3/www/ccall/run.bat) with the `.venv` Python environment, PostgreSQL, Redis, and `FFMPEG_PATH`, and [backup.ps1](file:///c:/xampp_lite_8_3/www/ccall/backup.ps1) for scheduled DB dumps. Multi-worker production WSGI (`gunicorn.conf.py`) requires Linux/Docker.

---

## 7. Minimum Resource Requirements & Troubleshooting

| Tier | vCPU | RAM | Disk | Concurrent Calls |
|---|---|---|---|---|
| Staging / Small | 2 vCPU | 4 GB | 20 GB SSD | Up to 10 |
| Production | 4 vCPU | 8 GB | 50+ GB SSD | 25–50 |

**Common Troubleshooting:**
- **Calls ring but no audio / 200 OK timeout:** Confirm `SIP_ADVERTISED_IP` and `RTP_ADVERTISED_IP` equal the public Elastic IP and UDP `RTP_PORT_MIN..MAX` (`10000-20000`) is open.
- **`/health/ready` returns 503:** Verify `postgres` and `redis` containers are healthy on `internal` network (`docker compose ps`).
- **Workers exit on startup:** Validate `SECRET_KEY`, `SIP_PASSWORD_ENCRYPTION_KEY`, and `DATABASE_URL` with `python scripts/validate_production_secrets.py --env .env.production`.
