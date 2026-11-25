# C-Call V2 — Operator Deployment & Firewall Checklist (AWS Singapore, Coolify & CyberPanel)

## 1. Strict Firewall & SIP/RTP Port Policy (All Environments)

Never expose database, cache, Prometheus, Gunicorn, or wide-open `0.0.0.0/0` SIP/RTP ports publicly.

| Port / Protocol | Intended Service | Public Exposure? | Allowed Source CIDR |
|---|---|---|---|
| `80/tcp`, `443/tcp` | Nginx / OpenLiteSpeed / Traefik HTTPS | Yes (HTTP redirects to HTTPS) | `0.0.0.0/0` |
| `22/tcp` | SSH admin | Restricted | Operator Bastion / Admin IP(s) ONLY |
| `5060/udp` (`SIP_PORT`) | `call_worker` SIP signaling | Carrier-only (outbound/symmetric) | **Authorized SIP Carrier IP(s)/CIDR(s) ONLY** |
| `10000:20000/udp` (or narrowed `RTP_PORT_MIN:RTP_PORT_MAX`) | `call_worker` RTP media | Carrier-only | **Authorized SIP Carrier IP(s)/CIDR(s) ONLY** |
| `5000/tcp` | Gunicorn web (`web`) | **NO** | `127.0.0.1` loopback / Coolify internal Traefik network only |
| `5432/tcp` | PostgreSQL 16 | **NO** | Compose `internal` network (`internal: true`) only |
| `6379/tcp` | Redis 7 | **NO** | Compose `internal` network (`internal: true`) only |
| `9090/tcp`, `9093/tcp` | Prometheus / Alertmanager | **NO** | `127.0.0.1` / private VPC CIDR only |

### Docker `DOCKER-USER` Protection (Critical on Ubuntu Hosts Running `ufw`)
Docker inserts container forwarding rules into the `DOCKER` chain *before* standard `ufw` `INPUT` rules. Verify and enforce `DOCKER-USER` so no container bind can bypass host UFW rules:
```bash
# Inspect current DOCKER-USER rules
sudo iptables -L DOCKER-USER -v -n

# Example: block any accidental direct WAN access to internal ports on primary NIC (e.g. eth0 / ens5)
EXT_IF="$(ip route show default | awk '/default/ {print $5; exit}')"
sudo iptables -I DOCKER-USER -i "$EXT_IF" -p tcp -m multiport --dports 5432,6379,5000,9090,9093 -j DROP
# Allow established return traffic & authorized carrier SIP/RTP only
sudo iptables -I DOCKER-USER -i "$EXT_IF" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
```

---

## 2. AWS Singapore (`ap-southeast-1`) Operator Checklist

- [ ] **Region & Host Sizing:** Provision Ubuntu 22.04/24.04 LTS EC2 in `ap-southeast-1` (`Staging`: 2 vCPU / 4 GB RAM / 20+ GB gp3; `Production`: 4 vCPU / 8 GB RAM / 50+ GB gp3) with static **Elastic IP** attached.
- [ ] **EC2 Security Group (Inbound):**
  - `TCP 80`, `TCP 443`: `0.0.0.0/0` (or Cloudflare IPs if proxied; keep webhook routes unblocked for WooCommerce/bKash/SSLCommerz).
  - `TCP 22`: Operator IP `/32` only.
  - `UDP 5060` + `UDP <RTP_PORT_MIN>-<RTP_PORT_MAX>` (e.g. `10000-20000`): **Authorized SIP trunk carrier CIDR(s) ONLY** — never `0.0.0.0/0`.
  - **Do NOT open** `5000`, `5432`, `6379`, `9090`, `9093`.
- [ ] **NAT & Source/Dest Environment Variables (`.env.production`):**
  ```ini
  SIP_TRANSPORT=udp
  SIP_BIND_IP=0.0.0.0
  RTP_BIND_IP=0.0.0.0
  SIP_ADVERTISED_IP=<ELASTIC_PUBLIC_IPV4>
  RTP_ADVERTISED_IP=<ELASTIC_PUBLIC_IPV4>
  RTP_PORT_MIN=10000
  RTP_PORT_MAX=20000
  ASTERISK_DIAL_FORMAT=e164   # or local_bd / plus_e164 per trunk
  MOCK_SIP_PROVIDER=false
  ```
- [ ] **Kernel & UFW Provisioning:** If using fresh host provisioning script, export carrier allowlist before running:
  ```bash
  export SIP_CARRIER_CIDRS="<CARRIER_IP_1>/32,<CARRIER_IP_2>/32"
  sudo -E bash scripts/provision_production_server.sh
  ```
  Confirm `/etc/sysctl.d/99-ccall-hardening.conf` has `net.ipv4.ip_forward = 1` (required for Docker container egress).
- [ ] **Pre-Deploy Secret Audit:** Run `python3 scripts/validate_production_secrets.py --env .env.production` (must exit `0`).
- [ ] **Chronological Time Sync:** Confirm `timedatectl status` shows `UTC` and NTP synchronized (required for HMAC webhook timestamps, session TTLs, and SIP `Date` headers).

---

## 3. Coolify Deployment Checklist (Isolated Staging & Production Co-Existence)

- [ ] **Project Isolation:** Create a separate Coolify Compose resource named `ccall-staging` (do **NOT** reuse database, Redis, domain, or volumes of existing production apps).
- [ ] **Compose Stack Selection:** Configure Compose files in Coolify as:
  `docker-compose.production.yml:docker-compose.staging.coolify.yml`
