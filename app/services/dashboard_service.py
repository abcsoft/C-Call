from __future__ import annotations

import datetime
import math
import logging
from decimal import Decimal
from typing import Any
from app.db import db
from app.models.wallet import get_wallet
from app.models.order import get_orders

import json

logger = logging.getLogger("ccall")

class DashboardJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return {"__decimal__": str(obj)}
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return {"__datetime__": obj.isoformat()}
        return super().default(obj)

def dashboard_json_decoder(dct):
    if "__decimal__" in dct:
        return Decimal(dct["__decimal__"])
    if "__datetime__" in dct:
        val = dct["__datetime__"]
        try:
            return datetime.datetime.fromisoformat(val)
        except ValueError:
            try:
                return datetime.date.fromisoformat(val)
            except ValueError:
                return val
    return dct

def get_dashboard_stats(tenant_id: int) -> dict[str, Any]:
    """Retrieve all current dashboard KPIs and statistics for a tenant."""
    from app.extensions import redis_ext
    
    cache_key = f"tenant:stats:{tenant_id}"
    try:
        cached = redis_ext.client.get(cache_key)
        if cached:
            val = cached.decode("utf-8") if isinstance(cached, bytes) else cached
            return json.loads(val, object_hook=dashboard_json_decoder)
    except Exception as e:
        logger.error(f"Redis get stats cache failed: {e}")

    today = datetime.date.today()
    seven_days_ago = today - datetime.timedelta(days=6)
    
    # 1. Fetch Today's & 7-day consolidated Orders Stats in a single conditional aggregation query
    order_stats = db.query_one(
        """
        SELECT 
            COUNT(*) FILTER (WHERE created_at >= %s) as today_orders,
            COUNT(*) FILTER (WHERE order_status = 'CONFIRMED' AND created_at >= %s) as today_confirmed,
            COUNT(*) FILTER (WHERE order_status = 'CANCELLED' AND created_at >= %s) as today_cancelled,
            COUNT(*) FILTER (WHERE order_status = 'NO_ANSWER' AND created_at >= %s) as today_no_answer,
            COUNT(*) FILTER (WHERE order_status = 'RETRY_SCHEDULED' AND created_at >= %s) as today_pending_retry,
            COUNT(*) FILTER (WHERE order_status = 'UNREACHABLE' AND created_at >= %s) as today_unreachable,
            COUNT(*) as total_7d,
            COUNT(*) FILTER (WHERE order_status = 'CONFIRMED') as confirmed_7d
        FROM orders
        WHERE tenant_id = %s AND created_at >= %s
        """,
        (today, today, today, today, today, today, tenant_id, seven_days_ago)
    ) or {}
    
    today_orders = order_stats.get("today_orders") or 0
    today_confirmed = order_stats.get("today_confirmed") or 0
    today_cancelled = order_stats.get("today_cancelled") or 0
    today_no_answer = order_stats.get("today_no_answer") or 0
    today_pending_retry = order_stats.get("today_pending_retry") or 0
    today_unreachable = order_stats.get("today_unreachable") or 0
    
    # 2. Wallet Balance & Alert Info
    wallet = get_wallet(tenant_id) or {}
    wallet_balance = Decimal(wallet.get("balance") or 0)
    
    # 3. 7-day Success Rate: confirmed / total for last 7 days (including today)
    total_7d = order_stats.get("total_7d") or 0
    confirmed_7d = order_stats.get("confirmed_7d") or 0
    success_rate_7d = round((confirmed_7d / total_7d) * 100, 1) if total_7d > 0 else 0.0
    
    # 4. Calls today & Minutes today
    call_stats = db.query_one(
        """
        SELECT 
            COUNT(*) as total_calls,
            COALESCE(SUM(talk_seconds), 0) as total_seconds
        FROM calls
        WHERE tenant_id = %s AND started_at >= %s
        """,
        (tenant_id, today)
    ) or {}
    calls_today = call_stats.get("total_calls") or 0
    total_seconds = call_stats.get("total_seconds") or 0
    minutes_today = int(math.ceil(total_seconds / 60.0))
    
    # Cost today: sum of debits from wallet_transactions today
    cost_stats = db.query_one(
        """
        SELECT COALESCE(SUM(amount), 0) as total_cost
        FROM wallet_transactions
        WHERE tenant_id = %s AND type = 'debit' AND created_at >= %s
        """,
        (tenant_id, today)
    ) or {}
    cost_today = Decimal(cost_stats.get("total_cost") or 0)
    
    # Recent 10 orders
    recent_orders = get_orders(tenant_id, limit=10)
    
    stats = {
        "today_orders": today_orders,
        "today_confirmed": today_confirmed,
        "today_cancelled": today_cancelled,
        "today_no_answer": today_no_answer,
        "today_pending_retry": today_pending_retry,
        "today_unreachable": today_unreachable,
        "wallet_balance": wallet_balance,
        "success_rate_7d": success_rate_7d,
        "calls_today": calls_today,
        "minutes_today": minutes_today,
        "cost_today": cost_today,
        "recent_orders": recent_orders
    }
    
    try:
        redis_ext.client.setex(
            cache_key,
            60,
            json.dumps(stats, cls=DashboardJSONEncoder)
        )
    except Exception as e:
        logger.error(f"Redis set stats cache failed: {e}")
        
    return stats

def get_chart_data(tenant_id: int, days: int = 7) -> dict[str, Any]:
    """Retrieve historical daily order stats for the dashboard chart."""
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=days-1)
    
    rows = db.query(
        """
        SELECT 
            DATE(created_at AT TIME ZONE 'Asia/Dhaka') as day,
            SUM(CASE WHEN order_status = 'CONFIRMED' THEN 1 ELSE 0 END) as confirmed,
            SUM(CASE WHEN order_status = 'CANCELLED' THEN 1 ELSE 0 END) as cancelled,
            SUM(CASE WHEN order_status = 'NO_ANSWER' THEN 1 ELSE 0 END) as no_answer
        FROM orders
        WHERE tenant_id = %s AND created_at >= %s
        GROUP BY DATE(created_at AT TIME ZONE 'Asia/Dhaka')
        ORDER BY day ASC
        """,
        (tenant_id, start_date)
    )
    
    # Safe date key parsing
    stats_by_day = {}
    for r in rows:
        day_val = r["day"]
        if isinstance(day_val, datetime.datetime):
            day_val = day_val.date()
        elif isinstance(day_val, str):
            try:
                day_val = datetime.date.fromisoformat(day_val)
            except ValueError:
                pass
        stats_by_day[day_val] = r

    labels = []
    confirmed = []
    cancelled = []
    no_answer = []
    
    for i in range(days):
        d = start_date + datetime.timedelta(days=i)
        labels.append(d.strftime("%b %d"))
        
        day_stats = stats_by_day.get(d)
        if day_stats:
            confirmed.append(int(day_stats["confirmed"] or 0))
            cancelled.append(int(day_stats["cancelled"] or 0))
            no_answer.append(int(day_stats["no_answer"] or 0))
        else:
            confirmed.append(0)
            cancelled.append(0)
            no_answer.append(0)
            
    return {
        "labels": labels,
        "confirmed": confirmed,
        "cancelled": cancelled,
        "no_answer": no_answer
    }
