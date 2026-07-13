# tests/integration/test_tts_hardening.py
"""Hardening audit tests for TTS quota reservation and billing (Step 5).

Phases 2–7: strict idempotency, exact Decimal arithmetic, provider-failure
refund, production-call idempotency, live-API-style tests, and scheduler
cleanup idempotency.

All money assertions use Decimal exactly — no >= approximations.
Threading note: prevent_background_threads fixture serialises non-daemon
threads via mock_start (real thread + immediate join), so concurrency tests
run sequentially through the same DB connection but verify the idempotency
path that would be taken in production.
"""
from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal

import pytest

from app.db import db
from app.exceptions import InsufficientBalanceError, TTSQuotaExceededError
from app.services.tts_usage_service import (
    _calculate_overage_charge,
    commit_tts_usage,
    expire_stale_tts_reservations,
    make_preview_idempotency_key,
    make_production_idempotency_key,
    release_tts_usage,
    reserve_tts_usage,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _today_month() -> date:
    now = datetime.now(timezone.utc)
    return date(now.year, now.month, 1)


def _block_policy(quota: int | None = 10_000) -> dict:
    return {
        "monthly_character_quota": quota,
        "extra_usage_policy": "block",
        "overage_rate_per_1000_chars": Decimal("0"),
        "minimum_overage_charge": Decimal("0"),
        "allow_preview_overage": False,
    }


def _charge_policy(quota: int = 10_000, rate: str = "0.5", minimum: str = "0") -> dict:
    return {
        "monthly_character_quota": quota,
        "extra_usage_policy": "charge_wallet",
        "overage_rate_per_1000_chars": Decimal(rate),
        "minimum_overage_charge": Decimal(minimum),
        "allow_preview_overage": False,
    }


def _fill_quota(tid: int, chars: int, billing_month: date) -> None:
    """Insert a committed reservation that consumes `chars` of quota."""
    db.execute(
        """
        INSERT INTO tts_usage_reservations
            (tenant_id, provider, credential_mode, purpose, idempotency_key,
             billing_month, characters_reserved, quota_characters_reserved,
             overage_characters_reserved, estimated_overage_amount, status)
        VALUES (%s, 'gemini', 'platform_managed', 'production', %s, %s,
                %s, %s, 0, 0, 'committed')
        """,
        (tid, make_preview_idempotency_key(), billing_month, chars, chars),
    )


def _set_wallet(tid: int, balance: Decimal) -> None:
    db.execute(
        "INSERT INTO wallets (tenant_id, balance) VALUES (%s, %s) "
        "ON CONFLICT (tenant_id) DO UPDATE SET balance = %s",
        (tid, balance, balance),
    )


def _get_balance(tid: int) -> Decimal:
    row = db.query_one("SELECT balance FROM wallets WHERE tenant_id = %s", (tid,))
    return Decimal(str(row["balance"])) if row else Decimal("0")


def _reservation_count(tid: int, key: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS cnt FROM tts_usage_reservations "
        "WHERE tenant_id = %s AND idempotency_key = %s",
        (tid, key),
    )
    return int(row["cnt"]) if row else 0


def _wallet_txn_count(tid: int, ref_id: str) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS cnt FROM wallet_transactions "
        "WHERE tenant_id = %s AND ref_id = %s",
        (tid, ref_id),
    )
    return int(row["cnt"]) if row else 0


def _usage_row(tid: int, provider: str) -> dict | None:
    return db.query_one(
        "SELECT * FROM tenant_tts_usage WHERE tenant_id = %s AND provider = %s",
        (tid, provider),
    )


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2: Strict Idempotency
# ══════════════════════════════════════════════════════════════════════════════

