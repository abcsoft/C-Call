from __future__ import annotations

import datetime

import psycopg
import pytz

from app.db import db
from app.models.order import get_order_by_id_system
from app.models.tenant import create_tenant
from app.scheduler import with_advisory_lock
from app.services.retry_service import (
    _apply_dnd,
    cancel_pending_retries,
    process_due_retries,
    schedule_retry,
)

DHAKA = pytz.timezone("Asia/Dhaka")


def _make_order(tenant_id: int, max_attempts: int = 3, attempt_count: int = 0, **overrides) -> int:
    return db.insert_returning_id(
        """
        INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount,
                             order_status, max_attempts, attempt_count)
        VALUES (%s, %s, '01712345678', 'Rahim', 500.0, %s, %s, %s)
        RETURNING id
        """,
        (
            tenant_id,
            overrides.get("external_order_id", f"WC-{datetime.datetime.now().timestamp()}"),
            overrides.get("order_status", "CALLING"),
            max_attempts,
            attempt_count,
        ),
    )


def _make_rule(tenant_id: int, attempt_no: int, delay_minutes: int, dnd_start: str, dnd_end: str, is_enabled: bool = True) -> None:
    db.execute(
        """
        INSERT INTO retry_rules (tenant_id, attempt_no, delay_minutes, retry_on, dnd_start, dnd_end, max_attempts, is_enabled)
        VALUES (%s, %s, %s, ARRAY['no_answer','busy'], %s, %s, 3, %s)
        ON CONFLICT (tenant_id, attempt_no) DO UPDATE SET
            delay_minutes = EXCLUDED.delay_minutes, dnd_start = EXCLUDED.dnd_start,
            dnd_end = EXCLUDED.dnd_end, is_enabled = EXCLUDED.is_enabled
        """,
        (tenant_id, attempt_no, delay_minutes, dnd_start, dnd_end, is_enabled),
    )


# ---------------------------------------------------------------------------
# _apply_dnd — pure function, deterministic fixed datetimes (no DB needed)
# ---------------------------------------------------------------------------

def test_dnd_pushes_retry_past_window():
    """A retry time inside a same-day DND window (e.g. 13:00-14:00) gets
    pushed to dnd_end + 5 minutes."""
    # 2026-06-19 13:30 Dhaka time -> 07:30 UTC
    inside = datetime.datetime(2026, 6, 19, 7, 30, tzinfo=datetime.timezone.utc)
    result = _apply_dnd(inside, datetime.time(13, 0), datetime.time(14, 0), DHAKA)

    expected_local = DHAKA.localize(datetime.datetime(2026, 6, 19, 14, 5))
    assert result == expected_local.astimezone(datetime.timezone.utc)

    # Outside the window — unchanged.
    outside = datetime.datetime(2026, 6, 19, 4, 0, tzinfo=datetime.timezone.utc)  # 10:00 Dhaka
    assert _apply_dnd(outside, datetime.time(13, 0), datetime.time(14, 0), DHAKA) == outside


def test_overnight_dnd_handled_correctly():
    """Overnight window 21:00 -> 09:00 (dnd_start > dnd_end) on both sides
    of midnight."""
    dnd_start, dnd_end = datetime.time(21, 0), datetime.time(9, 0)

    # 23:00 Dhaka (evening side) -> pushed to 09:05 *next* day.
    evening_utc = datetime.datetime(2026, 6, 19, 17, 0, tzinfo=datetime.timezone.utc)  # 23:00 Dhaka
    result = _apply_dnd(evening_utc, dnd_start, dnd_end, DHAKA)
    expected = DHAKA.localize(datetime.datetime(2026, 6, 20, 9, 5)).astimezone(datetime.timezone.utc)
    assert result == expected

    # 05:00 Dhaka (early-morning side) -> pushed to 09:05 *same* day.
    morning_utc = datetime.datetime(2026, 6, 18, 23, 0, tzinfo=datetime.timezone.utc)  # 05:00 Dhaka on the 19th
    result2 = _apply_dnd(morning_utc, dnd_start, dnd_end, DHAKA)
    expected2 = DHAKA.localize(datetime.datetime(2026, 6, 19, 9, 5)).astimezone(datetime.timezone.utc)
    assert result2 == expected2

    # 14:00 Dhaka — outside the overnight window entirely — unchanged.
    daytime_utc = datetime.datetime(2026, 6, 19, 8, 0, tzinfo=datetime.timezone.utc)  # 14:00 Dhaka
    assert _apply_dnd(daytime_utc, dnd_start, dnd_end, DHAKA) == daytime_utc


