from __future__ import annotations

import os
import time
import datetime
import hmac
import hashlib
from flask import Blueprint, jsonify, request
from app.db import db
from app.extensions import redis_ext, scheduler, limiter, csrf

START_TIME = time.time()
_BUILD_VERSION = os.environ.get("BUILD_VERSION", "1.0.0")

health_bp = Blueprint("health", __name__)
csrf.exempt(health_bp)
limiter.exempt(health_bp)

_METRICS_TOKEN = os.environ.get("METRICS_AUTH_TOKEN", "")


def _check_metrics_auth() -> bool:
    """Require Bearer token for /health/details and /metrics in production.
    Token is set via METRICS_AUTH_TOKEN env var. If unset, internal callers
    (same-host Prometheus) may pass; enforce at Nginx layer instead."""
    if not _METRICS_TOKEN:
        return True  # Nginx must restrict /metrics to scraper IP
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[len("Bearer "):]
    return hmac.compare_digest(token, _METRICS_TOKEN)


@health_bp.route("/health/live")
def live():
    """Liveness probe — process alive, no external dependency."""
    return jsonify({"status": "ok"}), 200


@health_bp.route("/health/ready")
def ready():
    """Readiness probe — DB and Redis reachable."""
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
    return jsonify({
        "status": "unready",
        "database": "ok" if db_ok else "down",
        "redis": "ok" if redis_ok else "down",
    }), 503


@health_bp.route("/health/details")
def detailed_health():
    """Detailed health — requires METRICS_AUTH_TOKEN Bearer header.
    Returns DB pool state, Redis state, scheduler status, SIP providers.
    Never exposes credentials or tenant data.
    """
    if not _check_metrics_auth():
        return jsonify({"error": "Unauthorized"}), 401

    overall_status = "ok"
    checks: dict = {}

    try:
        start_t = time.monotonic()
        db.query_one("SELECT 1")
        latency = int((time.monotonic() - start_t) * 1000)
        checks["database"] = {"status": "ok", "latency_ms": latency}
    except Exception as e:
        overall_status = "degraded"
        checks["database"] = {"status": "down", "error": type(e).__name__}

    try:
        start_t = time.monotonic()
        redis_ext.client.ping()
        latency = int((time.monotonic() - start_t) * 1000)
        checks["redis"] = {"status": "ok", "latency_ms": latency}
    except Exception as e:
        overall_status = "degraded"
        checks["redis"] = {"status": "down", "error": type(e).__name__}

    try:
        jobs = scheduler.get_jobs()
        checks["scheduler"] = {"status": "ok", "jobs_registered": len(jobs)}
    except Exception:
        checks["scheduler"] = {"status": "not_running"}

    try:
        providers = db.query("SELECT name, status FROM sip_providers")
        checks["sip_providers"] = {
            p["name"]: p["status"] for p in providers
        }
    except Exception as e:
        checks["sip_providers"] = {"error": type(e).__name__}

    try:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM call_jobs WHERE job_status IN ('queued','leased')"
        )
        checks["call_queue_depth"] = int(row["n"]) if row else 0
    except Exception:
        checks["call_queue_depth"] = "unavailable"

    if checks.get("database", {}).get("status") == "down" or \
       checks.get("redis", {}).get("status") == "down":
        overall_status = "down"

    return jsonify({
        "status": overall_status,
        "version": _BUILD_VERSION,
        "uptime_seconds": int(time.time() - START_TIME),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "checks": checks,
    }), 200 if overall_status in ("ok", "degraded") else 503


# Backward compat — old /api/v1/health alias (now protected)
@health_bp.route("/api/v1/health")
def api_health():
    return detailed_health()
