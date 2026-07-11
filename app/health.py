from __future__ import annotations

import time
import datetime
from flask import Blueprint, jsonify
from app.db import db
from app.extensions import redis_ext, scheduler, limiter, csrf

START_TIME = time.time()

health_bp = Blueprint("health", __name__)
csrf.exempt(health_bp)
limiter.exempt(health_bp)

@health_bp.route("/health/live")
def live():
    """Kubernetes liveness probe. Always returns 200 OK."""
    return jsonify({"status": "ok"}), 200

@health_bp.route("/health/ready")
def ready():
    """Kubernetes readiness probe. Returns 200 if DB and Redis are connected, 503 otherwise."""
    db_ok = False
    redis_ok = False
    
    try:
        db.query_one("SELECT 1")
        db_ok = True
    except Exception:
        pass

    try:
        redis_ext.client.ping()
        redis_ok = True
    except Exception:
        pass

    if db_ok and redis_ok:
        return jsonify({"status": "ok"}), 200
    else:
        return jsonify({
            "status": "unready",
            "database": "ok" if db_ok else "down",
            "redis": "ok" if redis_ok else "down"
        }), 503

@health_bp.route("/api/v1/health")
def detailed_health():
    """Detailed health check for metrics and administration dashboard."""
    overall_status = "ok"
    checks = {}

    # 1. Database health and latency
    try:
        start_t = time.monotonic()
        db.query_one("SELECT 1")
        latency = int((time.monotonic() - start_t) * 1000)
        checks["database"] = {"status": "ok", "latency_ms": latency}
    except Exception as e:
        overall_status = "degraded"
        checks["database"] = {"status": "down", "error": str(e)}

    # 2. Redis health and latency
    try:
        start_t = time.monotonic()
        redis_ext.client.ping()
        latency = int((time.monotonic() - start_t) * 1000)
        checks["redis"] = {"status": "ok", "latency_ms": latency}
    except Exception as e:
        overall_status = "degraded"
        checks["redis"] = {"status": "down", "error": str(e)}

    # 3. Scheduler status and job count
    try:
        jobs = scheduler.get_jobs()
        checks["scheduler"] = {"status": "ok", "jobs_pending": len(jobs)}
    except Exception as e:
        overall_status = "degraded"
        checks["scheduler"] = {"status": "down", "error": str(e)}

    # 4. SIP Providers configuration status
    sip_providers = {}
    try:
        providers = db.query("SELECT name, status FROM sip_providers")
        for p in providers:
            provider_name = p["name"].lower().replace(" ", "_")
            if p["status"] == "active":
                sip_providers[provider_name] = {"status": "ok"}
            else:
                sip_providers[provider_name] = {
                    "status": "degraded",
                    "error": "Provider marked inactive"
                }
                overall_status = "degraded"
    except Exception as e:
        overall_status = "degraded"
        sip_providers["error"] = str(e)
    checks["sip_providers"] = sip_providers

    # If critical checks fail, overall status becomes down
    if checks.get("database", {}).get("status") == "down" or checks.get("redis", {}).get("status") == "down":
        overall_status = "down"

    uptime_seconds = int(time.time() - START_TIME)

    return jsonify({
        "status": overall_status,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": checks,
        "version": "1.0.0",
        "uptime_seconds": uptime_seconds
    }), 200 if overall_status in ("ok", "degraded") else 503