# ---------------------------------------------------------------------------
# schedule_retry — real DB (test Postgres via the `app` fixture)
# ---------------------------------------------------------------------------

def test_retry_scheduled_with_correct_delay(app):
    with app.app_context():
        tenant_id = create_tenant("Retry Delay Store", "retry-delay@store.com")
        order_id = _make_order(tenant_id, max_attempts=3, attempt_count=0)
        # Zero-width DND window never matches, isolating the delay assertion.
        _make_rule(tenant_id, attempt_no=1, delay_minutes=15, dnd_start="00:00", dnd_end="00:00")

        before = datetime.datetime.now(datetime.timezone.utc)
        result = schedule_retry(order_id, tenant_id, current_attempt=0)
        assert result

        order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
        assert order["order_status"] == "RETRY_SCHEDULED"
        delay = order["next_retry_at"] - before
        assert datetime.timedelta(minutes=14, seconds=55) <= delay <= datetime.timedelta(minutes=15, seconds=10)


def test_max_retries_sets_unreachable(app):
    with app.app_context():
        tenant_id = create_tenant("Max Retries Store", "max-retries@store.com")
        order_id = _make_order(tenant_id, max_attempts=2, attempt_count=2)

        result = schedule_retry(order_id, tenant_id, current_attempt=2)
        assert not result

        order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
        assert order["order_status"] == "UNREACHABLE"
        assert order["next_retry_at"] is None


def test_disabled_retry_rule_sets_unreachable(app):
    with app.app_context():
        tenant_id = create_tenant("Disabled Rule Store", "disabled-rule@store.com")
        order_id = _make_order(tenant_id, max_attempts=5, attempt_count=0)
        _make_rule(tenant_id, attempt_no=1, delay_minutes=30, dnd_start="21:00", dnd_end="09:00", is_enabled=False)

        result = schedule_retry(order_id, tenant_id, current_attempt=0)
        assert not result

        order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
        assert order["order_status"] == "UNREACHABLE"
        assert order["next_retry_at"] is None


def test_retry_respects_retry_on_filter(app):
    """A call_result not present in the next attempt's retry_rules.retry_on
    must go straight to UNREACHABLE — no retry scheduled even though
    attempts remain. Regression test for the bug where order_service
    collapsed call_result into a generic NO_ANSWER status before calling
    schedule_retry(), so retry_on filtering never actually ran."""
    with app.app_context():
        tenant_id = create_tenant("Retry Filter Store", "retry-filter@store.com")
        order_id = _make_order(tenant_id, max_attempts=5, attempt_count=0)
        # Attempt 1 retries ONLY on 'no_answer', not 'busy'.
        db.execute(
            "UPDATE retry_rules SET retry_on = %s WHERE tenant_id = %s AND attempt_no = 1",
            (["no_answer"], tenant_id),
        )

        # 'busy' is NOT in retry_on -> must go straight to UNREACHABLE, no retry.
        result = schedule_retry(order_id, tenant_id, current_attempt=0, call_result="busy")
        assert result is False

        order = get_order_by_id_system(order_id)
        assert order["order_status"] == "UNREACHABLE"
        assert order["next_retry_at"] is None


def test_retry_allows_result_present_in_retry_on(app):
    """Sanity counterpart: a call_result that IS in retry_on still
    schedules normally — the filter doesn't block everything."""
    with app.app_context():
        tenant_id = create_tenant("Retry Allow Store", "retry-allow@store.com")
        order_id = _make_order(tenant_id, max_attempts=5, attempt_count=0)
        db.execute(
            "UPDATE retry_rules SET retry_on = %s WHERE tenant_id = %s AND attempt_no = 1",
            (["no_answer"], tenant_id),
        )

        result = schedule_retry(order_id, tenant_id, current_attempt=0, call_result="no_answer")
        assert result is True

        order = get_order_by_id_system(order_id)
        assert order["order_status"] == "RETRY_SCHEDULED"
        assert order["next_retry_at"] is not None


