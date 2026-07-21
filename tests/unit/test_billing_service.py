from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.models.tenant import create_tenant
from app.models.wallet import credit_balance, get_wallet
from app.services.billing_service import finalize_call_billing
from app.services.order_service import update_status_from_call


@pytest.fixture
def billing_tenant(app):
    """Tenant with default platform rate (৳0.80/min, 60s pulse) and a
    funded wallet — matches order_service's test_setup fixture pattern."""
    with app.app_context():
        tenant_id = create_tenant({
            "business_name": "Billing Test Store",
            "email": "billing@store.com",
            "plan_name": "free",
            "status": "active"
        })
        credit_balance(tenant_id, Decimal("100.00"), "Initial Deposit")
        return tenant_id


def _make_call(tenant_id: int, status: str, talk_seconds: int, order_id: int | None = None) -> int:
    return db.insert_returning_id(
        """
        INSERT INTO calls (number, status, direction, talk_seconds, tenant_id, order_id, call_type, started_at)
        VALUES ('01799999999', %s, 'outbound', %s, %s, %s, 'direct', '2026-07-20 00:00:00+00')
        RETURNING id
        """,
        (status, talk_seconds, tenant_id, order_id),
    )


def test_direct_completed_call_bills_and_deducts_wallet(app, billing_tenant):
    """Direct completed 6s call -> billable 60s, charge ৳0.80, wallet decreases."""
    tenant_id = billing_tenant
    with app.app_context():
        balance_before = get_wallet(tenant_id)["balance"]
        call_id = _make_call(tenant_id, "completed", 6)

        result = finalize_call_billing(call_id)

        assert result["billed"] is True
        assert result["billable_seconds"] == 60
        assert result["charge"] == Decimal("0.8000")

        call = db.query_one("SELECT * FROM calls WHERE id = %s", (call_id,))
        assert call["charge"] == Decimal("0.8000")
        assert call["billable_seconds"] == 60
        assert call["wallet_transaction_id"] is not None

        balance_after = get_wallet(tenant_id)["balance"]
        assert balance_after == balance_before - Decimal("0.8000")


def test_order_completed_call_bills_once_via_update_status_from_call(app, billing_tenant):
    """Order completed 8s call -> billable 60s, charge ৳0.80, wallet decreases once only."""
    tenant_id = billing_tenant
    with app.app_context():
        balance_before = get_wallet(tenant_id)["balance"]

        order_id = db.insert_returning_id(
            """
            INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount)
            VALUES (%s, 'WC-BILL-1', '+8801700000000', 'Customer', 100.0)
            RETURNING id
            """,
            (tenant_id,)
        )
        call_id = _make_call(tenant_id, "completed", 8, order_id=order_id)
        db.insert_returning_id(
            "INSERT INTO order_call_attempts (order_id, tenant_id, attempt_no, call_id) VALUES (%s, %s, 1, %s) RETURNING id",
            (order_id, tenant_id, call_id),
        )

        update_status_from_call(
            order_id=order_id, tenant_id=tenant_id, call_result="completed",
            dtmf_digit="1", talk_seconds=8, call_id=call_id,
        )

        call = db.query_one("SELECT * FROM calls WHERE id = %s", (call_id,))
        assert call["charge"] == Decimal("0.8000")
        assert call["billable_seconds"] == 60
        assert call["wallet_transaction_id"] is not None

        balance_after_first = get_wallet(tenant_id)["balance"]
        assert balance_after_first == balance_before - Decimal("0.8000")

        # Refresh CDR (simulated by re-invoking the same path) -> no second debit.
        update_status_from_call(
            order_id=order_id, tenant_id=tenant_id, call_result="completed",
            dtmf_digit="1", talk_seconds=8, call_id=call_id,
        )
        finalize_call_billing(call_id)

        balance_after_second = get_wallet(tenant_id)["balance"]
        assert balance_after_second == balance_after_first

        attempt = db.query_one("SELECT * FROM order_call_attempts WHERE call_id = %s", (call_id,))
        assert attempt["charge"] == Decimal("0.8000")
        assert attempt["wallet_transaction_id"] == call["wallet_transaction_id"]


