# E:\c-call-ivr\app\services\revenue_service.py
"""Platform revenue reporting — gross revenue vs. estimated SIP cost, per
tenant and daily breakdown, for the admin Revenue page.

Cost estimation note: there's no real per-minute SIP trunk cost stored
anywhere in the schema (sip_providers has no rate field — trunk billing is
typically a separate carrier invoice, not per-call metered in this DB).
SIP_COST_PER_MINUTE below is a configurable estimate used purely for the
"estimated margin" report; it is NOT used for tenant billing (rate_service
handles that, from customer_rates / plan defaults).
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from app.db import db

SIP_COST_PER_MINUTE = Decimal(os.environ.get("SIP_COST_PER_MINUTE_ESTIMATE", "0.30"))

CALL_STATUS_TO_ORDER_STATUS = (
    "CONFIRMED", "CANCELLED", "NO_ANSWER", "UNREACHABLE", "FAILED",
)


def get_revenue_report(date_from, date_to) -> dict[str, Any]:
    """Revenue/cost/profit report for [date_from, date_to] (inclusive dates).

    Returns:
        {
          total_revenue: Decimal, total_calls: int, total_minutes: int,
          total_cost: Decimal, total_profit: Decimal, margin_pct: float,
          per_tenant: [{tenant_id, name, plan, calls, minutes, revenue, cost, profit}],
          daily_breakdown: [{date, revenue, calls}],
          status_distribution: {CONFIRMED: n, CANCELLED: n, ...},
        }
    """
    totals_row = db.query_one(
        """
        SELECT
            COALESCE(SUM(wt.amount), 0) AS revenue,
            COUNT(DISTINCT c.id) AS calls,
            COALESCE(SUM(c.talk_seconds), 0) AS talk_seconds
        FROM wallet_transactions wt
        LEFT JOIN calls c ON c.tenant_id = wt.tenant_id
            AND c.started_at >= %s AND c.started_at < (%s::date + interval '1 day')
        WHERE wt.type = 'debit' AND wt.created_at >= %s AND wt.created_at < (%s::date + interval '1 day')
        """,
        (date_from, date_to, date_from, date_to),
    ) or {}

    total_revenue = Decimal(str(totals_row.get("revenue") or 0))
    total_calls = int(totals_row.get("calls") or 0)
    total_minutes = int(Decimal(str(totals_row.get("talk_seconds") or 0)) / 60)
    total_cost = (Decimal(total_minutes) * SIP_COST_PER_MINUTE).quantize(Decimal("0.01"))
    total_profit = total_revenue - total_cost
    margin_pct = float((total_profit / total_revenue * 100)) if total_revenue > 0 else 0.0

    per_tenant_rows = db.query(
        """
        SELECT
            t.id AS tenant_id, t.business_name AS name, pl.name AS plan,
            COUNT(DISTINCT c.id) AS calls,
            COALESCE(SUM(c.talk_seconds), 0) AS talk_seconds,
            COALESCE((
                SELECT SUM(amount) FROM wallet_transactions wt
                WHERE wt.tenant_id = t.id AND wt.type = 'debit'
                  AND wt.created_at >= %s AND wt.created_at < (%s::date + interval '1 day')
            ), 0) AS revenue
        FROM tenants t
        LEFT JOIN plans pl ON pl.id = t.plan_id
        LEFT JOIN calls c ON c.tenant_id = t.id
            AND c.started_at >= %s AND c.started_at < (%s::date + interval '1 day')
        GROUP BY t.id, t.business_name, pl.name
        ORDER BY revenue DESC
        """,
        (date_from, date_to, date_from, date_to),
    )

    per_tenant = []
    for row in per_tenant_rows:
        minutes = int(Decimal(str(row["talk_seconds"] or 0)) / 60)
        revenue = Decimal(str(row["revenue"] or 0))
        cost = (Decimal(minutes) * SIP_COST_PER_MINUTE).quantize(Decimal("0.01"))
        per_tenant.append({
            "tenant_id": row["tenant_id"],
            "name": row["name"],
            "plan": row["plan"] or "—",
            "calls": int(row["calls"] or 0),
            "minutes": minutes,
            "revenue": revenue,
            "cost": cost,
            "profit": revenue - cost,
        })

    daily_rows = db.query(
        """
        SELECT day, COALESCE(SUM(revenue), 0) AS revenue,
               (SELECT COUNT(*) FROM calls WHERE DATE(started_at) = day) AS calls
        FROM (
             SELECT DATE(created_at) AS day, amount AS revenue
             FROM wallet_transactions
             WHERE type = 'debit' AND created_at >= %s AND created_at < (%s::date + interval '1 day')
        ) wt
        GROUP BY day
        ORDER BY day ASC
        """,
        (date_from, date_to),
    )

    status_rows = db.query(
        """
        SELECT order_status, COUNT(*) AS n
        FROM orders
        WHERE created_at >= %s AND created_at < (%s::date + interval '1 day')
          AND order_status = ANY(%s)
        GROUP BY order_status
        """,
        (date_from, date_to, list(CALL_STATUS_TO_ORDER_STATUS)),
    )
    status_distribution = {s: 0 for s in CALL_STATUS_TO_ORDER_STATUS}
    for row in status_rows:
        status_distribution[row["order_status"]] = int(row["n"])

    return {
        "total_revenue": total_revenue,
        "total_calls": total_calls,
        "total_minutes": total_minutes,
        "total_cost": total_cost,
        "total_profit": total_profit,
        "margin_pct": margin_pct,
        "per_tenant": per_tenant,
        "daily_breakdown": daily_rows,
        "status_distribution": status_distribution,
    }