- [ ] **Verified Overlay Isolation:** [docker-compose.staging.coolify.yml](file:///c:/xampp_lite_8_3/www/ccall/docker-compose.staging.coolify.yml) isolates:
  - DB `ccall_staging` (`ccall_staging_app`) on `staging_postgres_data`
  - Redis on `staging_redis_data`
  - Audio/recordings on `staging_audio_data`
  - Backups on `staging_backup_data`
  - Web exposed internally (`ports: []`, `expose: ["5000"]`) with Traefik host routing on `Host(${STAGING_DOMAIN})` (zero conflict with port `5000` on host).
- [ ] **Required Coolify Environment Variables:** Set `DB_PASSWORD`, `REDIS_PASSWORD`, `STAGING_DATABASE_URL` (`postgresql://ccall_staging_app:CHANGE_ME_DB_PASSWORD@postgres:5432/ccall_staging`), `STAGING_REDIS_URL` (`redis://:CHANGE_ME_REDIS_PASSWORD@redis:6379/0`), `FLASK_SECRET_KEY` (128 hex), `API_KEY_PEPPER` (64 hex), `SIP_PASSWORD_ENCRYPTION_KEY` (Fernet), `BACKUP_ENCRYPTION_PASSPHRASE`, `STAGING_DOMAIN`, `SIP_ADVERTISED_IP`, `RTP_ADVERTISED_IP`, `SIP_TRANSPORT=udp`.
- [ ] **`.env` File Check:** Ensure `.env` exists in Compose working directory (even empty if Coolify passes vars via environment) before `docker compose up` so `env_file: .env` in `x-app-base` succeeds.
- [ ] **Deploy Order & Verification:**
  1. Start `postgres` and `redis` and confirm `healthy`.
  2. Execute one-off `migrate` container (`python migrations/run_migrations.py`, `28/28` migrations).
  3. Start `scheduler` (`replicas=1`, `ENABLE_SCHEDULER=true`) and `call_worker` (`ENABLE_CALL_WORKER=true`, `ENABLE_CALL_MONITOR=true`).
  4. Start `web` and verify `https://${STAGING_DOMAIN}/health/live` and `/health/ready` return HTTP `200`.
- [ ] **SIP/NAT Diagnostics First (Zero Cost):** Test Admin → Providers (`Run SIP Diagnostic`) and verify separate `OPTIONS` + non-disruptive RFC 3261 `REGISTER` badges without initiating calls.

---

## 4. CyberPanel (`OpenLiteSpeed` / `Nginx` + `systemd`) Checklist

- [ ] **Runtime User & Source Checkout:** Create non-root user `ccall`, check out exact tagged commit `4880385` (or latest verified release commit) into `/opt/ccall`, create Python `3.11+` virtualenv `/opt/ccall/.venv`, install `requirements.txt`, install OS `ffmpeg` & `postgresql-client-16`.
- [ ] **Environment Security:** Populate `/opt/ccall/.env` with `chmod 600` (`chown ccall:ccall`), verify `python3 scripts/validate_production_secrets.py --env /opt/ccall/.env` passes, confirm `FLASK_ENV=production`, `HOST=127.0.0.1`, `PORT=5000`, `TRUSTED_PROXIES=127.0.0.1`.
- [ ] **Database & Redis:** Create dedicated PostgreSQL 16 role/database & authenticated Redis 7 instance bound to `127.0.0.1`; run `.venv/bin/python migrations/run_migrations.py` (`001`–`028`).
- [ ] **Systemd Process Separation (`ENABLE_*` guards):**
  - `ccall-web.service`: `ENABLE_SCHEDULER=false ENABLE_CALL_WORKER=false ENABLE_CALL_MONITOR=false` → `.venv/bin/gunicorn --config gunicorn.conf.py run:app` (bound to `127.0.0.1:5000`).
  - `ccall-scheduler.service`: `ENABLE_SCHEDULER=true ENABLE_CALL_WORKER=false ENABLE_CALL_MONITOR=false` → `.venv/bin/python -m app.commands.run_scheduler` (**singleton `1` instance only**).
  - `ccall-worker.service`: `ENABLE_SCHEDULER=false ENABLE_CALL_WORKER=true ENABLE_CALL_MONITOR=true` → `.venv/bin/python -m app.commands.run_call_worker`.
- [ ] **CyberPanel / OpenLiteSpeed Reverse Proxy & Security:**
  - Issue Let's Encrypt SSL for target domain in CyberPanel.
  - Configure External App (`Web Server` → `127.0.0.1:5000`) + Rewrite/Context forwarding `Host`, `X-Forwarded-For`, `X-Forwarded-Proto: https`.
  - Block direct access to `/.env`, `/.git`, `/migrations/`, `/instance/`, `/backups/`, `*.sql`, `*.dump`.
  - Restrict `/metrics` via IP allowlist + `METRICS_AUTH_TOKEN` Bearer header.
- [ ] **CyberPanel / CSF Firewall (`UDP` Carrier-Only):** Allow inbound UDP `5060` and `10000:20000` (or `RTP_PORT_MIN:RTP_PORT_MAX`) **strictly for authorized SIP trunk carrier IP(s)**; block external `5000`, `5432`, `6379`.
- [ ] **Verification:** Check `systemctl status ccall-web ccall-scheduler ccall-worker`, `curl -fsS https://<domain>/health/live`, `curl -fsS https://<domain>/health/ready`, and schedule encrypted cron via `scripts/backup.sh` + periodic isolated verification via `scripts/restore_verify.sh`.
