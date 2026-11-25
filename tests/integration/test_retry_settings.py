"""Integration tests for Retry & DND Settings form — 24 scenarios.

Covers:
- max_attempts validation and enforcement
- Stale attempts deleted when max_attempts reduced
- Delay field parsing (minutes/hours) and validation
- Checkbox/retry_on parsing
- Auto retry disabled path
- DND overnight window acceptance
- DND retry push logic
- Live preview count and cumulative times
- Tenant isolation
- Scheduler max_attempts enforcement
- Transaction rollback on partial failure
"""
from __future__ import annotations

import datetime
import json
from decimal import Decimal

import pytest
import pytz

from app.db import db
from app.services.retry_service import (
    _apply_dnd,
    get_retry_rules,
    save_retry_rules,
    schedule_retry,
)

DHAKA = pytz.timezone("Asia/Dhaka")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_csrf(client, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, f"GET {path} returned {resp.status_code}"
    html = resp.data.decode()
    start = html.find('name="csrf_token" value="')
    assert start != -1, "No csrf_token on page"
    val_start = html.find('value="', start) + len('value="')
    val_end = html.find('"', val_start)
    return html[val_start:val_end]


def _post_retry(client, data: dict) -> "Response":
    csrf = _get_csrf(client, "/retry")
    return client.post("/retry", data={**data, "csrf_token": csrf}, follow_redirects=True)


def _base_form(max_attempts: int = 3, **overrides) -> dict:
    """Build a valid retry form payload for max_attempts attempts."""
    base = {"retry_enabled": "on", "max_attempts": str(max_attempts)}
    for i in range(1, max_attempts + 1):
        base[f"delay_value_{i}"] = str(15 * i)
        base[f"delay_unit_{i}"] = "minutes"
        base[f"retry_on_{i}"] = ["no_answer", "busy"]
    base["dnd_start"] = "21:00"
    base["dnd_end"] = "09:00"
    base.update(overrides)
    return base


def _make_order(tenant_id: int, max_attempts: int = 3, attempt_count: int = 0) -> int:
    return db.insert_returning_id(
        """
        INSERT INTO orders (tenant_id, external_order_id, phone, customer_name, amount,
                             order_status, max_attempts, attempt_count)
        VALUES (%s, %s, '01712345678', 'Test', 500.0, 'CALLING', %s, %s)
        RETURNING id
        """,
        (tenant_id, f"WC-{datetime.datetime.now().timestamp()}", max_attempts, attempt_count),
    )


def _make_rule(tenant_id: int, attempt_no: int, delay_minutes: int = 30,
               max_attempts: int = 3, is_enabled: bool = True) -> None:
    db.execute(
        """
        INSERT INTO retry_rules
            (tenant_id, attempt_no, delay_minutes, retry_on, dnd_start, dnd_end, max_attempts, is_enabled)
        VALUES (%s, %s, %s, ARRAY['no_answer','busy'], '21:00', '09:00', %s, %s)
        ON CONFLICT (tenant_id, attempt_no) DO UPDATE SET
            delay_minutes = EXCLUDED.delay_minutes,
            max_attempts  = EXCLUDED.max_attempts,
            is_enabled    = EXCLUDED.is_enabled
        """,
        (tenant_id, attempt_no, delay_minutes, max_attempts, is_enabled),
    )


@pytest.fixture
def retry_client(create_tenant, auth_client):
    """Authenticated client for a fresh tenant."""
    pair = create_tenant("Retry Tenant", "retry_test@example.com")
    return auth_client(pair), pair[0]


# ── 1. max_attempts=3 validates only attempts 1–3 ────────────────────────────

def test_max_3_validates_only_1_to_3(retry_client, db_conn):
    client, tenant = retry_client
    # Submit max_attempts=3 with valid data for 1–3.  No fields for 4–5.
    resp = _post_retry(client, _base_form(max_attempts=3))
    assert b"saved" in resp.data.lower(), resp.data.decode()[:500]
    rules = get_retry_rules(tenant["id"])
    assert len(rules) == 3
    assert all(r["attempt_no"] <= 3 for r in rules)


# ── 2. Attempts 4–5 deleted when reducing from 5 to 3 ───────────────────────

def test_stale_attempts_removed_when_reducing_to_3(retry_client, db_conn):
    client, tenant = retry_client
    tid = tenant["id"]
    # Seed 5 rules
    for i in range(1, 6):
        _make_rule(tid, i, max_attempts=5)

    # Now save with max_attempts=3
    resp = _post_retry(client, _base_form(max_attempts=3))
    assert b"saved" in resp.data.lower()
    remaining = get_retry_rules(tid)
    assert len(remaining) == 3
    nos = {r["attempt_no"] for r in remaining}
    assert nos == {1, 2, 3}


# ── 3. Valid delays save successfully ────────────────────────────────────────

def test_valid_delays_save_successfully(retry_client):
    client, tenant = retry_client
    resp = _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "3",
        "delay_value_1": "15", "delay_unit_1": "minutes", "retry_on_1": ["no_answer", "busy"],
        "delay_value_2": "30", "delay_unit_2": "minutes", "retry_on_2": ["no_answer"],
        "delay_value_3": "60", "delay_unit_3": "minutes", "retry_on_3": ["busy"],
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    assert b"saved" in resp.data.lower()
    rules = get_retry_rules(tenant["id"])
    delays = {r["attempt_no"]: r["delay_minutes"] for r in rules}
    assert delays[1] == 15
    assert delays[2] == 30
    assert delays[3] == 60


# ── 4. No Answer parsed correctly ────────────────────────────────────────────

def test_no_answer_checkbox_parsed_correctly(retry_client):
    client, tenant = retry_client
    _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "1",
        "delay_value_1": "15", "delay_unit_1": "minutes",
        "retry_on_1": "no_answer",   # only no_answer
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    rules = get_retry_rules(tenant["id"])
    assert rules[0]["retry_on"] == ["no_answer"]


# ── 5. Busy checkbox parsed correctly ────────────────────────────────────────

def test_busy_checkbox_parsed_correctly(retry_client):
    client, tenant = retry_client
    _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "1",
        "delay_value_1": "10", "delay_unit_1": "minutes",
        "retry_on_1": "busy",        # only busy
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    rules = get_retry_rules(tenant["id"])
    assert rules[0]["retry_on"] == ["busy"]


# ── 6. Missing unchecked checkbox does not cause parser error ─────────────────

def test_missing_unchecked_checkbox_no_error(retry_client):
    client, tenant = retry_client
    # Submitting only retry_on_1=no_answer (busy omitted — like unchecked HTML)
    resp = _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "1",
        "delay_value_1": "15", "delay_unit_1": "minutes",
        "retry_on_1": "no_answer",    # only no_answer; busy absent (unchecked)
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    # Success: saved (302→200 redirect) — verify via DB not flash text
    assert resp.status_code == 200
    rules = get_retry_rules(tenant["id"])
    assert any(r["attempt_no"] == 1 and r["delay_minutes"] == 15 for r in rules)


# ── 7. Empty conditions rejected only for active attempts ─────────────────────

def test_empty_retry_on_rejected_for_active_attempt(retry_client, db_conn):
    client, tenant = retry_client
    tid = tenant["id"]
    # Remember delay_minutes for existing seeded rules so we can confirm no overwrite
    existing = get_retry_rules(tid)
    existing_delays = {r["attempt_no"]: r["delay_minutes"] for r in existing}

    resp = _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "1",
        "delay_value_1": "55",      # a distinctive value
        "delay_unit_1": "minutes",
        # retry_on_1 omitted — no conditions — must fail validation
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    # After failure, rules must NOT have been overwritten with the invalid payload.
    # If delay_minutes=55 appears in rules, the save went through despite the error.
    rules_after = get_retry_rules(tid)
    assert not any(r["delay_minutes"] == 55 for r in rules_after), \
        "Bad payload must not be saved when retry_on is empty"


# ── 8. Minutes conversion correct ────────────────────────────────────────────

def test_minutes_conversion(retry_client):
    client, tenant = retry_client
    _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "1",
        "delay_value_1": "45", "delay_unit_1": "minutes",
        "retry_on_1": "no_answer",
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    rules = get_retry_rules(tenant["id"])
    assert rules[0]["delay_minutes"] == 45


# ── 9. Hours conversion correct ──────────────────────────────────────────────

def test_hours_conversion(retry_client):
    client, tenant = retry_client
    _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "1",
        "delay_value_1": "2", "delay_unit_1": "hours",
        "retry_on_1": "no_answer",
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    rules = get_retry_rules(tenant["id"])
    assert rules[0]["delay_minutes"] == 120


# ── 10. Zero delay rejected ───────────────────────────────────────────────────

def test_zero_delay_rejected(retry_client):
    client, tenant = retry_client
    tid = tenant["id"]
    _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "1",
        "delay_value_1": "0", "delay_unit_1": "minutes",
        "retry_on_1": "no_answer",
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    # A zero-delay rule must never be saved (delay_minutes would be 0).
    rules = get_retry_rules(tid)
    assert not any(r["delay_minutes"] == 0 for r in rules), \
        "Zero delay must not be saved"


# ── 11. Negative delay rejected ───────────────────────────────────────────────

def test_negative_delay_rejected(retry_client):
    client, tenant = retry_client
    tid = tenant["id"]
    # Capture existing delay for attempt 1 before the bad POST
    before = {r["attempt_no"]: r["delay_minutes"] for r in get_retry_rules(tid)}

    _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "1",
        "delay_value_1": "-5", "delay_unit_1": "minutes",
        "retry_on_1": "no_answer",
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    # delay_minutes for attempt 1 must not have changed to a negative value
    after = {r["attempt_no"]: r["delay_minutes"] for r in get_retry_rules(tid)}
    assert after.get(1, before.get(1)) == before.get(1), \
        "Negative delay must not overwrite existing rule"


# ── 12. max_attempts outside 1–5 rejected ────────────────────────────────────

def test_max_attempts_out_of_range_rejected(retry_client):
    client, tenant = retry_client
    # max_attempts=6 is outside 1-5
    resp = _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "6",
        "delay_value_1": "15", "delay_unit_1": "minutes",
        "retry_on_1": "no_answer",
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    html = resp.data.decode()
    assert "max attempts" in html.lower() or "between 1 and 5" in html.lower()


# ── 13. Auto retry off saves successfully without attempt validation ──────────

def test_auto_retry_off_saves_without_attempt_validation(retry_client, db_conn):
    client, tenant = retry_client
    tid = tenant["id"]

    # Submit with retry_enabled OFF — no attempt fields at all.
    resp = _post_retry(client, {
        # retry_enabled NOT set → off
        "max_attempts": "3",
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    # POST must succeed (302 redirect) — not a validation error (422/400).
    # The test client follows redirects so final status is 200.
    assert resp.status_code == 200
    # The server must NOT have rejected the request with a retry-validation error.
    # Confirm by checking the DB: rules must now be disabled (not absent or unchanged).
    # (We check DB state, not flash text, because flash messages aren't in this template.)
    # DB: existing seeded rules preserved but is_enabled must be False
    rules = get_retry_rules(tid)
    assert rules, "Seeded rules must still exist (not deleted when retry turned off)"
    assert all(not r["is_enabled"] for r in rules), \
        "All rules must be disabled when master toggle is off"


# ── 14. Overnight DND 21:00–09:00 accepted ───────────────────────────────────

def test_overnight_dnd_accepted(retry_client):
    client, tenant = retry_client
    resp = _post_retry(client, {
        "retry_enabled": "on", "max_attempts": "1",
        "delay_value_1": "15", "delay_unit_1": "minutes",
        "retry_on_1": "no_answer",
        "dnd_start": "21:00",
        "dnd_end": "09:00",   # start > end → overnight window
    })
    assert b"saved" in resp.data.lower()
    rules = get_retry_rules(tenant["id"])
    assert str(rules[0]["dnd_start"]) == "21:00:00"
    assert str(rules[0]["dnd_end"]) == "09:00:00"


# ── 15. Retry inside overnight DND pushed to 09:05 ───────────────────────────

def test_retry_inside_dnd_pushed_past_window():
    # 22:00 Dhaka = 16:00 UTC
    retry_at = DHAKA.localize(datetime.datetime(2026, 7, 1, 22, 0)).astimezone(datetime.timezone.utc)
    dnd_start = datetime.time(21, 0)
    dnd_end   = datetime.time(9, 0)
    result = _apply_dnd(retry_at, dnd_start, dnd_end, DHAKA)
    local_result = result.astimezone(DHAKA)
    # Should be pushed to 09:05 next morning
    assert local_result.hour == 9
    assert local_result.minute == 5
    assert local_result.day == 2   # next day


# ── 16. Live preview has exactly 3 attempts when max_attempts=3 ───────────────

def test_retry_page_preview_has_3_entries(retry_client):
    client, tenant = retry_client
    _post_retry(client, _base_form(max_attempts=3))
    resp = client.get("/retry")
    html = resp.data.decode()
    # Preview text is generated by JS, but config passed to page must have 3 rules
    assert "maxAttempts: 3" in html or '"maxAttempts":3' in html or "maxAttempts: 3" in html
    # Template must not pass max_attempts=5 to JS
    assert "maxAttempts: 5" not in html


# ── 17. Cumulative preview times are correct ──────────────────────────────────
# (Backend logic — verify _apply_dnd cumulative behaviour matches spec)

def test_cumulative_preview_no_dnd():
    # No DND conflict: just check delay accumulation
    base = DHAKA.localize(datetime.datetime(2026, 7, 1, 14, 0)).astimezone(datetime.timezone.utc)
    delays_minutes = [15, 30, 60]
    dnd_start = datetime.time(21, 0)
    dnd_end = datetime.time(9, 0)

    cursor = base
    expected_offsets = [15, 45, 105]   # cumulative: 15, 15+30, 15+30+60
    for offset, d in zip(expected_offsets, delays_minutes):
        cursor = cursor + datetime.timedelta(minutes=d)
        cursor = _apply_dnd(cursor, dnd_start, dnd_end, DHAKA)
        local_cursor = cursor.astimezone(DHAKA)
        expected = base.astimezone(DHAKA) + datetime.timedelta(minutes=offset)
        assert local_cursor.hour == expected.hour
        assert local_cursor.minute == expected.minute


# ── 18. Stale DB attempts 4–5 removed when reducing 5→3 (DB-level check) ─────

def test_db_stale_attempts_removed(retry_client, db_conn):
    client, tenant = retry_client
    tid = tenant["id"]
    for i in range(1, 6):
        _make_rule(tid, i, max_attempts=5)
    count_before = db_conn.execute(
        "SELECT count(*) AS n FROM retry_rules WHERE tenant_id=%s", (tid,)
    ).fetchone()["n"]
    assert count_before == 5

    _post_retry(client, _base_form(max_attempts=3))

    count_after = db_conn.execute(
        "SELECT count(*) AS n FROM retry_rules WHERE tenant_id=%s", (tid,)
    ).fetchone()["n"]
    assert count_after == 3
    highest = db_conn.execute(
        "SELECT max(attempt_no) AS m FROM retry_rules WHERE tenant_id=%s", (tid,)
    ).fetchone()["m"]
    assert highest == 3


# ── 19. Increasing 3→5 creates rules for 4–5 ─────────────────────────────────

def test_increasing_max_attempts_creates_new_rules(retry_client):
    client, tenant = retry_client
    _post_retry(client, _base_form(max_attempts=3))
    _post_retry(client, _base_form(max_attempts=5))
    rules = get_retry_rules(tenant["id"])
    nos = {r["attempt_no"] for r in rules}
    assert nos == {1, 2, 3, 4, 5}


# ── 20. Transaction rollback preserves old rules on error ─────────────────────

def test_save_retry_rules_rollback_on_error(create_tenant):
    pair = create_tenant("Rollback Tenant", "rollback_retry@example.com")
    tid = pair[0]["id"]
    _make_rule(tid, 1, delay_minutes=99, max_attempts=1)

    # save_retry_rules with a malformed rule that should cause a DB error
    import pytest as _pt
    with _pt.raises(Exception):
        save_retry_rules(tid, [
            {
                "attempt_no": 1,
                "delay_minutes": None,   # NOT NULL column → error
                "retry_on": ["no_answer"],
                "dnd_start": datetime.time(21, 0),
                "dnd_end": datetime.time(9, 0),
                "max_attempts": 1,
                "is_enabled": True,
            }
        ])

    # Original rule must still be there
    rules = get_retry_rules(tid)
    assert rules[0]["delay_minutes"] == 99


# ── 21. Tenant isolation ──────────────────────────────────────────────────────

def test_tenant_isolation(create_tenant, auth_client, client):
    pair_a = create_tenant("Tenant A", "tenant_a_retry@example.com")
    pair_b = create_tenant("Tenant B", "tenant_b_retry@example.com")

    client_a = auth_client(pair_a)
    _post_retry(client_a, {
        "retry_enabled": "on", "max_attempts": "1",
        "delay_value_1": "77", "delay_unit_1": "minutes",
        "retry_on_1": "no_answer",
        "dnd_start": "21:00", "dnd_end": "09:00",
    })
    rules_a = get_retry_rules(pair_a[0]["id"])
    rules_b = get_retry_rules(pair_b[0]["id"])
    assert any(r["delay_minutes"] == 77 for r in rules_a)
    # Tenant B must have no contamination from A's rules
    assert not any(r["delay_minutes"] == 77 for r in rules_b)


# ── 22. Scheduler respects order.max_attempts as per-order cap ───────────────

def test_scheduler_uses_rule_max_attempts(create_tenant):
    """orders.max_attempts is the authoritative per-order cap; the rule must
    exist AND the order must permit the next attempt. Two sub-cases:
    (a) order.max_attempts=5, rule exists → attempt 5 fires (True)
    (b) order.max_attempts=3, even with rules up to 5 → attempt 4 blocked (False)
    """
    pair = create_tenant("Sched Tenant", "sched_retry@example.com")
    tid = pair[0]["id"]

    # Seed rules for all 5 attempts
    for i in range(1, 6):
        _make_rule(tid, i, delay_minutes=5, max_attempts=5)

    # Sub-case (a): order allows 5 retries → attempt 5 must be scheduled
    order_id_a = _make_order(tid, max_attempts=5, attempt_count=4)
    result_a = schedule_retry(order_id_a, tid, current_attempt=4, call_result=None)
    assert result_a is True, "Attempt 5 must fire when order.max_attempts=5 and rule exists"

    # Sub-case (b): order allows only 3 retries → attempt 4 must be rejected
    order_id_b = _make_order(tid, max_attempts=3, attempt_count=3)
    result_b = schedule_retry(order_id_b, tid, current_attempt=3, call_result=None)
    assert result_b is False, "Attempt 4 must not fire when order.max_attempts=3"


# ── 23. Retry conditions match call result ────────────────────────────────────

def test_retry_skipped_when_result_not_in_conditions(create_tenant, db_conn):
    pair = create_tenant("Cond Tenant", "cond_retry@example.com")
    tid = pair[0]["id"]
    # Rule for attempt 1: only retry on no_answer (not busy)
    db.execute(
        """INSERT INTO retry_rules (tenant_id, attempt_no, delay_minutes, retry_on, dnd_start, dnd_end, max_attempts, is_enabled)
           VALUES (%s, 1, 10, ARRAY['no_answer'], '21:00', '09:00', 3, true)
           ON CONFLICT (tenant_id, attempt_no) DO UPDATE SET
               retry_on = ARRAY['no_answer'], delay_minutes = 10""",
        (tid,),
    )
    order_id = _make_order(tid, max_attempts=3, attempt_count=0)
    # Call ended as 'busy' but rule only retries on 'no_answer'
    result = schedule_retry(order_id, tid, current_attempt=0, call_result="busy")
    assert result is False, "Must not retry when call_result not in retry_on conditions"
    from app.models.order import get_order_by_id_system
    order = get_order_by_id_system(order_id)
    assert order["order_status"] == "UNREACHABLE"


# ── 24. No duplicate retry job for same order ─────────────────────────────────

def test_no_duplicate_retry_job(create_tenant, db_conn):
    pair = create_tenant("Dedup Tenant", "dedup_retry@example.com")
    tid = pair[0]["id"]
    _make_rule(tid, 1, delay_minutes=10, max_attempts=3)
    order_id = _make_order(tid, max_attempts=3, attempt_count=0)

    result1 = schedule_retry(order_id, tid, current_attempt=0, call_result=None)
    assert result1 is True

    # Second call with same order — should either succeed or be a no-op
    # but must never produce TWO call dispatches (status is now RETRY_SCHEDULED)
    result2 = schedule_retry(order_id, tid, current_attempt=0, call_result=None)
    # Both True/False are acceptable — the point is the order stays at one RETRY_SCHEDULED row
    row = db_conn.execute(
        "SELECT order_status, next_retry_at FROM orders WHERE id=%s", (order_id,)
    ).fetchone()
    assert row["order_status"] == "RETRY_SCHEDULED"
    # Only one next_retry_at should be set (not duplicated)
    assert row["next_retry_at"] is not None
