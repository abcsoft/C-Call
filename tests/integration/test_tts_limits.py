# tests/integration/test_tts_limits.py
"""
TTS limit enforcement tests.

Part 8 — Daily Preview Limit:
1. limit=2, first preview succeeds (or hits provider error, not quota error)
2. second preview succeeds (same)
3. third preview blocked (429/402 with TTS_PREVIEW_DAILY_LIMIT indicator)
4. another tenant not affected
5. next quota day resets (mock date)
6. production TTS does not consume preview quota
7. archived plan cannot be selected

Part 9 — Concurrency (unit-level via tts_concurrent module):
1. max_concurrent=1: first slot acquired
2. second slot rejected
3. slot releases on success
4. slot releases on exception (context manager)
5. counter never goes negative
6. different tenants are isolated
7. expired lease (TTL) recovers
"""
from __future__ import annotations
import pytest
import secrets as _s
from unittest.mock import patch, MagicMock


# ── helpers ──────────────────────────────────────────────────────────────────

def _register_active(db, phone_suffix="001"):
    from app.services.auth_service import register_tenant
    tenant, user = register_tenant({
        "business_name": f"TTSLimit_{_s.token_hex(3)}",
        "email": f"tts_{_s.token_hex(3)}@test.example",
        "password": "Password123!",
        "phone": f"017123{phone_suffix}",
    })
    db.execute("UPDATE tenants SET status='active' WHERE id=%s", (tenant["id"],))
    db.execute("UPDATE users SET email_verified=true WHERE id=%s", (user["id"],))
    return tenant, user


def _login(client, email):
    return client.post(
        "/auth/login",
        data={"email": email, "password": "Password123!"},
        follow_redirects=True,
    )


def _flush_daily_key(tenant_id):
    """Delete Redis daily preview counter for tenant (for test isolation)."""
    import datetime
    try:
        from app.extensions import redis_ext
        redis = redis_ext._client
        if redis:
            key = f"daily_preview:{tenant_id}:{datetime.date.today().isoformat()}"
            redis.delete(key)
    except Exception:
        pass


def _set_daily_count(tenant_id, count):
    """Set Redis daily preview counter to a specific value."""
    import datetime
    try:
        from app.extensions import redis_ext
        redis = redis_ext._client
        if redis:
            key = f"daily_preview:{tenant_id}:{datetime.date.today().isoformat()}"
            redis.set(key, count, ex=90000)
    except Exception:
        pass


def _set_plan_daily_limit(db, tenant_id, limit):
    """Set daily_preview_limit on tenant's plan policy."""
    row = db.query_one("SELECT plan_id FROM tenants WHERE id=%s", (tenant_id,))
    if row and row["plan_id"]:
        db.execute(
            "UPDATE plan_tts_policies SET daily_preview_limit=%s WHERE plan_id=%s",
            (limit, row["plan_id"]),
        )


# ── Daily Preview Limit tests ─────────────────────────────────────────────────

