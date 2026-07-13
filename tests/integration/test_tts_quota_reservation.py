# tests/integration/test_tts_quota_reservation.py
"""Integration tests for TTS quota reservation, billing, and idempotency.

Phase 14+15: automated + concurrency tests.

Coverage:
- reserve_tts_usage: BYOK passthrough, platform_managed quota split,
  block policy enforcement, charge_wallet overage debit, idempotency
- commit_tts_usage: UPSERT correct columns, refund surplus overage
- release_tts_usage: refund estimated overage, idempotent
- expire_stale_tts_reservations: marks expired, refunds wallet
- calculate_tts_billable_characters: edge cases
- Daily preview counter: increment before call, DB fallback
"""
from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.db import db
from app.exceptions import InsufficientBalanceError, TTSQuotaExceededError
from app.services.tts_usage_service import (
    calculate_tts_billable_characters,
    commit_tts_usage,
    expire_stale_tts_reservations,
    make_preview_idempotency_key,
    make_production_idempotency_key,
    release_tts_usage,
    reserve_tts_usage,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _block_policy(quota: int | None = 10_000) -> dict:
    return {
        "monthly_character_quota": quota,
        "extra_usage_policy": "block",
        "overage_rate_per_1000_chars": Decimal("0"),
        "minimum_overage_charge": Decimal("0"),
        "allow_preview_overage": False,
    }


def _charge_wallet_policy(
    quota: int = 10_000,
    rate: str = "0.5",
    minimum: str = "0",
    allow_preview_overage: bool = False,
) -> dict:
    return {
        "monthly_character_quota": quota,
        "extra_usage_policy": "charge_wallet",
        "overage_rate_per_1000_chars": Decimal(rate),
        "minimum_overage_charge": Decimal(minimum),
        "allow_preview_overage": allow_preview_overage,
    }


def _make_tenant_with_wallet(balance: Decimal = Decimal("100")) -> int:
    tid = db.insert_returning_id(
        "INSERT INTO tenants (business_name, status) VALUES (%s, 'active') RETURNING id",
        (f"Quota Test {uuid.uuid4().hex[:6]}",),
    )
    db.execute(
        "INSERT INTO wallets (tenant_id, balance) VALUES (%s, %s) ON CONFLICT (tenant_id) DO UPDATE SET balance = %s",
        (tid, balance, balance),
    )
    return tid


def _today_month() -> date:
    now = datetime.now(timezone.utc)
    return date(now.year, now.month, 1)


# ── BYOK / free provider passthrough ─────────────────────────────────────────

class TestByokPassthrough:
    def test_byok_edge_creates_reservation_with_zero_overage(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("BYOK Edge", "byok_edge@t.com")
            tid = t["id"]
            key = make_preview_idempotency_key()
            res = reserve_tts_usage(
                tid, "edge", "byok", "preview", key, 500,
                plan_policy=_block_policy(10_000),
            )
            assert res["status"] == "reserved"
            assert int(res["overage_characters_reserved"]) == 0
            assert Decimal(str(res["estimated_overage_amount"])) == Decimal("0")

    def test_byok_gemini_creates_reservation_no_charge(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("BYOK Gemini", "byok_gem@t.com")
            tid = t["id"]
            key = make_production_idempotency_key(tid, "WC-1", "h1", "gemini", "production", 1)
            res = reserve_tts_usage(
                tid, "gemini", "byok", "production", key, 200,
                plan_policy=_block_policy(10_000),
            )
            assert res["status"] == "reserved"
            assert Decimal(str(res["estimated_overage_amount"])) == Decimal("0")
            # No wallet deduction for BYOK
            wrow = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tid,))
            # wallet starts at 0 (no wallet insert), so just verify no error
            assert res["wallet_transaction_id"] is None


# ── Block policy ──────────────────────────────────────────────────────────────

class TestBlockPolicy:
    def test_quota_available_allows_reservation(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Block Allow", "block_allow@t.com")
            tid = t["id"]
            key = make_preview_idempotency_key()
            res = reserve_tts_usage(
                tid, "gemini", "platform_managed", "preview", key, 100,
                plan_policy=_block_policy(10_000),
            )
            assert res["status"] == "reserved"
            assert int(res["quota_characters_reserved"]) == 100
            assert int(res["overage_characters_reserved"]) == 0

    def test_quota_exhausted_raises_quota_exceeded(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Block Deny", "block_deny@t.com")
            tid = t["id"]
            billing_month = _today_month()
            # Pre-fill committed usage equal to full quota
            db.execute(
                """
                INSERT INTO tenant_tts_usage
                    (tenant_id, provider, credential_mode, billing_month, characters_used)
                VALUES (%s, 'gemini', 'platform_managed', %s, 10000)
                ON CONFLICT (tenant_id, provider, credential_mode, billing_month)
                DO UPDATE SET characters_used = 10000
                """,
                (tid, billing_month),
            )
            # Also fill reservations so the "reserved" estimate sees full usage
            key1 = make_preview_idempotency_key()
            db.execute(
                """
                INSERT INTO tts_usage_reservations
                    (tenant_id, provider, credential_mode, purpose, idempotency_key,
                     billing_month, characters_reserved, quota_characters_reserved,
                     overage_characters_reserved, estimated_overage_amount, status)
                VALUES (%s, 'gemini', 'platform_managed', 'preview', %s, %s, 10000, 10000, 0, 0, 'committed')
                """,
                (tid, key1, billing_month),
            )
            key2 = make_preview_idempotency_key()
            with pytest.raises(TTSQuotaExceededError):
                reserve_tts_usage(
                    tid, "gemini", "platform_managed", "preview", key2, 1,
                    plan_policy=_block_policy(10_000),
                )

    def test_preview_overage_blocked_by_default(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Preview Block", "prev_block@t.com")
            tid = t["id"]
            billing_month = _today_month()
            # Fill quota
            db.execute(
                """
                INSERT INTO tts_usage_reservations
                    (tenant_id, provider, credential_mode, purpose, idempotency_key,
                     billing_month, characters_reserved, quota_characters_reserved,
                     overage_characters_reserved, estimated_overage_amount, status)
                VALUES (%s, 'gemini', 'platform_managed', 'preview', %s, %s, 10000, 10000, 0, 0, 'committed')
                """,
                (tid, make_preview_idempotency_key(), billing_month),
            )
            key = make_preview_idempotency_key()
            with pytest.raises(TTSQuotaExceededError):
                reserve_tts_usage(
                    tid, "gemini", "platform_managed", "preview", key, 100,
                    plan_policy=_charge_wallet_policy(10_000, allow_preview_overage=False),
                )


# ── charge_wallet policy ──────────────────────────────────────────────────────

class TestChargeWalletPolicy:
    def test_overage_deducted_from_wallet(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("CW Overage", "cw_overage@t.com")
            tid = t["id"]
            # Wallet with balance
            db.execute(
                "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 50) ON CONFLICT (tenant_id) DO UPDATE SET balance = 50",
                (tid,),
            )
            billing_month = _today_month()
            # Fill 9500 of 10000 quota via existing committed reservation
            db.execute(
                """
                INSERT INTO tts_usage_reservations
                    (tenant_id, provider, credential_mode, purpose, idempotency_key,
                     billing_month, characters_reserved, quota_characters_reserved,
                     overage_characters_reserved, estimated_overage_amount, status)
                VALUES (%s, 'gemini', 'platform_managed', 'production', %s, %s, 9500, 9500, 0, 0, 'committed')
                """,
                (tid, make_preview_idempotency_key(), billing_month),
            )
            key = make_production_idempotency_key(tid, "WC-1", "h", "gemini", "production", 1)
            # Request 1500 chars: 500 from quota, 1000 overage → 1 × 0.5 = 0.5 BDT
            res = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1500,
                plan_policy=_charge_wallet_policy(10_000, rate="0.5"),
            )
            assert int(res["quota_characters_reserved"]) == 500
            assert int(res["overage_characters_reserved"]) == 1000
            assert Decimal(str(res["estimated_overage_amount"])) == Decimal("0.5")
            assert res["wallet_transaction_id"] is not None
            # Wallet debited
            wrow = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tid,))
            assert Decimal(str(wrow["balance"])) == Decimal("49.5")

    def test_insufficient_wallet_raises_error(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("CW Insufficient", "cw_insuf@t.com")
            tid = t["id"]
            db.execute(
                "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 0) ON CONFLICT (tenant_id) DO UPDATE SET balance = 0",
                (tid,),
            )
            billing_month = _today_month()
            db.execute(
                """
                INSERT INTO tts_usage_reservations
                    (tenant_id, provider, credential_mode, purpose, idempotency_key,
                     billing_month, characters_reserved, quota_characters_reserved,
                     overage_characters_reserved, estimated_overage_amount, status)
                VALUES (%s, 'gemini', 'platform_managed', 'production', %s, %s, 10000, 10000, 0, 0, 'committed')
                """,
                (tid, make_preview_idempotency_key(), billing_month),
            )
            key = make_production_idempotency_key(tid, "WC-2", "h", "gemini", "production", 1)
            with pytest.raises(InsufficientBalanceError):
                reserve_tts_usage(
                    tid, "gemini", "platform_managed", "production", key, 1000,
                    plan_policy=_charge_wallet_policy(10_000, rate="0.5"),
                )

    def test_preview_overage_allowed_when_flag_set(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("CW Preview OK", "cw_prev_ok@t.com")
            tid = t["id"]
            db.execute(
                "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 10) ON CONFLICT (tenant_id) DO UPDATE SET balance = 10",
                (tid,),
            )
            billing_month = _today_month()
            db.execute(
                """
                INSERT INTO tts_usage_reservations
                    (tenant_id, provider, credential_mode, purpose, idempotency_key,
                     billing_month, characters_reserved, quota_characters_reserved,
                     overage_characters_reserved, estimated_overage_amount, status)
                VALUES (%s, 'gemini', 'platform_managed', 'preview', %s, %s, 10000, 10000, 0, 0, 'committed')
                """,
                (tid, make_preview_idempotency_key(), billing_month),
            )
            key = make_preview_idempotency_key()
            res = reserve_tts_usage(
                tid, "gemini", "platform_managed", "preview", key, 500,
                plan_policy=_charge_wallet_policy(10_000, rate="0.1", allow_preview_overage=True),
            )
            assert res["status"] == "reserved"
            assert int(res["overage_characters_reserved"]) == 500


# ── Idempotency ───────────────────────────────────────────────────────────────

class TestIdempotency:
    def test_duplicate_reservation_key_returns_existing(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Idem Dup", "idem_dup@t.com")
            tid = t["id"]
            key = make_preview_idempotency_key()
            r1 = reserve_tts_usage(
                tid, "edge", "byok", "preview", key, 100,
                plan_policy=_block_policy(),
            )
            r2 = reserve_tts_usage(
                tid, "edge", "byok", "preview", key, 200,  # different chars
                plan_policy=_block_policy(),
            )
            # Second call returns same row — characters_reserved is from first
            assert r1["id"] == r2["id"]
            assert int(r2["characters_reserved"]) == 100


# ── commit_tts_usage ──────────────────────────────────────────────────────────

class TestCommitTtsUsage:
    def test_commit_updates_reservation_status(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Commit Test", "commit@t.com")
            tid = t["id"]
            key = make_preview_idempotency_key()
            res = reserve_tts_usage(
                tid, "edge", "byok", "preview", key, 300,
                plan_policy=_block_policy(),
            )
            commit_tts_usage(res["id"], tid, actual_characters=300, actual_audio_seconds=5.0)
            row = db.query_one(
                "SELECT status FROM tts_usage_reservations WHERE id = %s", (res["id"],)
            )
            assert row["status"] == "committed"

    def test_commit_upserts_tenant_tts_usage(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Commit Usage", "commit_usage@t.com")
            tid = t["id"]
            key = make_production_idempotency_key(tid, "WC-50", "x", "edge", "production", 1)
            res = reserve_tts_usage(
                tid, "edge", "byok", "production", key, 400,
                plan_policy=_block_policy(),
            )
            commit_tts_usage(res["id"], tid, actual_characters=400, actual_audio_seconds=8.0)
            usage = db.query_one(
                """
                SELECT characters_used, production_requests FROM tenant_tts_usage
                WHERE tenant_id = %s AND provider = 'edge'
                """,
                (tid,),
            )
            assert usage is not None
            assert int(usage["characters_used"]) >= 400
            assert int(usage["production_requests"]) >= 1

    def test_commit_idempotent_on_double_call(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Commit Idem", "commit_idem@t.com")
            tid = t["id"]
            key = make_preview_idempotency_key()
            res = reserve_tts_usage(
                tid, "edge", "byok", "preview", key, 100,
                plan_policy=_block_policy(),
            )
            commit_tts_usage(res["id"], tid, actual_characters=100, actual_audio_seconds=2.0)
            commit_tts_usage(res["id"], tid, actual_characters=100, actual_audio_seconds=2.0)  # second call
            usage = db.query_one(
                "SELECT preview_requests FROM tenant_tts_usage WHERE tenant_id = %s AND provider = 'edge'",
                (tid,),
            )
            assert int(usage["preview_requests"]) == 1  # not double-counted

    def test_commit_refunds_surplus_overage(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Commit Refund", "commit_refund@t.com")
            tid = t["id"]
            db.execute(
                "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 100) ON CONFLICT (tenant_id) DO UPDATE SET balance = 100",
                (tid,),
            )
            billing_month = _today_month()
            # Fill quota so overage kicks in
            db.execute(
                """
                INSERT INTO tts_usage_reservations
                    (tenant_id, provider, credential_mode, purpose, idempotency_key,
                     billing_month, characters_reserved, quota_characters_reserved,
                     overage_characters_reserved, estimated_overage_amount, status)
                VALUES (%s, 'gemini', 'platform_managed', 'production', %s, %s, 10000, 10000, 0, 0, 'committed')
                """,
                (tid, make_preview_idempotency_key(), billing_month),
            )
            key = make_production_idempotency_key(tid, "WC-99", "h", "gemini", "production", 1)
            res = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 2000,
                plan_policy=_charge_wallet_policy(10_000, rate="1.0"),
            )
            balance_after_reserve = Decimal(str(
                db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tid,))["balance"]
            ))
            # Estimated: ceil(2000/1000) × 1.0 = 2.0 BDT
            # Actual: only 1500 chars generated → overage_amount = ceil(1500/1000) × 1.0 = 2.0 still
            # Let's test with 1000 actual chars → ceil(1000/1000) × 1.0 = 1.0 → refund 1.0
            commit_tts_usage(
                res["id"], tid,
                actual_characters=1000,
                actual_audio_seconds=10.0,
                actual_overage_amount=Decimal("1.0"),
            )
            balance_after_commit = Decimal(str(
                db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tid,))["balance"]
            ))
            # Should have been refunded 1.0 (estimated 2.0 - actual 1.0)
            assert balance_after_commit == balance_after_reserve + Decimal("1.0")


# ── release_tts_usage ─────────────────────────────────────────────────────────

class TestReleaseTtsUsage:
    def test_release_marks_reservation_released(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Release Test", "release@t.com")
            tid = t["id"]
            key = make_preview_idempotency_key()
            res = reserve_tts_usage(
                tid, "edge", "byok", "preview", key, 100,
                plan_policy=_block_policy(),
            )
            release_tts_usage(res["id"], tid, error_code="PROVIDER_TIMEOUT")
            row = db.query_one(
                "SELECT status, error_code FROM tts_usage_reservations WHERE id = %s", (res["id"],)
            )
            assert row["status"] == "released"
            assert row["error_code"] == "PROVIDER_TIMEOUT"

    def test_release_refunds_overage_wallet_debit(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Release Refund", "rel_refund@t.com")
            tid = t["id"]
            db.execute(
                "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 100) ON CONFLICT (tenant_id) DO UPDATE SET balance = 100",
                (tid,),
            )
            billing_month = _today_month()
            db.execute(
                """
                INSERT INTO tts_usage_reservations
                    (tenant_id, provider, credential_mode, purpose, idempotency_key,
                     billing_month, characters_reserved, quota_characters_reserved,
                     overage_characters_reserved, estimated_overage_amount, status)
                VALUES (%s, 'gemini', 'platform_managed', 'production', %s, %s, 10000, 10000, 0, 0, 'committed')
                """,
                (tid, make_preview_idempotency_key(), billing_month),
            )
            key = make_production_idempotency_key(tid, "WC-1", "h", "gemini", "production", 1)
            res = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 2000,
                plan_policy=_charge_wallet_policy(10_000, rate="0.5"),
            )
            balance_after_reserve = Decimal(str(
                db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tid,))["balance"]
            ))
            release_tts_usage(res["id"], tid, error_code="NETWORK_ERROR")
            balance_after_release = Decimal(str(
                db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tid,))["balance"]
            ))
            assert balance_after_release > balance_after_reserve

    def test_release_idempotent(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Release Idem", "rel_idem@t.com")
            tid = t["id"]
            key = make_preview_idempotency_key()
            res = reserve_tts_usage(
                tid, "edge", "byok", "preview", key, 100,
                plan_policy=_block_policy(),
            )
            release_tts_usage(res["id"], tid)
            release_tts_usage(res["id"], tid)  # second call must not error
            row = db.query_one(
                "SELECT status FROM tts_usage_reservations WHERE id = %s", (res["id"],)
            )
            assert row["status"] == "released"


# ── expire_stale_tts_reservations ─────────────────────────────────────────────

class TestExpireStaleReservations:
    def test_expired_rows_marked_expired(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Expire Test", "expire@t.com")
            tid = t["id"]
            billing_month = _today_month()
            idem = make_preview_idempotency_key()
            # Insert a reservation that already expired
            db.execute(
                """
                INSERT INTO tts_usage_reservations
                    (tenant_id, provider, credential_mode, purpose, idempotency_key,
                     billing_month, characters_reserved, quota_characters_reserved,
                     overage_characters_reserved, estimated_overage_amount, status,
                     expires_at)
                VALUES (%s, 'gemini', 'platform_managed', 'production', %s, %s, 500, 500, 0, 0, 'reserved',
                        now() - INTERVAL '1 minute')
                """,
                (tid, idem, billing_month),
            )
            count = expire_stale_tts_reservations()
            assert count >= 1
            row = db.query_one(
                "SELECT status FROM tts_usage_reservations WHERE idempotency_key = %s", (idem,)
            )
            assert row["status"] == "expired"

    def test_future_reservations_not_expired(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Future Expire", "future_exp@t.com")
            tid = t["id"]
            billing_month = _today_month()
            idem = make_preview_idempotency_key()
            db.execute(
                """
                INSERT INTO tts_usage_reservations
                    (tenant_id, provider, credential_mode, purpose, idempotency_key,
                     billing_month, characters_reserved, quota_characters_reserved,
                     overage_characters_reserved, estimated_overage_amount, status,
                     expires_at)
                VALUES (%s, 'edge', 'byok', 'preview', %s, %s, 100, 100, 0, 0, 'reserved',
                        now() + INTERVAL '10 minutes')
                """,
                (tid, idem, billing_month),
            )
            expire_stale_tts_reservations()
            row = db.query_one(
                "SELECT status FROM tts_usage_reservations WHERE idempotency_key = %s", (idem,)
            )
            assert row["status"] == "reserved"


# ── Daily preview counter (route-level) ───────────────────────────────────────

class TestDailyPreviewCounter:
    def test_preview_increments_redis_counter_before_generation(self, auth_client, create_tenant, app):
        with app.app_context():
            t, u = create_tenant("Daily Preview", "daily_prev@t.com")
            client = auth_client((t, u))
            tenant_id = t["id"]

            import datetime
            from app.extensions import redis_ext
            daily_key = f"daily_preview:{tenant_id}:{datetime.date.today().isoformat()}"
            redis_ext._client.data.pop(daily_key, None)

            with patch("app.blueprints.customer.settings.generate_order_audio", return_value="preview-abc"):
                res = client.post("/api/settings/tts-preview", data={
                    "voice_script": "হ্যালো {customer_name}",
                    "tts_provider": "edge",
                    "edge_tts_voice": "bn-BD-NabanitaNeural",
                })
            assert res.status_code == 200
            count_after = redis_ext._client.data.get(daily_key, 0)
            assert count_after >= 1

    def test_preview_blocked_when_daily_limit_reached(self, auth_client, create_tenant, app):
        with app.app_context():
            t, u = create_tenant("Daily Limit", "daily_lim@t.com")
            client = auth_client((t, u))
            tenant_id = t["id"]

            import datetime
            from app.extensions import redis_ext
            from app.models.plan_tts_policy import get_plan_tts_policy
            from app.models.tenant import get_tenant_by_id
            _t = get_tenant_by_id(tenant_id)
            policy = get_plan_tts_policy(_t.get("plan_id"))
            daily_limit = policy.get("daily_preview_limit", 10)

            daily_key = f"daily_preview:{tenant_id}:{datetime.date.today().isoformat()}"
            # Set counter to the limit already
            redis_ext._client.data[daily_key] = daily_limit

            res = client.post("/api/settings/tts-preview", data={
                "voice_script": "הello",
                "tts_provider": "edge",
                "edge_tts_voice": "bn-BD-NabanitaNeural",
            })
            assert res.status_code == 402

    def test_preview_counter_decremented_on_infra_failure(self, auth_client, create_tenant, app):
        with app.app_context():
            t, u = create_tenant("Decr On Fail", "decr_fail@t.com")
            client = auth_client((t, u))
            tenant_id = t["id"]

            import datetime
            from app.extensions import redis_ext
            daily_key = f"daily_preview:{tenant_id}:{datetime.date.today().isoformat()}"
            redis_ext._client.data.pop(daily_key, None)

            with patch("app.blueprints.customer.settings.generate_order_audio",
                       side_effect=RuntimeError("provider timeout")):
                res = client.post("/api/settings/tts-preview", data={
                    "voice_script": "test",
                    "tts_provider": "edge",
                    "edge_tts_voice": "bn-BD-NabanitaNeural",
                })
            assert res.status_code == 422
            # Counter should be 0 again (decremented after failure)
            count_after = redis_ext._client.data.get(daily_key, 0)
            assert count_after == 0


# ── Concurrency: double-reservation race ─────────────────────────────────────

class TestConcurrencyNoDoubleCharge:
    def test_concurrent_same_idempotency_key_only_one_wallet_debit(self, app, create_tenant):
        """Two threads racing with same idempotency key must not double-debit the wallet."""
        with app.app_context():
            t, u = create_tenant("Concurrent", "concurrent@t.com")
            tid = t["id"]
            db.execute(
                "INSERT INTO wallets (tenant_id, balance) VALUES (%s, 100) ON CONFLICT (tenant_id) DO UPDATE SET balance = 100",
                (tid,),
            )
            billing_month = _today_month()
            # Fill quota so overage kicks in
            db.execute(
                """
                INSERT INTO tts_usage_reservations
                    (tenant_id, provider, credential_mode, purpose, idempotency_key,
                     billing_month, characters_reserved, quota_characters_reserved,
                     overage_characters_reserved, estimated_overage_amount, status)
                VALUES (%s, 'gemini', 'platform_managed', 'production', %s, %s, 10000, 10000, 0, 0, 'committed')
                """,
                (tid, make_preview_idempotency_key(), billing_month),
            )

            shared_key = make_production_idempotency_key(tid, "WC-RACE", "x", "gemini", "production", 1)
            results = []
            errors = []

            def _do_reserve():
                try:
                    r = reserve_tts_usage(
                        tid, "gemini", "platform_managed", "production", shared_key, 1000,
                        plan_policy=_charge_wallet_policy(10_000, rate="0.5"),
                    )
                    results.append(r)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=_do_reserve) for _ in range(4)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

            # At least one succeeded; all others got the same idempotent row
            assert len(errors) == 0 or all(isinstance(e, (InsufficientBalanceError,)) for e in errors)
            # Wallet debited at most once
            wrow = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tid,))
            balance = Decimal(str(wrow["balance"]))
            # Expected: max one debit of 0.5 (1000 chars overage × 0.5/1000)
            assert balance >= Decimal("99.5"), f"Balance {balance} suggests double-debit"