class TestStrictIdempotency:
    """10 sequential calls with same key must produce exactly one row, one txn,
    exact balance, and all callers see the same reservation ID."""

    def test_10_same_key_one_reservation_one_txn_exact_balance(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Idem10", "idem10@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())  # quota exhausted → all overage

            key = make_production_idempotency_key(tid, "WC-IDEM", "x", "gemini", "production", 1)
            results, errors = [], []

            def _try_reserve():
                try:
                    results.append(reserve_tts_usage(
                        tid, "gemini", "platform_managed", "production", key, 1000,
                        plan_policy=_charge_policy(10_000, rate="0.5"),
                    ))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=_try_reserve) for _ in range(10)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

            # No errors — all 10 calls succeed (sequential idempotency)
            assert errors == [], f"Unexpected errors: {errors}"

            # Exactly 1 reservation row
            assert _reservation_count(tid, key) == 1

            # All callers returned the same reservation ID
            ids = {r["id"] for r in results}
            assert len(ids) == 1, f"Multiple reservation IDs found: {ids}"

            # Exactly 1 wallet debit transaction for this key
            overage_ref = f"tts_overage:{key}"
            assert _wallet_txn_count(tid, overage_ref) == 1

            # Exact balance: 100 - 0.5 = 99.5 (1000 chars overage × 0.5/1000, ceil=1)
            assert _get_balance(tid) == Decimal("99.5"), \
                f"Expected 99.5, got {_get_balance(tid)}"

    def test_cross_tenant_same_key_creates_separate_reservations(self, app, create_tenant):
        """Same order/attempt on two different tenants → two rows, two wallet transactions.

        make_production_idempotency_key hashes tenant_id into the key, so even with
        the same order_id/script/provider the keys differ across tenants — and
        wallet_transactions.ref_id (which includes the key) also differs, avoiding
        the global unique constraint collision.
        """
        with app.app_context():
            tA, uA = create_tenant("CrossA", "crossA@h.com")
            tB, uB = create_tenant("CrossB", "crossB@h.com")
            _set_wallet(tA["id"], Decimal("50"))
            _set_wallet(tB["id"], Decimal("50"))
            _fill_quota(tA["id"], 10_000, _today_month())
            _fill_quota(tB["id"], 10_000, _today_month())

            # Same logical order but different tenant → different hashed keys
            kA = make_production_idempotency_key(tA["id"], "WC-SAME", "x", "gemini", "production", 1)
            kB = make_production_idempotency_key(tB["id"], "WC-SAME", "x", "gemini", "production", 1)
            assert kA != kB, "Keys must differ when tenant_id differs"

            rA = reserve_tts_usage(
                tA["id"], "gemini", "platform_managed", "production", kA, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            rB = reserve_tts_usage(
                tB["id"], "gemini", "platform_managed", "production", kB, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )

            assert rA["id"] != rB["id"]
            assert _reservation_count(tA["id"], kA) == 1
            assert _reservation_count(tB["id"], kB) == 1
            assert _wallet_txn_count(tA["id"], f"tts_overage:{kA}") == 1
            assert _wallet_txn_count(tB["id"], f"tts_overage:{kB}") == 1
            assert _get_balance(tA["id"]) == Decimal("49.5")
            assert _get_balance(tB["id"]) == Decimal("49.5")

    def test_different_keys_same_tenant_creates_separate_reservations(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("DiffKeys", "diffkeys@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            k1 = make_production_idempotency_key(tid, "WC-1", "x", "gemini", "production", 1)
            k2 = make_production_idempotency_key(tid, "WC-1", "x", "gemini", "production", 2)
            r1 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", k1, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            r2 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", k2, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )

            assert r1["id"] != r2["id"]
            assert _reservation_count(tid, k1) == 1
            assert _reservation_count(tid, k2) == 1
            # Two separate debits
            assert _get_balance(tid) == Decimal("99.0")

    def test_committed_retry_does_not_debit_again(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("CommitRetry", "commit_retry@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-CMT", "x", "gemini", "production", 1)
            r1 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            # Pass actual_overage_amount to prevent surplus refund (actual == estimated)
            commit_tts_usage(
                r1["id"], tid, actual_characters=1000, actual_audio_seconds=5.0,
                actual_overage_amount=Decimal("0.5"),
            )

            balance_after_commit = _get_balance(tid)
            assert balance_after_commit == Decimal("99.5")  # deducted once

            # Retry reserve with same key → returns committed row, no new debit
            r2 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            assert r2["id"] == r1["id"]
            assert _get_balance(tid) == Decimal("99.5")  # unchanged
            assert _wallet_txn_count(tid, f"tts_overage:{key}") == 1  # still exactly one

    def test_released_retry_returns_released_row_not_new_reservation(self, app, create_tenant):
        """After release, same key returns the released row (idempotent).
        A retry must use a new attempt key to create a fresh reservation."""
        with app.app_context():
            t, u = create_tenant("RelRetry", "rel_retry@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-REL", "x", "gemini", "production", 1)
            r1 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            release_tts_usage(r1["id"], tid, error_code="PROVIDER_DOWN")
            balance_after_release = _get_balance(tid)
            assert balance_after_release == Decimal("100.0")  # fully refunded

            # Same key → returns the released row (not a new reservation)
            r2 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            assert r2["id"] == r1["id"]
            assert r2["status"] == "released"
            # Balance must not change (no new debit for idempotent released row)
            assert _get_balance(tid) == Decimal("100.0")
            assert _reservation_count(tid, key) == 1

            # A genuinely new attempt uses a different key (attempt+1)
            key2 = make_production_idempotency_key(tid, "WC-REL", "x", "gemini", "production", 2)
            r3 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key2, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            assert r3["id"] != r1["id"]
            assert r3["status"] == "reserved"

    def test_no_duplicate_usage_increment_on_double_commit(self, app, create_tenant):
        """commit_tts_usage called twice must increment tenant_tts_usage exactly once."""
        with app.app_context():
            t, u = create_tenant("DblCommit", "dbl_commit@h.com")
            tid = t["id"]
            key = make_preview_idempotency_key()
            r = reserve_tts_usage(
                tid, "edge", "byok", "preview", key, 500,
                plan_policy=_block_policy(),
            )
            commit_tts_usage(r["id"], tid, actual_characters=500, actual_audio_seconds=5.0)
            commit_tts_usage(r["id"], tid, actual_characters=500, actual_audio_seconds=5.0)

            usage = _usage_row(tid, "edge")
            assert usage is not None
            assert int(usage["preview_requests"]) == 1
            assert int(usage["characters_used"]) == 500


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3: Wallet Exactness
# ══════════════════════════════════════════════════════════════════════════════

class TestWalletExactnessUnit:
    """Pure-unit tests for _calculate_overage_charge.

    Code uses ROUND_CEILING: overage_chars is ceiled to the nearest 1000
    before multiplying by rate_per_1000. All values are Decimal.
    """

    def test_1_char_overage(self):
        # ceil(1/1000) = 1 × 0.5 = 0.5
        assert _calculate_overage_charge(1, Decimal("0.5"), Decimal("0")) == Decimal("0.5")

    def test_999_chars_overage(self):
        # ceil(999/1000) = 1 × 0.5 = 0.5
        assert _calculate_overage_charge(999, Decimal("0.5"), Decimal("0")) == Decimal("0.5")

    def test_1000_chars_exact(self):
        # ceil(1000/1000) = 1 × 0.5 = 0.5
        assert _calculate_overage_charge(1000, Decimal("0.5"), Decimal("0")) == Decimal("0.5")

    def test_1001_chars_tips_to_next_thousand(self):
        # ceil(1001/1000) = 2 × 0.5 = 1.0
        assert _calculate_overage_charge(1001, Decimal("0.5"), Decimal("0")) == Decimal("1.0")

    def test_zero_overage_no_charge(self):
        assert _calculate_overage_charge(0, Decimal("5.0"), Decimal("0")) == Decimal("0")

    def test_minimum_charge_applied_when_below(self):
        # ceil(100/1000)=1 × 0.001 = 0.001 < minimum 1.0 → 1.0
        assert _calculate_overage_charge(100, Decimal("0.001"), Decimal("1.0")) == Decimal("1.0")

    def test_minimum_charge_not_applied_when_above(self):
        # ceil(10000/1000)=10 × 0.5 = 5.0 > minimum 1.0 → 5.0
        assert _calculate_overage_charge(10_000, Decimal("0.5"), Decimal("1.0")) == Decimal("5.0")

    def test_result_is_decimal_not_float(self):
        charge = _calculate_overage_charge(500, Decimal("0.75"), Decimal("0"))
        assert isinstance(charge, Decimal)
        assert float not in type(charge).__mro__


class TestWalletExactnessIntegration:
    """Integration: full reserve/commit/release cycle with exact Decimal assertions."""

    def test_zero_overage_within_quota_no_wallet_touch(self, app, create_tenant):
        """Request within quota: no wallet deduction, wallet_transaction_id is None."""
        with app.app_context():
            t, u = create_tenant("NoOverage", "no_overage@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("50"))
            key = make_preview_idempotency_key()
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "preview", key, 80,
                plan_policy=_block_policy(quota=100),
            )
            assert r["wallet_transaction_id"] is None
            assert Decimal(str(r["estimated_overage_amount"])) == Decimal("0")
            assert _get_balance(tid) == Decimal("50")  # unchanged

    def test_exact_overage_debit_and_transaction_count(self, app, create_tenant):
        """Overage=20 chars at rate=5.00/1000: ceil(20/1000)=1×5.00=5.00 debited."""
        with app.app_context():
            t, u = create_tenant("OvExact", "ov_exact@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 100, _today_month())  # 100/100 used

            key = make_production_idempotency_key(tid, "WC-OV", "x", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 20,
                plan_policy=_charge_policy(100, rate="5.0"),
            )
            assert Decimal(str(r["estimated_overage_amount"])) == Decimal("5.0")
            assert _get_balance(tid) == Decimal("95.0")
            assert _wallet_txn_count(tid, f"tts_overage:{key}") == 1

    def test_commit_exact_surplus_refund(self, app, create_tenant):
        """Estimated 2.0, actual 1.0 → refund 1.0 exactly."""
        with app.app_context():
            t, u = create_tenant("Surplus", "surplus@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-SRP", "x", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 2000,
                plan_policy=_charge_policy(10_000, rate="1.0"),
            )
            # estimated: ceil(2000/1000)=2 × 1.0 = 2.0
            assert Decimal(str(r["estimated_overage_amount"])) == Decimal("2.0")
            balance_after_reserve = _get_balance(tid)
            assert balance_after_reserve == Decimal("98.0")

            # Commit with actual_overage_amount=1.0 → refund 1.0
            commit_tts_usage(
                r["id"], tid,
                actual_characters=1000,
                actual_audio_seconds=5.0,
                actual_overage_amount=Decimal("1.0"),
            )
            balance_final = _get_balance(tid)
            assert balance_final == Decimal("99.0")  # 100 - 2.0 + 1.0 refund

    def test_insufficient_balance_raises_and_no_reservation_created(self, app, create_tenant):
        """InsufficientBalanceError: reservation row must NOT be created."""
        with app.app_context():
            t, u = create_tenant("InsBal", "ins_bal@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("0.1"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-INS", "x", "gemini", "production", 1)
            with pytest.raises(InsufficientBalanceError):
                reserve_tts_usage(
                    tid, "gemini", "platform_managed", "production", key, 1000,
                    plan_policy=_charge_policy(10_000, rate="0.5"),
                )
            # No reservation row created
            assert _reservation_count(tid, key) == 0
            # Balance unchanged
            assert _get_balance(tid) == Decimal("0.1")

    def test_wallet_debit_description_contains_no_secrets(self, app, create_tenant):
        """wallet_transactions.description must not contain provider API keys or rendered text."""
        with app.app_context():
            t, u = create_tenant("NoSecret", "no_secret@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("50"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-SEC", "x", "gemini", "production", 1)
            reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            row = db.query_one(
                "SELECT description FROM wallet_transactions WHERE tenant_id = %s AND ref_id = %s",
                (tid, f"tts_overage:{key}"),
            )
            assert row is not None
            desc = row["description"]
            # Description must not contain secrets or rendered script text
            assert "AIza" not in desc  # no Gemini API key prefix
            assert "sk-" not in desc   # no OpenAI-style key
            assert "{customer" not in desc  # no unrendered template variable


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4: Provider Failure and Refund
# ══════════════════════════════════════════════════════════════════════════════

class TestProviderFailureRefund:
    def test_release_restores_balance_exactly(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("Rel Exact", "rel_exact@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-RF", "x", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            assert _get_balance(tid) == Decimal("99.5")  # debited

            release_tts_usage(r["id"], tid, error_code="PROVIDER_TIMEOUT")

            # Balance fully restored
            assert _get_balance(tid) == Decimal("100.0")

    def test_release_creates_exactly_one_refund_transaction(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("RefTxn1", "ref_txn1@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-RT1", "x", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            release_tts_usage(r["id"], tid)

            refund_ref = f"tts_release_refund:{r['id']}"
            assert _wallet_txn_count(tid, refund_ref) == 1

    def test_release_idempotent_no_double_refund(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("DblRelease", "dbl_rel@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-DR", "x", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            release_tts_usage(r["id"], tid)
            release_tts_usage(r["id"], tid)  # second call must be no-op

            # Exactly one refund transaction
            refund_ref = f"tts_release_refund:{r['id']}"
            assert _wallet_txn_count(tid, refund_ref) == 1
            # Balance restored exactly once: 100 - 0.5 + 0.5 = 100
            assert _get_balance(tid) == Decimal("100.0")

    def test_successful_usage_not_updated_on_release(self, app, create_tenant):
        """release must not write to tenant_tts_usage."""
        with app.app_context():
            t, u = create_tenant("RelNoUsage", "rel_no_usage@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-RNU", "x", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            release_tts_usage(r["id"], tid)

            usage = _usage_row(tid, "gemini")
            # Either no row or production_requests == 0
            if usage:
                assert int(usage["production_requests"]) == 0

    def test_reservation_status_is_released(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("StatusRel", "status_rel@h.com")
            tid = t["id"]
            key = make_preview_idempotency_key()
            r = reserve_tts_usage(
                tid, "edge", "byok", "preview", key, 100,
                plan_policy=_block_policy(),
            )
            release_tts_usage(r["id"], tid, error_code="NET_TIMEOUT")
            row = db.query_one(
                "SELECT status, error_code, released_at FROM tts_usage_reservations WHERE id = %s",
                (r["id"],),
            )
            assert row["status"] == "released"
            assert row["error_code"] == "NET_TIMEOUT"
            assert row["released_at"] is not None

    def test_refund_ref_id_contains_no_provider_secrets(self, app, create_tenant):
        """wallet_transactions.description and ref_id for refund must not contain secrets."""
        with app.app_context():
            t, u = create_tenant("RefSecret", "ref_secret@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-RSEC", "x", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            release_tts_usage(r["id"], tid)

            refund_ref = f"tts_release_refund:{r['id']}"
            row = db.query_one(
                "SELECT description, ref_id FROM wallet_transactions WHERE ref_id = %s",
                (refund_ref,),
            )
            assert row is not None
            assert "AIza" not in row["description"]
            assert "sk-" not in row["description"]
            # ref_id is the reservation UUID, not a secret
            assert str(r["id"]) in row["ref_id"]


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5: Production Call Idempotency
# ══════════════════════════════════════════════════════════════════════════════

class TestProductionCallIdempotency:
    def test_first_call_one_reservation_one_commit(self, app, create_tenant):
        """Happy path: reserve → commit → exactly one reservation, one usage row."""
        with app.app_context():
            t, u = create_tenant("ProdFirst", "prod_first@h.com")
            tid = t["id"]
            key = make_production_idempotency_key(tid, "WC-P1", "abc", "edge", "production", 1)
            r = reserve_tts_usage(
                tid, "edge", "byok", "production", key, 500,
                plan_policy=_block_policy(),
            )
            commit_tts_usage(r["id"], tid, actual_characters=500, actual_audio_seconds=5.0)

            assert _reservation_count(tid, key) == 1
            row = db.query_one(
                "SELECT status FROM tts_usage_reservations WHERE id = %s", (r["id"],)
            )
            assert row["status"] == "committed"

            usage = _usage_row(tid, "edge")
            assert int(usage["production_requests"]) == 1
            assert int(usage["characters_used"]) == 500

    def test_retry_same_key_no_extra_debit(self, app, create_tenant):
        """Worker retry with same key: reserve again returns committed row, balance unchanged."""
        with app.app_context():
            t, u = create_tenant("ProdRetry", "prod_retry@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-R1", "abc", "gemini", "production", 1)
            r1 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            commit_tts_usage(
                r1["id"], tid, actual_characters=1000, actual_audio_seconds=5.0,
                actual_overage_amount=Decimal("0.5"),
            )
            balance_after_commit = _get_balance(tid)
            assert balance_after_commit == Decimal("99.5")

            # Simulate webhook/worker retry with same key
            r2 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            assert r2["id"] == r1["id"]
            # No additional debit
            assert _get_balance(tid) == Decimal("99.5")
            assert _wallet_txn_count(tid, f"tts_overage:{key}") == 1

    def test_failed_attempt_then_new_key_creates_separate_reservation(self, app, create_tenant):
        """Attempt 1 fails (released). Attempt 2 uses a new key → separate reservation."""
        with app.app_context():
            t, u = create_tenant("ProdFail", "prod_fail@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 10_000, _today_month())

            k1 = make_production_idempotency_key(tid, "WC-F1", "abc", "gemini", "production", 1)
            r1 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", k1, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            release_tts_usage(r1["id"], tid, error_code="ATTEMPT_1_FAILED")

            balance_after_release = _get_balance(tid)
            assert balance_after_release == Decimal("100.0")  # refunded

            # New attempt uses attempt number 2 → different key
            k2 = make_production_idempotency_key(tid, "WC-F1", "abc", "gemini", "production", 2)
            r2 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", k2, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            assert r2["id"] != r1["id"]
            assert r2["status"] == "reserved"
            # Debited for second attempt
            assert _get_balance(tid) == Decimal("99.5")

    def test_byok_provider_no_reservation_wallet_charge(self, app, create_tenant):
        """BYOK path: reserve+commit, no wallet_transaction_id, no wallet debit."""
        with app.app_context():
            t, u = create_tenant("BYOKProd", "byok_prod@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("50"))

            key = make_production_idempotency_key(tid, "WC-B1", "abc", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "byok", "production", key, 1000,
                plan_policy=_block_policy(10_000),
            )
            assert r["wallet_transaction_id"] is None
            commit_tts_usage(r["id"], tid, actual_characters=1000, actual_audio_seconds=4.0)
            # Wallet not touched
            assert _get_balance(tid) == Decimal("50")


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 6: Live API-style Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestLiveApiBlockPolicy:
    def test_80_chars_within_100_quota_succeeds(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("BlkOK", "blk_ok@h.com")
            tid = t["id"]
            key = make_preview_idempotency_key()
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "preview", key, 80,
                plan_policy=_block_policy(quota=100),
            )
            assert r["status"] == "reserved"
            assert int(r["overage_characters_reserved"]) == 0

    def test_30_chars_after_80_exceeds_100_quota_blocked(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("BlkDeny", "blk_deny@h.com")
            tid = t["id"]
            _fill_quota(tid, 80, _today_month())  # 80/100 used

            key = make_preview_idempotency_key()
            with pytest.raises(TTSQuotaExceededError):
                reserve_tts_usage(
                    tid, "gemini", "platform_managed", "preview", key, 30,
                    plan_policy=_block_policy(quota=100),
                )
            # No reservation row created for blocked request
            assert _reservation_count(tid, key) == 0


class TestLiveApiWalletOverage:
    def test_exact_20_char_overage_debit_and_idempotent_retry(self, app, create_tenant):
        """quota=100, request=120 → overage=20, ceil(20/1000)=1 × 5.00 = 5.00 debited."""
        with app.app_context():
            t, u = create_tenant("OvLive", "ov_live@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))
            _fill_quota(tid, 100, _today_month())  # quota exhausted

            key = make_production_idempotency_key(tid, "WC-OVL", "x", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 20,
                plan_policy=_charge_policy(100, rate="5.0"),
            )
            assert Decimal(str(r["estimated_overage_amount"])) == Decimal("5.0")
            assert _get_balance(tid) == Decimal("95.0")

            # Idempotent retry: no second debit
            r2 = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 20,
                plan_policy=_charge_policy(100, rate="5.0"),
            )
            assert r2["id"] == r["id"]
            assert _get_balance(tid) == Decimal("95.0")
            assert _wallet_txn_count(tid, f"tts_overage:{key}") == 1


class TestLiveApiProviderFailure:
    def test_reserve_then_release_full_refund_no_success_usage(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("PFLive", "pf_live@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("50"))
            _fill_quota(tid, 10_000, _today_month())

            key = make_production_idempotency_key(tid, "WC-PFL", "x", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "platform_managed", "production", key, 1000,
                plan_policy=_charge_policy(10_000, rate="0.5"),
            )
            assert _get_balance(tid) == Decimal("49.5")

            release_tts_usage(r["id"], tid, error_code="PROVIDER_EXCEPTION")

            assert _get_balance(tid) == Decimal("50.0")  # fully restored
            usage = _usage_row(tid, "gemini")
            if usage:
                assert int(usage["production_requests"]) == 0


class TestLiveApiByok:
    def test_byok_gemini_no_wallet_transaction(self, app, create_tenant):
        """BYOK Gemini: no platform quota consumption, no wallet charge."""
        with app.app_context():
            t, u = create_tenant("BYOKLive", "byok_live@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("50"))

            key = make_production_idempotency_key(tid, "WC-BL", "x", "gemini", "production", 1)
            r = reserve_tts_usage(
                tid, "gemini", "byok", "production", key, 5000,
                plan_policy=_block_policy(10_000),
            )
            assert r["wallet_transaction_id"] is None
            assert Decimal(str(r["estimated_overage_amount"])) == Decimal("0")
            assert _get_balance(tid) == Decimal("50")

            row = db.query_one(
                "SELECT COUNT(*) AS cnt FROM wallet_transactions WHERE tenant_id = %s",
                (tid,),
            )
            assert int(row["cnt"]) == 0  # absolutely no wallet transactions for BYOK


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 7: Cleanup Scheduler
# ══════════════════════════════════════════════════════════════════════════════

class TestCleanupScheduler:
    def _insert_stale_reservation(
        self, tid: int, *,
        status: str = "reserved",
        wallet_txn_id: int | None = None,
        overage: Decimal = Decimal("0"),
        minutes_past: int = 5,
    ) -> str:
        """Insert a reservation with expires_at in the past. Returns idempotency_key."""
        key = make_preview_idempotency_key()
        db.execute(
            """
            INSERT INTO tts_usage_reservations
                (tenant_id, provider, credential_mode, purpose, idempotency_key,
                 billing_month, characters_reserved, quota_characters_reserved,
                 overage_characters_reserved, estimated_overage_amount, status,
                 wallet_transaction_id, expires_at)
            VALUES (%s, 'gemini', 'platform_managed', 'production', %s, %s,
                    1000, 1000, 0, %s, %s, %s,
                    now() - INTERVAL '1 second' * %s)
            """,
            (tid, key, _today_month(), overage, status, wallet_txn_id, minutes_past * 60),
        )
        return key

    def test_stale_reserved_row_marked_expired(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("ExpireTest", "expire_test@h.com")
            tid = t["id"]
            key = self._insert_stale_reservation(tid)
            count = expire_stale_tts_reservations()
            assert count >= 1
            row = db.query_one(
                "SELECT status FROM tts_usage_reservations WHERE idempotency_key = %s", (key,)
            )
            assert row["status"] == "expired"

    def test_committed_row_not_expired(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("NoExpireCmt", "no_exp_cmt@h.com")
            tid = t["id"]
            key = self._insert_stale_reservation(tid, status="committed")
            expire_stale_tts_reservations()
            row = db.query_one(
                "SELECT status FROM tts_usage_reservations WHERE idempotency_key = %s", (key,)
            )
            assert row["status"] == "committed"

    def test_released_row_not_expired(self, app, create_tenant):
        with app.app_context():
            t, u = create_tenant("NoExpireRel", "no_exp_rel@h.com")
            tid = t["id"]
            key = self._insert_stale_reservation(tid, status="released")
            expire_stale_tts_reservations()
            row = db.query_one(
                "SELECT status FROM tts_usage_reservations WHERE idempotency_key = %s", (key,)
            )
            assert row["status"] == "released"

    def test_expire_refunds_overage_exactly_once(self, app, create_tenant):
        """Stale row with wallet debit → expire → exactly one credit transaction."""
        with app.app_context():
            t, u = create_tenant("ExpireRefund", "expire_refund@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))

            # Insert a real wallet debit so wallet_transaction_id is valid
            from app.services.wallet_service import deduct as wallet_deduct
            txn = wallet_deduct(tid, Decimal("0.5"), description="test debit", ref_id=None)
            assert _get_balance(tid) == Decimal("99.5")

            key = self._insert_stale_reservation(
                tid, wallet_txn_id=txn["id"], overage=Decimal("0.5")
            )
            # Update the reservation's wallet_transaction_id correctly
            db.execute(
                "UPDATE tts_usage_reservations SET wallet_transaction_id = %s, "
                "estimated_overage_amount = 0.5 WHERE idempotency_key = %s",
                (txn["id"], key),
            )

            expire_stale_tts_reservations()

            # Balance restored
            assert _get_balance(tid) == Decimal("100.0")

            # Exactly one credit transaction with the expire refund ref_id
            res_row = db.query_one(
                "SELECT id FROM tts_usage_reservations WHERE idempotency_key = %s", (key,)
            )
            expire_ref = f"tts_expire_refund:{res_row['id']}"
            assert _wallet_txn_count(tid, expire_ref) == 1

    def test_expire_idempotent_double_run(self, app, create_tenant):
        """Running expire_stale_tts_reservations twice on same row must not double-refund."""
        with app.app_context():
            t, u = create_tenant("ExpireIdem", "expire_idem@h.com")
            tid = t["id"]
            _set_wallet(tid, Decimal("100"))

            from app.services.wallet_service import deduct as wallet_deduct
            txn = wallet_deduct(tid, Decimal("0.5"), description="test debit", ref_id=None)

            key = self._insert_stale_reservation(
                tid, wallet_txn_id=txn["id"], overage=Decimal("0.5")
            )
            db.execute(
                "UPDATE tts_usage_reservations SET wallet_transaction_id = %s, "
                "estimated_overage_amount = 0.5 WHERE idempotency_key = %s",
                (txn["id"], key),
            )

            expire_stale_tts_reservations()
            expire_stale_tts_reservations()  # second run must be no-op

            # Balance restored exactly once: 100 - 0.5 + 0.5 = 100
            assert _get_balance(tid) == Decimal("100.0")

            res_row = db.query_one(
                "SELECT id FROM tts_usage_reservations WHERE idempotency_key = %s", (key,)
            )
            expire_ref = f"tts_expire_refund:{res_row['id']}"
            assert _wallet_txn_count(tid, expire_ref) == 1

    def test_expire_does_not_decrement_committed_usage(self, app, create_tenant):
        """Run expire on a DB with committed rows → usage totals unchanged."""
        with app.app_context():
            t, u = create_tenant("ExpireUsage", "expire_usage@h.com")
            tid = t["id"]

            # Create a committed row
            key = make_preview_idempotency_key()
            r = reserve_tts_usage(
                tid, "edge", "byok", "preview", key, 100, plan_policy=_block_policy()
            )
            commit_tts_usage(r["id"], tid, actual_characters=100, actual_audio_seconds=1.0)
            usage_before = _usage_row(tid, "edge")
            assert usage_before is not None

            # Run expire (no stale rows for this tenant)
            expire_stale_tts_reservations()

            usage_after = _usage_row(tid, "edge")
            assert int(usage_after["preview_requests"]) == int(usage_before["preview_requests"])
            assert int(usage_after["characters_used"]) == int(usage_before["characters_used"])

    def test_expire_processes_multiple_stale_rows(self, app, create_tenant):
        """Three stale rows → all three get expired in one run."""
        with app.app_context():
            t, u = create_tenant("ExpireBatch", "expire_batch@h.com")
            tid = t["id"]
            keys = [self._insert_stale_reservation(tid) for _ in range(3)]
            count = expire_stale_tts_reservations()
            assert count >= 3
            for key in keys:
                row = db.query_one(
                    "SELECT status FROM tts_usage_reservations WHERE idempotency_key = %s", (key,)
                )
                assert row["status"] == "expired"
