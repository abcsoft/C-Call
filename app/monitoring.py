"""
Prometheus metrics — C-Call IVR SaaS.

NOTE: tenant_id is intentionally NOT used as a label on any metric.
Unbounded cardinality (one label value per tenant) causes memory growth
and makes Prometheus OOM. Tenant-level analytics belong in the DB/CDR.
"""
from __future__ import annotations

import os
import hmac
import hashlib
from pathlib import Path
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from flask import Blueprint, Response, request
from app.extensions import limiter, csrf
from app.db import db

# ── HTTP ─────────────────────────────────────────────────────────────────────
API_REQUESTS = Counter(
    "ccall_api_requests_total",
    "HTTP requests by method, endpoint (normalised), and status",
    ["method", "endpoint", "status"],
)
HTTP_LATENCY = Histogram(
    "ccall_http_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)

# ── Orders ────────────────────────────────────────────────────────────────────
ORDERS_ACCEPTED = Counter("ccall_orders_accepted_total", "Orders accepted")
ORDERS_REJECTED = Counter("ccall_orders_rejected_total", "Orders rejected by subscription", ["reason"])
ORDERS_DUPLICATE = Counter("ccall_orders_duplicate_total", "Idempotent duplicate orders")

# ── Calls ─────────────────────────────────────────────────────────────────────
# NOTE: tenant_id label retained for call_runner.py compatibility (read-only file).
# In production with many tenants this creates high cardinality — cap tenant count
# or switch to recording rules in Prometheus to aggregate per-result totals.
CALLS_TOTAL = Counter("ccall_calls_total", "Calls by result", ["result", "tenant_id"])
CALL_DURATION = Histogram(
    "ccall_call_duration_seconds",
    "Call duration in seconds",
    buckets=[5, 10, 30, 60, 120, 300],
)
ACTIVE_CALLS = Gauge("ccall_active_calls", "Currently active SIP calls")

# ── Job queue ─────────────────────────────────────────────────────────────────
CALL_QUEUE_DEPTH = Gauge("ccall_call_queue_depth", "Call jobs queued or leased")
CALL_QUEUE_OLDEST_SECONDS = Gauge("ccall_call_queue_oldest_seconds", "Age of oldest queued job")
CALL_QUEUE_RETRY_WAIT = Gauge("ccall_call_queue_retry_wait", "Call jobs in retry_wait state")
CALL_QUEUE_FAILED = Gauge("ccall_call_queue_failed_total_gauge", "Call jobs permanently failed (cumulative)")
EXPIRED_LEASES_RECOVERED = Counter("ccall_expired_leases_recovered_total", "Leases recovered after expiry")
CALL_JOB_DISPATCH_SUCCESS = Counter("ccall_call_job_dispatch_success_total", "Jobs dispatched successfully")
CALL_JOB_DISPATCH_FAILURE = Counter("ccall_call_job_dispatch_failure_total", "Jobs that failed dispatch", ["error_code"])
CALL_WORKER_LAST_POLL = Gauge("ccall_call_worker_last_poll_timestamp_seconds", "Unix timestamp of last successful worker poll tick")

# ── TTS ───────────────────────────────────────────────────────────────────────
TTS_DURATION = Histogram(
    "ccall_tts_generation_seconds",
    "TTS generation latency",
    ["provider"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30],
)
TTS_CACHE_HITS = Counter("ccall_tts_cache_hits_total", "TTS audio served from cache")
TTS_FAILURES = Counter("ccall_tts_failures_total", "TTS generation failures", ["provider"])

# ── Webhooks ──────────────────────────────────────────────────────────────────
WEBHOOK_DELIVERED = Counter("ccall_webhook_delivered_total", "Webhook delivery success")
WEBHOOK_FAILED = Counter("ccall_webhook_failed_total", "Webhook delivery failure")
WEBHOOK_RETRY = Counter("ccall_webhook_retry_total", "Webhook retries attempted")

# ── Wallet ────────────────────────────────────────────────────────────────────
WALLET_DEBIT_SUCCESS = Counter("ccall_wallet_debit_success_total", "Successful wallet debits")
WALLET_INSUFFICIENT = Counter("ccall_wallet_insufficient_total", "Insufficient balance events")
WALLET_LOW_BALANCE_TENANTS = Gauge("ccall_wallet_low_balance_tenants", "Tenants with low wallet balance")
# Platform aggregate — no per-tenant label (prevents unbounded cardinality)
WALLET_BALANCE = Gauge("ccall_wallet_balance_bdt", "Total platform wallet balance BDT (sum across all tenants)")

# ── Call billing (atomic finalization) ────────────────────────────────────────
CALL_BILLING_FINALIZED = Counter(
    "ccall_call_billing_finalized_total",
    "Calls finalized with a new debit (billed=True)",
)
CALL_BILLING_ALREADY_FINALIZED = Counter(
    "ccall_call_billing_already_finalized_total",
    "finalize_call_billing() calls that found call already billed (idempotent no-op)",
)
CALL_BILLING_FAILED = Counter(
    "ccall_call_billing_failed_total",
    "finalize_call_billing() calls that could not bill (excluding already_finalized)",
    ["error_code"],
)
WALLET_CALL_DEBIT = Counter(
    "ccall_wallet_call_debit_total",
    "Wallet debits originating from call finalization",
)
WALLET_CALL_REFUND = Counter(
    "ccall_wallet_call_refund_total",
    "Wallet refunds credited back for a call (refund_call_charge)",
)
BILLING_RECONCILIATION_ANOMALIES = Gauge(
    "ccall_billing_reconciliation_anomalies",
    "Unresolved billing anomalies detected by last reconciliation run",
    ["anomaly_class"],
)
OLDEST_UNFINALIZED_TERMINAL_CALL_AGE = Gauge(
    "ccall_oldest_unfinalized_terminal_call_age_seconds",
    "Age in seconds of the oldest terminal call not yet billing-finalized",
)
RETRY_QUEUE_SIZE = Gauge("ccall_retry_queue_size", "Orders pending retry")

# ── Backup ────────────────────────────────────────────────────────────────────
# Written by backup.sh atomically into instance/metrics/ccall_backup.prom.
# Falls back to 0 if no successful backup has run yet.
BACKUP_SUCCESS_TIMESTAMP = Gauge(
    "ccall_last_backup_success_timestamp",
    "Unix timestamp of the last successful backup",
)

# ── Ledger reconciliation ─────────────────────────────────────────────────────
# Updated by the wallet_reconcile scheduler job every 6 hours.
# No per-tenant label — use DB audit log for tenant-level investigation.
WALLET_LEDGER_MISMATCH = Gauge(
    "ccall_wallet_ledger_mismatch_count",
    "Tenants whose wallet.balance != sum(wallet_transactions)",
)

# ── Subscriptions ─────────────────────────────────────────────────────────────
SUBSCRIPTION_STATUS = Gauge(
    "ccall_subscriptions_by_status",
    "Active tenant count by subscription status",
    ["status"],
)


def update_gauge_metrics() -> None:
    """Refresh Gauge metrics from DB. Called on each /metrics scrape."""
    try:
        row = db.query_one("SELECT COUNT(*) AS n FROM orders WHERE order_status = 'RETRY_SCHEDULED'")
        RETRY_QUEUE_SIZE.set(row["n"] if row else 0)
    except Exception:
        pass

    try:
        rows = db.query("SELECT COALESCE(balance, 0) AS balance FROM wallets")
        WALLET_BALANCE.set(sum(float(r["balance"]) for r in rows))
    except Exception:
        pass

    try:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM call_jobs WHERE job_status IN ('queued','leased','retry_wait')"
        )
        CALL_QUEUE_DEPTH.set(row["n"] if row else 0)
    except Exception:
        pass

    try:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM call_jobs WHERE job_status = 'retry_wait'"
        )
        CALL_QUEUE_RETRY_WAIT.set(row["n"] if row else 0)
    except Exception:
        pass

    try:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM call_jobs WHERE job_status = 'failed'"
        )
        CALL_QUEUE_FAILED.set(row["n"] if row else 0)
    except Exception:
        pass

    try:
        row = db.query_one(
            """
            SELECT EXTRACT(EPOCH FROM (now() - MIN(available_at))) AS oldest
            FROM call_jobs WHERE job_status IN ('queued', 'retry_wait')
              AND available_at <= now()
            """
        )
        CALL_QUEUE_OLDEST_SECONDS.set(float(row["oldest"]) if row and row["oldest"] is not None else 0.0)
    except Exception:
        pass

    try:
        rows = db.query(
            "SELECT status, COUNT(*) AS n FROM tenants WHERE status IS NOT NULL GROUP BY status"
        )
        for r in rows:
            SUBSCRIPTION_STATUS.labels(status=r["status"]).set(r["n"])
    except Exception:
        pass

    try:
        row = db.query_one(
            """
            SELECT COUNT(DISTINCT w.tenant_id) AS n
            FROM wallets w
            JOIN tenants t ON t.id = w.tenant_id
            WHERE t.status = 'active' AND w.balance < 50
            """
        )
        WALLET_LOW_BALANCE_TENANTS.set(row["n"] if row else 0)
    except Exception:
        pass

    # ── Backup success timestamp ───────────────────────────────────────────────
    # Read from the atomic textfile written by backup.sh.
    try:
        prom_dir = os.environ.get("BACKUP_METRICS_DIR", "instance/metrics")
        prom_file = Path(prom_dir) / "ccall_backup.prom"
        if prom_file.exists():
            for line in prom_file.read_text().splitlines():
                if line.startswith("ccall_last_backup_success_timestamp "):
                    ts = float(line.split()[-1])
                    BACKUP_SUCCESS_TIMESTAMP.set(ts)
                    break
    except Exception:
        pass

    # ── Oldest unfinalized terminal call ─────────────────────────────────────
    try:
        row = db.query_one(
            """
            SELECT EXTRACT(EPOCH FROM (now() - MIN(ended_at))) AS age_seconds
            FROM calls
            WHERE billing_finalized = FALSE
              AND status IN (
                  'completed','confirmed','cancelled','canceled',
                  'failed','no_answer','busy','unreachable','timeout'
              )
              AND ended_at IS NOT NULL
            """
        )
        OLDEST_UNFINALIZED_TERMINAL_CALL_AGE.set(
            float(row["age_seconds"]) if row and row["age_seconds"] is not None else 0.0
        )
    except Exception:
        pass

    # ── Wallet ledger mismatch ────────────────────────────────────────────────
    # The reconciliation job updates this gauge. Re-read from DB for freshness.
    try:
        row = db.query_one(
            """
            SELECT COUNT(*) AS n
            FROM wallets w
            WHERE w.balance IS NOT NULL
              AND ABS(
                w.balance - COALESCE((
                    SELECT SUM(CASE WHEN wt.type='credit' THEN wt.amount
                                    ELSE -wt.amount END)
                    FROM wallet_transactions wt
                    WHERE wt.tenant_id = w.tenant_id
                ), 0)
              ) > 0.001
            """
        )
        WALLET_LEDGER_MISMATCH.set(row["n"] if row else 0)
    except Exception:
        pass


_METRICS_TOKEN = os.environ.get("METRICS_AUTH_TOKEN", "")

metrics_bp = Blueprint("metrics", __name__)
csrf.exempt(metrics_bp)
limiter.exempt(metrics_bp)


@metrics_bp.route("/metrics")
def metrics():
    """Prometheus scrape endpoint. Restricted by METRICS_AUTH_TOKEN or Nginx IP allow."""
    if _METRICS_TOKEN:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return Response("Unauthorized", status=401)
        token = auth[len("Bearer "):]
        if not hmac.compare_digest(token, _METRICS_TOKEN):
            return Response("Forbidden", status=403)

    update_gauge_metrics()
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