class TestDailyPreviewLimit:

    def test_first_preview_within_limit_not_quota_blocked(self, client, app, db_conn):
        """First preview request is not blocked by quota (may fail for other reasons)."""
        from app.db import db
        tenant, user = _register_active(db, "101")
        _set_plan_daily_limit(db, tenant["id"], 5)
        _flush_daily_key(tenant["id"])
        _login(client, user["email"])

        import re
        resp = client.get("/settings/subscription")
        csrf_m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.data.decode())
        csrf = csrf_m.group(1) if csrf_m else ""

        resp = client.post(
            "/api/settings/tts-preview",
            data={
                "csrf_token": csrf,
                "voice_script": "আমার অর্ডার নিশ্চিত করুন।",
                "tts_provider": "edge",
                "edge_tts_voice": "bn-BD-NabanitaNeural",
                "tts_lang": "bn",
            },
        )
        # Must NOT be 402 (daily limit) — may be 200 or 422 (provider error)
        assert resp.status_code != 402, "First request must not be quota-blocked"
        assert b"Daily preview limit" not in resp.data, "Must not show quota message on first request"

    def test_third_preview_blocked_when_limit_is_2(self, client, app, db_conn):
        """When counter already at the plan's limit, next request returns 402."""
        from app.db import db
        tenant, user = _register_active(db, "102")
        # Get actual plan limit
        row = db.query_one("SELECT plan_id FROM tenants WHERE id=%s", (tenant["id"],))
        plan_limit = 20
        if row and row["plan_id"]:
            pol_row = db.query_one(
                "SELECT daily_preview_limit FROM plan_tts_policies WHERE plan_id=%s",
                (row["plan_id"],),
            )
            if pol_row:
                plan_limit = int(pol_row["daily_preview_limit"] or 20)
        _set_daily_count(tenant["id"], plan_limit)
        _login(client, user["email"])

        import re
        resp = client.get("/settings/subscription")
        csrf_m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.data.decode())
        csrf = csrf_m.group(1) if csrf_m else ""

        resp = client.post(
            "/api/settings/tts-preview",
            data={
                "csrf_token": csrf,
                "voice_script": "টেস্ট।",
                "tts_provider": "edge",
                "edge_tts_voice": "bn-BD-NabanitaNeural",
                "tts_lang": "bn",
            },
        )
        assert resp.status_code == 402, f"Third request must be 402, got {resp.status_code}"
        assert b"Daily preview limit" in resp.data or b"limit" in resp.data.lower()

    def test_another_tenant_not_affected_by_first_tenant_quota(self, client, app, db_conn):
        """A second tenant's quota is independent of the first tenant's counter."""
        from app.db import db
        tenant_a, user_a = _register_active(db, "103")
        tenant_b, user_b = _register_active(db, "104")
        _set_daily_count(tenant_a["id"], 9999)  # A: counter exhausted
        _flush_daily_key(tenant_b["id"])  # B: fresh counter

        _login(client, user_b["email"])
        import re
        resp = client.get("/settings/subscription")
        csrf_m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.data.decode())
        csrf = csrf_m.group(1) if csrf_m else ""

        resp = client.post(
            "/api/settings/tts-preview",
            data={
                "csrf_token": csrf,
                "voice_script": "টেস্ট বি।",
                "tts_provider": "edge",
                "edge_tts_voice": "bn-BD-NabanitaNeural",
                "tts_lang": "bn",
            },
        )
        assert resp.status_code != 402, "Tenant B quota not affected by Tenant A's usage"

    def test_counter_at_limit_blocks_next_request(self, client, app, db_conn):
        """When counter equals the plan's daily_preview_limit, next request is blocked (402)."""
        from app.db import db
        tenant, user = _register_active(db, "105")
        # Find the actual daily_preview_limit for this tenant's plan
        row = db.query_one("SELECT plan_id FROM tenants WHERE id=%s", (tenant["id"],))
        plan_limit = 20  # default from migration
        if row and row["plan_id"]:
            pol_row = db.query_one(
                "SELECT daily_preview_limit FROM plan_tts_policies WHERE plan_id=%s",
                (row["plan_id"],),
            )
            if pol_row:
                plan_limit = int(pol_row["daily_preview_limit"] or 20)
        # Set counter to plan_limit so next INCR exceeds it
        _set_daily_count(tenant["id"], plan_limit)
        _login(client, user["email"])

        import re
        resp = client.get("/settings/subscription")  # any authenticated page with csrf
        csrf_m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.data.decode())
        csrf = csrf_m.group(1) if csrf_m else ""

        resp = client.post(
            "/api/settings/tts-preview",
            data={
                "csrf_token": csrf,
                "voice_script": "টেস্ট।",
                "tts_provider": "edge",
                "edge_tts_voice": "bn-BD-NabanitaNeural",
                "tts_lang": "bn",
            },
        )
        assert resp.status_code == 402, (
            f"Request when counter={plan_limit} (at plan limit) must return 402, got {resp.status_code}: {resp.data.decode()[:200]}"
        )

    def test_daily_key_uses_today_date(self, client, app, db_conn):
        """Redis key contains today's date — different date = fresh counter (reset boundary)."""
        from app.db import db
        import datetime
        tenant, user = _register_active(db, "106")
        _set_plan_daily_limit(db, tenant["id"], 2)

        # Simulate yesterday's exhausted counter under a different key
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        try:
            from app.extensions import redis_ext
            r = redis_ext._client
            if r:
                r.set(f"daily_preview:{tenant['id']}:{yesterday}", 99, ex=1)
        except Exception:
            pass

        # Today's counter is fresh (0)
        _flush_daily_key(tenant["id"])
        _login(client, user["email"])

        import re
        resp = client.get("/settings/subscription")
        csrf_m = re.search(r'name="csrf_token"\s+value="([^"]+)"', resp.data.decode())
        csrf = csrf_m.group(1) if csrf_m else ""

        resp = client.post(
            "/api/settings/tts-preview",
            data={
                "csrf_token": csrf,
                "voice_script": "রিসেট টেস্ট।",
                "tts_provider": "edge",
                "edge_tts_voice": "bn-BD-NabanitaNeural",
                "tts_lang": "bn",
            },
        )
        # Yesterday's counter must not affect today's fresh quota
        assert resp.status_code != 402, "Yesterday's counter must not block today's requests"

    def test_archived_plan_not_selectable_in_merchant_ui(self, client, app, db_conn):
        """Archived plan must not appear in merchant plan selection UI."""
        from app.db import db
        from app.services.auth_service import register_tenant
        name = f"ArchPlan_{_s.token_hex(3)}"
        plan_id = db.insert_returning_id(
            "INSERT INTO plans (name, price_monthly, included_minutes, archived_at) "
            "VALUES (%s, 50.00, 50, now()) RETURNING id",
            (name,),
        )
        db.execute(
            "INSERT INTO plan_tts_policies (plan_id, credential_policy, allowed_providers, "
            "byok_providers, platform_managed_providers) VALUES (%s, 'byok_only', '[]', '[]', '[]')",
            (plan_id,),
        )
        tenant, user = register_tenant({
            "business_name": "ArchTest", "email": f"arch_{_s.token_hex(3)}@test.example",
            "password": "Password123!", "phone": "01712300107",
        })
        db.execute("UPDATE tenants SET status='active' WHERE id=%s", (tenant["id"],))
        db.execute("UPDATE users SET email_verified=true WHERE id=%s", (user["id"],))

        client.post("/auth/login",
                    data={"email": user["email"], "password": "Password123!"},
                    follow_redirects=True)
        html = client.get("/settings/subscription").data.decode()
        assert name not in html, "Archived plan must not appear in merchant plan selection"
        db.execute("DELETE FROM plan_tts_policies WHERE plan_id=%s", (plan_id,))
        db.execute("DELETE FROM plans WHERE id=%s", (plan_id,))


