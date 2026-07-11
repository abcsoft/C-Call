from decimal import Decimal
import datetime
from app.db import db
from app.services.admin_service import (
    get_platform_stats,
    get_low_balance_tenants,
    get_failed_webhooks_last_hour,
    get_sip_provider_status,
    create_tenant_manual,
    impersonate_tenant,
    is_impersonation_active,
    end_impersonation
)

def test_admin_service_stats_and_status(app, create_tenant):
    with app.app_context():
        tenant, user = create_tenant("Admin Store", "admin@store.com")
        tenant_id = tenant["id"]
        
        # Set tenant status to active
        db.execute("UPDATE tenants SET status = 'active' WHERE id = %s", (tenant_id,))
        
        # Insert a sip provider
        provider_id = db.insert_returning_id(
            "INSERT INTO sip_providers (name, sip_domain, sip_port, sip_user, sip_password, status) VALUES ('Test SIP', 'sip.test.com', 5060, 'user', 'pass', 'active') RETURNING id"
        )
        
        # Test get_sip_provider_status
        providers = get_sip_provider_status()
        assert len(providers) >= 1
        our_provider = next(p for p in providers if p["id"] == provider_id)
        assert our_provider["healthy"] is True
        
        # Test get_low_balance_tenants
        db.execute("UPDATE wallets SET balance = 5.00 WHERE tenant_id = %s", (tenant_id,))
        low_bal = get_low_balance_tenants(Decimal("10.00"))
        assert len(low_bal) >= 1
        our_low = next(t for t in low_bal if t["id"] == tenant_id)
        assert our_low["balance"] == Decimal("5.00")
        
        # Test get_failed_webhooks_last_hour
        order_id = db.insert_returning_id(
            "INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount, order_status) VALUES (%s, 'ORD-ADM-1', '01712345678', 'Admin Customer', 10.00, 'FAILED') RETURNING id",
            (tenant_id,)
        )
        db.execute(
            """
            INSERT INTO webhook_logs (tenant_id, order_id, url, payload, status_code, response_body, success, attempt_no, sent_at)
            VALUES (%s, %s, 'http://test.com', '{}', 500, 'error', false, 1, now())
            """,
            (tenant_id, order_id)
        )
        assert get_failed_webhooks_last_hour() == 1
        
        # Test get_platform_stats
        stats = get_platform_stats()
        assert stats["total_tenants"] >= 1
        assert stats["active_tenants"] >= 1
        
        # Test create_tenant_manual
        new_tenant_id = create_tenant_manual("Manual Tenant", "manual@tenant.com", "Password123!", "free", Decimal("50.00"))
        assert new_tenant_id is not None
        new_wallet = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (new_tenant_id,))
        assert new_wallet["balance"] == Decimal("50.00")

def test_admin_service_impersonation(app, create_tenant):
    # impersonate_tenant uses session, so we need a request/session context
    with app.test_request_context():
        tenant, user = create_tenant("Impersonate Store", "imp@store.com")
        tenant_id = tenant["id"]
        admin_user_id = user["id"]
        
        # Fetch target user (the registered admin of the tenant)
        target_admin_row = db.query_one("SELECT * FROM users WHERE tenant_id = %s AND role = 'admin'", (tenant_id,))
        assert target_admin_row is not None
        
        # Test impersonation validations
        try:
            impersonate_tenant(admin_user_id=admin_user_id, tenant_id=tenant_id, reason="")
            assert False, "Should raise ValueError for empty reason"
        except ValueError:
            pass
            
        try:
            impersonate_tenant(admin_user_id=admin_user_id, tenant_id=999999, reason="Testing invalid tenant")
            assert False, "Should raise ValueError for invalid tenant"
        except ValueError:
            pass
        
        # Impersonate successfully
        res = impersonate_tenant(admin_user_id=admin_user_id, tenant_id=tenant_id, reason="Testing impersonation")
        assert res["target_user"]["id"] == target_admin_row["id"]
        assert is_impersonation_active() is True
        
        # End impersonation
        imp_id = end_impersonation()
        assert imp_id == admin_user_id
        assert is_impersonation_active() is False