def test_cancel_pending_retries(app):
    with app.app_context():
        tenant_id = create_tenant("Cancel Store", "cancel@store.com")
        order_id = _make_order(tenant_id, order_status="RETRY_SCHEDULED")
        db.execute(
            "UPDATE orders SET next_retry_at = now() + interval '10 minutes' WHERE id = %s",
            (order_id,),
        )

        cancel_pending_retries(order_id, tenant_id)

        order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
        assert order["order_status"] == "CANCELLED"
        assert order["next_retry_at"] is None


# ---------------------------------------------------------------------------
# process_due_retries
# ---------------------------------------------------------------------------

def test_process_due_retries_dispatches_calls(app):
    with app.app_context():
        tenant_id = create_tenant("Due Retry Store", "due-retry@store.com")
        order_id = _make_order(tenant_id, order_status="RETRY_SCHEDULED", attempt_count=1)
        db.execute(
            "UPDATE orders SET next_retry_at = now() - interval '1 minute' WHERE id = %s",
            (order_id,),
        )

        processed = process_due_retries()
        assert processed >= 1

        order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
        # Claimed out of RETRY_SCHEDULED regardless of whether the call itself
        # succeeded (no SIP/CLI fixture set up here) — that's the contract:
        # process_due_retries dispatches, it doesn't guarantee call success.
        assert order["order_status"] != "RETRY_SCHEDULED"
        assert order["attempt_count"] == 2


def test_process_due_retries_skips_order_already_claimed(app, monkeypatch):
    """Regression test for the dedup race: if an order's claim UPDATE
    matches zero rows (already claimed by another run between the SELECT
    and the UPDATE), process_due_retries must skip it — not dispatch a
    second call anyway. Simulated by flipping the order's status away
    from RETRY_SCHEDULED right before the claim UPDATE runs, mimicking
    a concurrent run winning the race first."""
    with app.app_context():
        tenant_id = create_tenant("Race Condition Store", "race-retry@store.com")
        order_id = _make_order(tenant_id, order_status="RETRY_SCHEDULED", attempt_count=1)
        db.execute(
            "UPDATE orders SET next_retry_at = now() - interval '1 minute' WHERE id = %s",
            (order_id,),
        )

        dispatch_calls = []
        monkeypatch.setattr(
            "app.services.order_service.queue_confirmation_call",
            lambda oid, tid: dispatch_calls.append(oid),
        )

        # get_due_retry_orders() already returned this order (stale read);
        # before process_due_retries() reaches its claim UPDATE, simulate
        # a concurrent run winning the race and claiming it first.
        stale_due_list = [{"id": order_id, "tenant_id": tenant_id}]
        monkeypatch.setattr("app.services.retry_service.get_due_retry_orders", lambda limit=50: stale_due_list)
        db.execute("UPDATE orders SET order_status = 'CALL_QUEUED' WHERE id = %s", (order_id,))

        processed = process_due_retries()

        assert processed == 0
        assert dispatch_calls == []  # never dispatched a second time


def test_process_due_retries_respects_limit_of_50(app):
    with app.app_context():
        tenant_id = create_tenant("Bulk Retry Store", "bulk-retry@store.com")
        for _ in range(3):
            order_id = _make_order(tenant_id, order_status="RETRY_SCHEDULED", attempt_count=0)
            db.execute(
                "UPDATE orders SET next_retry_at = now() - interval '1 minute' WHERE id = %s",
                (order_id,),
            )

        processed = process_due_retries()
        assert processed == 3  # well under the 50 cap, just proving the path runs for >1 order


# ---------------------------------------------------------------------------
# Distributed advisory lock (app/scheduler.py)
# ---------------------------------------------------------------------------

def test_concurrent_scheduler_uses_advisory_lock(app):
    """Second caller is skipped while another connection already holds the
    cluster-wide advisory lock; runs normally once it's released."""
    with app.app_context():
        calls = []
        fn = lambda: calls.append(1)  # noqa: E731

        test_db_url = app.config["DATABASE_URL"]
        holder_conn = psycopg.connect(test_db_url, autocommit=True)
        try:
            holder_conn.execute("SELECT pg_try_advisory_lock(12345)")

            with_advisory_lock(fn)()
            assert calls == [], "job must not run while another connection holds the lock"
        finally:
            holder_conn.execute("SELECT pg_advisory_unlock(12345)")
            holder_conn.close()

        with_advisory_lock(fn)()
        assert calls == [1], "job must run once the lock is free"
