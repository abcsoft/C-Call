# C-Call — Deployment Readiness Report

- **Compose & Dockerfile:** [docker-compose.production.yml](file:///c:/xampp_lite_8_3/www/ccall/docker-compose.production.yml), [Dockerfile](file:///c:/xampp_lite_8_3/www/ccall/Dockerfile)
- **Guide:** [PRODUCTION_DEPLOYMENT_GUIDE.md](file:///c:/xampp_lite_8_3/www/ccall/docs/PRODUCTION_DEPLOYMENT_GUIDE.md)

| Check | Status | Notes |
|---|---|---|
| Non-root container (`ccall` UID 1001), read-only rootfs, `cap_drop: ALL` | **PASS** | Verified in `Dockerfile` & `docker-compose.production.yml` |
| Private DB/Redis networking (`internal: true`) + app `egress` | **FIXED / PASS** | `postgres`/`redis` isolated on `internal`; `web`/`scheduler`/`call_worker` on `internal` + `egress` |
| Production WSGI (`gunicorn.conf.py`), healthchecks (`/health/live`, `/health/ready`) | **PASS** | Verified in compose & `app/health.py` |
| Live Docker image build & container startup | **BLOCKED** | Docker CLI/daemon is not installed on this Windows QA host |