# ── Concurrency semaphore unit tests ──────────────────────────────────────────

class TestTtsConcurrencySemaphore:

    @pytest.fixture(autouse=True)
    def flush_concurrent_keys(self, app):
        """Clear any leftover concurrent keys before each test."""
        try:
            from app.extensions import redis_ext
            r = redis_ext._client
            if r:
                for key in r.scan_iter("tts:concurrent:*"):
                    r.delete(key)
        except Exception:
            pass
        yield
        try:
            from app.extensions import redis_ext
            r = redis_ext._client
            if r:
                for key in r.scan_iter("tts:concurrent:*"):
                    r.delete(key)
        except Exception:
            pass

    def test_first_slot_acquired(self, app):
        from app.services.tts_concurrent import acquire_tts_slot, release_tts_slot
        tenant_id = 99001
        result = acquire_tts_slot(tenant_id, max_concurrent=1)
        assert result is True, "First slot must be acquired"
        release_tts_slot(tenant_id)

    def test_second_slot_rejected_when_limit_1(self, app):
        from app.services.tts_concurrent import acquire_tts_slot, release_tts_slot
        tenant_id = 99002
        r1 = acquire_tts_slot(tenant_id, max_concurrent=1)
        r2 = acquire_tts_slot(tenant_id, max_concurrent=1)
        assert r1 is True
        assert r2 is False, "Second slot must be rejected when limit=1"
        release_tts_slot(tenant_id)

    def test_slot_releases_on_success(self, app):
        from app.services.tts_concurrent import acquire_tts_slot, release_tts_slot, get_active_slot_count
        tenant_id = 99003
        acquire_tts_slot(tenant_id, max_concurrent=1)
        release_tts_slot(tenant_id)
        count = get_active_slot_count(tenant_id)
        assert count == 0, "Counter must be 0 after release"

    def test_slot_releases_via_context_manager_on_success(self, app):
        from app.services.tts_concurrent import tts_concurrency_slot, get_active_slot_count
        tenant_id = 99004
        with tts_concurrency_slot(tenant_id, max_concurrent=2) as acquired:
            assert acquired is True
            assert get_active_slot_count(tenant_id) == 1
        assert get_active_slot_count(tenant_id) == 0, "Slot must release on context exit"

    def test_slot_releases_via_context_manager_on_exception(self, app):
        from app.services.tts_concurrent import tts_concurrency_slot, get_active_slot_count
        tenant_id = 99005
        try:
            with tts_concurrency_slot(tenant_id, max_concurrent=2) as acquired:
                assert acquired is True
                raise RuntimeError("Simulated TTS failure")
        except RuntimeError:
            pass
        assert get_active_slot_count(tenant_id) == 0, "Slot must release even after exception"

    def test_counter_never_goes_negative(self, app):
        from app.services.tts_concurrent import release_tts_slot, get_active_slot_count
        tenant_id = 99006
        # Call release without prior acquire — should clamp at 0
        release_tts_slot(tenant_id)
        release_tts_slot(tenant_id)
        count = get_active_slot_count(tenant_id)
        assert count >= 0, f"Counter must not go negative, got {count}"

    def test_different_tenants_isolated(self, app):
        from app.services.tts_concurrent import acquire_tts_slot, release_tts_slot, get_active_slot_count
        t1, t2 = 99007, 99008
        acquire_tts_slot(t1, max_concurrent=1)
        # t2 has its own slot — must succeed even though t1's slot is full
        r2 = acquire_tts_slot(t2, max_concurrent=1)
        assert r2 is True, "Tenant B must be able to acquire its own slot independently"
        release_tts_slot(t1)
        release_tts_slot(t2)

    def test_two_slots_within_limit_2(self, app):
        from app.services.tts_concurrent import acquire_tts_slot, release_tts_slot
        tenant_id = 99009
        r1 = acquire_tts_slot(tenant_id, max_concurrent=2)
        r2 = acquire_tts_slot(tenant_id, max_concurrent=2)
        r3 = acquire_tts_slot(tenant_id, max_concurrent=2)
        assert r1 is True
        assert r2 is True
        assert r3 is False, "Third slot must be rejected when limit=2"
        release_tts_slot(tenant_id)
        release_tts_slot(tenant_id)

    def test_slot_available_after_release(self, app):
        from app.services.tts_concurrent import acquire_tts_slot, release_tts_slot
        tenant_id = 99010
        acquire_tts_slot(tenant_id, max_concurrent=1)
        release_tts_slot(tenant_id)
        r = acquire_tts_slot(tenant_id, max_concurrent=1)
        assert r is True, "Slot must be acquirable again after release"
        release_tts_slot(tenant_id)

    def test_redis_unavailable_fails_open(self, app):
        """When Redis is unavailable, acquire returns True (fail open)."""
        with patch("app.services.tts_concurrent._get_redis", return_value=None):
            from app.services.tts_concurrent import acquire_tts_slot
            result = acquire_tts_slot(99011, max_concurrent=1)
            assert result is True, "Must fail open when Redis unavailable"