def test_failed_zero_second_call_charges_zero(app, billing_tenant):
    """Failed 0s call -> charge 0, no wallet deduction, billing_finalized=TRUE."""
    tenant_id = billing_tenant
    with app.app_context():
        balance_before = get_wallet(tenant_id)["balance"]
        call_id = _make_call(tenant_id, "failed", 0)

        result = finalize_call_billing(call_id)

        assert result["billed"] is False
        assert result["charge"] == Decimal("0.0000")

        call = db.query_one("SELECT * FROM calls WHERE id = %s", (call_id,))
        assert call["charge"] == Decimal("0.0000")
        assert call["wallet_transaction_id"] is None
        # billing_finalized must be TRUE even for zero-charge — this is the
        # idempotency sentinel; without it the call would be re-processed forever.
        assert call["billing_finalized"] is True

        balance_after = get_wallet(tenant_id)["balance"]
        assert balance_after == balance_before


def test_zero_charge_call_idempotency(app, billing_tenant):
    """Zero-charge (no_answer) call finalized twice — second invocation must
    return 'already_billed' and must NOT touch the wallet a second time."""
    tenant_id = billing_tenant
    with app.app_context():
        balance_before = get_wallet(tenant_id)["balance"]
        call_id = _make_call(tenant_id, "no_answer", 0)

        first = finalize_call_billing(call_id)
        assert first["billed"] is False
        assert first["charge"] == Decimal("0.0000")

        second = finalize_call_billing(call_id)
        assert second["billed"] is False
        assert second["reason"] == "already_billed"

        call = db.query_one("SELECT * FROM calls WHERE id = %s", (call_id,))
        assert call["billing_finalized"] is True
        assert call["wallet_transaction_id"] is None

        # Wallet must be untouched through both invocations.
        assert get_wallet(tenant_id)["balance"] == balance_before


def test_no_answer_and_busy_set_billing_finalized(app, billing_tenant):
    """no_answer and busy calls — both terminal, both zero-charge — must set
    billing_finalized=TRUE so they are never re-processed."""
    tenant_id = billing_tenant
    with app.app_context():
        for status in ("no_answer", "busy"):
            call_id = _make_call(tenant_id, status, 0)
            result = finalize_call_billing(call_id)
            assert result["billed"] is False

            call = db.query_one("SELECT * FROM calls WHERE id = %s", (call_id,))
            assert call["billing_finalized"] is True, f"billing_finalized not set for status={status}"
            assert call["wallet_transaction_id"] is None


def test_finalize_call_billing_is_idempotent(app, billing_tenant):
    """Calling finalize_call_billing twice on the same call must not double-bill."""
    tenant_id = billing_tenant
    with app.app_context():
        call_id = _make_call(tenant_id, "completed", 30)

        first = finalize_call_billing(call_id)
        assert first["billed"] is True

        second = finalize_call_billing(call_id)
        assert second["billed"] is False
        assert second["reason"] == "already_billed"
        assert second["wallet_transaction_id"] == first["wallet_transaction_id"]


def test_finalize_call_billing_skips_when_tenant_is_null(app):
    with app.app_context():
        call_id = db.insert_returning_id(
            "INSERT INTO calls (number, status, direction, talk_seconds, tenant_id, started_at) "
            "VALUES ('01799999999', 'completed', 'outbound', 30, NULL, '2026-07-20 00:00:00+00') RETURNING id"
        )
        result = finalize_call_billing(call_id)
        assert result["billed"] is False
        assert result["reason"] == "no_tenant"


def test_historical_completed_unbilled_call_shows_warning_not_fake_charge(app, billing_tenant):
    """A historical completed call with talk_seconds>0, charge=0, no
    wallet_transaction_id (predates billing_service) must be flagged as
    unbilled by the CDR query helper, not silently shown with a recalculated
    charge."""
    tenant_id = billing_tenant
    with app.app_context():
        call_id = _make_call(tenant_id, "completed", 45)
        # Do NOT call finalize_call_billing — simulates a pre-existing
        # historical row that was never billed.

        from app.blueprints.customer.cdr import query_calls_common

        class FakeArgs(dict):
            def get(self, k, default=None):
                return super().get(k, default)

        context = query_calls_common(tenant_id, FakeArgs(limit="10"))
        row = next(c for c in context["calls"] if c["id"] == call_id)

        assert row["is_unbilled_historical"] is True
        assert row["charge"] == Decimal("0.00") or row["charge"] == 0
        assert row["display_charge"] == "৳0.00"
