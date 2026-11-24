# C-Call — Deployment Readiness Report (Docker / Coolify / CyberPanel)

- **Compose & Dockerfile:** [docker-compose.production.yml](file:///c:/xampp_lite_8_3/www/ccall/docker-compose.production.yml), [Dockerfile](file:///c:/xampp_lite_8_3/www/ccall/Dockerfile)
- **Guides:** [PRODUCTION_DEPLOYMENT_GUIDE.md](file:///c:/xampp_lite_8_3/www/ccall/docs/PRODUCTION_DEPLOYMENT_GUIDE.md), [CYBERPANEL_DEPLOYMENT.md](file:///c:/xampp_lite_8_3/www/ccall/docs/CYBERPANEL_DEPLOYMENT.md)

| Check | Status | Notes |
|---|---|---|
| Non-root container (`ccall` UID 1001), read-only rootfs, `cap_drop: ALL` | **PASS** | Verified in `Dockerfile` & `docker-compose.production.yml` |
| Private DB/Redis networking (`internal: true`) + app `egress` | **FIXED / PASS** | `postgres`/`redis` isolated on `internal`; `web`/`scheduler`/`call_worker` on `internal` + `egress` |
| Cloud SIP/RTP NAT env vars (`SIP_ADVERTISED_IP`, `RTP_ADVERTISED_IP`, `RTP_PORT_MIN`/`MAX`, `SIP_TRANSPORT`) | **FIXED / PASS** | Wired in `docker-compose.production.yml` for `call_worker` |
| Coolify & CyberPanel (OpenLiteSpeed/Nginx + systemd) compatibility | **PASS** | Region-agnostic (Singapore, USA, Bangladesh); no hardcoded geographic dependencies |
| Production WSGI (`gunicorn.conf.py`), healthchecks (`/health/live`, `/health/ready`) | **PASS** | Verified in compose & `app/health.py` |
| Live Docker image build & container startup | **BLOCKED** | Docker CLI/daemon is not installed on this Windows QA host |
