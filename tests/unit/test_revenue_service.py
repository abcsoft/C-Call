from decimal import Decimal
from app.db import db
from app.services.revenue_service import get_revenue_report

# Unique historical date — no other test inserts data on this date,
# so daily_breakdown["calls"] == 1 is deterministic without platform-wide
# cleanup between runs.  Model A isolation: unique date window.
_REPORT_DATE = "2020-06-15"
_REPORT_TS = "2020-06-15 10:00:00+00"


def test_revenue_report(app, create_tenant):
    with app.app_context():
        tenant, user = create_tenant("Revenue Store", "rev@store.com")
        tenant_id = tenant["id"]

        wallet_row = db.query_one("SELECT id FROM wallets WHERE tenant_id = %s", (tenant_id,))
        wallet_id = wallet_row["id"]

        # Insert data at the unique historical timestamp — all queries below
        # filter to [_REPORT_DATE, _REPORT_DATE] so no other test's rows appear.
        db.execute(
            """
            INSERT INTO wallet_transactions
              (wallet_id, tenant_id, type, amount, balance_after, description, created_at)
            VALUES (%s, %s, 'debit', 5.00, 45.00, 'Test Call', %s)
            """,
            (wallet_id, tenant_id, _REPORT_TS),
        )

        db.execute(
            """
            INSERT INTO calls
              (tenant_id, number, status, direction, talk_seconds, started_at)
            VALUES (%s, '01712345678', 'completed', 'outbound', 120, %s)
            """,
            (tenant_id, _REPORT_TS),
        )

        db.execute(
            """
            INSERT INTO orders
              (tenant_id, external_order_id, phone, customer_name, amount,
               order_status, created_at)
            VALUES (%s, 'ORD-REV-1', '01712345678', 'Rev Customer', 50.00,
                    'CONFIRMED', %s)
            """,
            (tenant_id, _REPORT_TS),
        )

        report = get_revenue_report(_REPORT_DATE, _REPORT_DATE)

        # Top-level platform aggregates — only our tenant has data on _REPORT_DATE.
        assert report["total_revenue"] == Decimal("5.00")
        assert report["total_calls"] == 1
        assert report["total_minutes"] == 2
        assert report["total_cost"] == Decimal("0.60")   # 2 min × 0.30
        assert report["total_profit"] == Decimal("4.40")
        assert report["margin_pct"] == 88.0

        # Per-tenant row scoped to our tenant.
        assert len(report["per_tenant"]) >= 1
        our_tenant_report = next(
            t for t in report["per_tenant"] if t["tenant_id"] == tenant_id
        )
        assert our_tenant_report["name"] == "Revenue Store"
        assert our_tenant_report["calls"] == 1
        assert our_tenant_report["minutes"] == 2
        assert our_tenant_report["revenue"] == Decimal("5.00")
        assert our_tenant_report["cost"] == Decimal("0.60")
        assert our_tenant_report["profit"] == Decimal("4.40")

        # Daily breakdown: one day entry at _REPORT_DATE with exactly one call
        # because no other test inserts calls on 2020-06-15.
        assert len(report["daily_breakdown"]) >= 1
        our_day_report = next(
            d for d in report["daily_breakdown"]
            if d["day"].isoformat() == _REPORT_DATE
        )
        assert our_day_report["revenue"] == Decimal("5.00")
        assert our_day_report["calls"] == 1   # exact — unique date window

        # Status distribution for orders created on _REPORT_DATE.
        assert report["status_distribution"]["CONFIRMED"] == 1
        assert report["status_distribution"]["CANCELLED"] == 0
