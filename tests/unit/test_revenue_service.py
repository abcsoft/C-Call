from decimal import Decimal
import datetime
from app.db import db
from app.services.revenue_service import get_revenue_report

def test_revenue_report(app, create_tenant):
    with app.app_context():
        # Setup tenant using fixture
        tenant, user = create_tenant("Revenue Store", "rev@store.com")
        tenant_id = tenant["id"]
        
        # Insert a wallet transaction
        wallet_row = db.query_one("SELECT id FROM wallets WHERE tenant_id = %s", (tenant_id,))
        wallet_id = wallet_row["id"]
        
        db.execute(
            """
            INSERT INTO wallet_transactions (wallet_id, tenant_id, type, amount, balance_after, description, created_at)
            VALUES (%s, %s, 'debit', 5.00, 45.00, 'Test Call', now())
            """,
            (wallet_id, tenant_id)
        )
        
        # Insert a call
        db.execute(
            """
            INSERT INTO calls (tenant_id, number, status, direction, talk_seconds, started_at)
            VALUES (%s, '01712345678', 'completed', 'outbound', 120, now())
            """,
            (tenant_id,)
        )
        
        # Insert an order
        db.execute(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status)
            VALUES (%s, 'ORD-REV-1', '01712345678', 'Rev Customer', 50.00, 'CONFIRMED')
            """,
            (tenant_id,)
        )
        
        # Call report for today
        today_str = datetime.date.today().isoformat()
        report = get_revenue_report(today_str, today_str)
        
        assert report["total_revenue"] == Decimal("5.00")
        assert report["total_calls"] == 1
        assert report["total_minutes"] == 2
        assert report["total_cost"] == Decimal("0.60")  # 2 minutes * 0.30
        assert report["total_profit"] == Decimal("4.40")
        assert report["margin_pct"] == 88.0
        
        assert len(report["per_tenant"]) >= 1
        # Find our tenant in report
        our_tenant_report = next(t for t in report["per_tenant"] if t["tenant_id"] == tenant_id)
        assert our_tenant_report["name"] == "Revenue Store"
        assert our_tenant_report["calls"] == 1
        assert our_tenant_report["minutes"] == 2
        assert our_tenant_report["revenue"] == Decimal("5.00")
        assert our_tenant_report["cost"] == Decimal("0.60")
        assert our_tenant_report["profit"] == Decimal("4.40")
        
        # Find today's daily breakdown entry
        assert len(report["daily_breakdown"]) >= 1
        # Since daily breakdown returns DATE(created_at) which is returned as date object
        our_day_report = next(d for d in report["daily_breakdown"] if d["day"].isoformat() == today_str)
        assert our_day_report["revenue"] == Decimal("5.00")
        # daily_breakdown aggregates all tenants; calls >= 1 (other real_db tests may
        # have committed calls for today).  Per-tenant precision is checked above via
        # our_tenant_report["calls"] == 1.
        assert our_day_report["calls"] >= 1
        
        assert report["status_distribution"]["CONFIRMED"] == 1
        assert report["status_distribution"]["CANCELLED"] == 0
