from __future__ import annotations

from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from flask import Blueprint, Response
from app.extensions import limiter, csrf
from app.db import db

# Metrics definitions per requirements
CALLS_TOTAL = Counter('ccall_calls_total', 'Total calls by result', ['result', 'tenant_id'])
CALL_DURATION = Histogram('ccall_call_duration_seconds', 'Call duration', buckets=[5, 10, 30, 60, 120, 300])
TTS_DURATION = Histogram('ccall_tts_generation_seconds', 'TTS generation time', ['provider'])
ACTIVE_CALLS = Gauge('ccall_active_calls', 'Currently active calls')
WALLET_BALANCE = Gauge('ccall_wallet_balance_bdt', 'Tenant wallet balance', ['tenant_id'])
API_REQUESTS = Counter('ccall_api_requests_total', 'API requests', ['method', 'endpoint', 'status'])
RETRY_QUEUE_SIZE = Gauge('ccall_retry_queue_size', 'Orders pending retry')

def update_gauge_metrics() -> None:
    """Updates Gauge metrics from database before exposing metrics."""
    # 1. Update wallet balances per tenant
    try:
        wallets = db.query("SELECT tenant_id, balance FROM wallets")
        for w in wallets:
            if w.get("tenant_id") is not None and w.get("balance") is not None:
                WALLET_BALANCE.labels(tenant_id=str(w["tenant_id"])).set(float(w["balance"]))
    except Exception:
        pass

    # 2. Update orders pending retry queue size
    try:
        row = db.query_one("SELECT COUNT(*) AS cnt FROM orders WHERE order_status = 'RETRY_SCHEDULED'")
        if row:
            RETRY_QUEUE_SIZE.set(row["cnt"])
    except Exception:
        pass

metrics_bp = Blueprint("metrics", __name__)
csrf.exempt(metrics_bp)
limiter.exempt(metrics_bp)

@metrics_bp.route("/metrics")
def metrics():
    """Prometheus scrape endpoint."""
    update_gauge_metrics()
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
