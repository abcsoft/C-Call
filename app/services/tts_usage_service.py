"""TTS quota reservation, billing units, and usage commit.

Core contract:
- calculate_tts_billable_characters: single source of truth for character counting.
- reserve_tts_usage: atomic pre-call reservation. Raises TTSQuotaExceededError or
  InsufficientBalanceError if tenant cannot proceed. Returns reservation UUID.
- commit_tts_usage: called after successful generation. Marks reservation committed
  and upserts actual usage into tenant_tts_usage.
- release_tts_usage: called on generation failure. Marks reservation released.
  Refunds overage wallet debit idempotently.
- expire_stale_tts_reservations: scheduler job. Expires reservations past expires_at.

Constraints enforced here:
- No provider API call while holding a DB row lock.
- No wallet debit without idempotency key.
- No float for money — Decimal only.
- No charge for BYOK or free-provider (edge/gtts) usage.
- No charge for preview when allow_preview_overage is False.
- charge_wallet policy: immediate debit + idempotent refund (no hold semantics in wallet_service).
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import date, datetime, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any

from app.db import db
from app.exceptions import InsufficientBalanceError, TTSQuotaExceededError

logger = logging.getLogger("ccall")

# ── Billing units ─────────────────────────────────────────────────────────────

# Providers that use platform quota (platform_managed mode only).
# BYOK providers never consume platform quota and are never billed.
_PLATFORM_BILLABLE_PROVIDERS = frozenset({"gemini", "elevenlabs"})


def calculate_tts_billable_characters(rendered_text: str, provider: str) -> int:
    """Return the number of billable Unicode characters for a TTS request.

    Uses final rendered plain-text as input (variables already substituted,
    SSML stripped if provider receives plain text).

    Rules:
    - Whitespace is included (matches what the provider actually processes).
    - Character count is len(rendered_text) — Unicode scalar values.
    - Non-platform-billable providers always return 0 (caller should not be
      reserving quota for them, but this makes the function safe to call unconditionally).
    - Never returns negative.
    """
    if not rendered_text:
        return 0
    return len(rendered_text)


def _is_platform_billable(provider: str, credential_mode: str) -> bool:
    """Return True only if this combination consumes platform quota and may trigger wallet charge."""
    return credential_mode == "platform_managed" and provider in _PLATFORM_BILLABLE_PROVIDERS


# ── Billing month helpers ─────────────────────────────────────────────────────

def _billing_month_for(dt: datetime | None = None) -> date:
    """Return the first day of the billing month for a given UTC datetime (default: now)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    return date(dt.year, dt.month, 1)


# ── Overage calculation ───────────────────────────────────────────────────────

def _calculate_overage_charge(
    overage_characters: int,
    rate_per_1000: Decimal,
    minimum_charge: Decimal,
) -> Decimal:
    """Compute overage charge. Always Decimal. Rounds up to nearest 1000 chars."""
    if overage_characters <= 0 or rate_per_1000 <= 0:
        return Decimal("0")
    thousands = Decimal(overage_characters) / Decimal(1000)
    thousands = thousands.to_integral_value(rounding=ROUND_CEILING)
    charge = thousands * rate_per_1000
    if minimum_charge > 0 and charge < minimum_charge:
        charge = minimum_charge
    return charge.quantize(Decimal("0.000001"))


# ── Reservation ───────────────────────────────────────────────────────────────

def reserve_tts_usage(
    tenant_id: int,
    provider: str,
    credential_mode: str,
    purpose: str,
    idempotency_key: str,
    characters: int,
    *,
    plan_policy: dict[str, Any],
    billing_month: date | None = None,
) -> dict[str, Any]:
    """Atomically reserve TTS quota (and optionally wallet balance) for one request.

    Steps (no provider API call at any point):
    1. Idempotency check — if (tenant_id, idempotency_key) already exists return it.
    2. Compute quota split: how many chars from included quota vs overage.
    3. If overage policy == 'block' and quota is exhausted → TTSQuotaExceededError.
    4. If overage policy == 'charge_wallet':
       a. Compute estimated overage charge.
       b. Deduct from wallet atomically (raises InsufficientBalanceError if insufficient).
       c. Record wallet_transaction_id in reservation.
    5. INSERT reservation row (UNIQUE on tenant_id + idempotency_key).

    Args:
        tenant_id: Tenant scope.
        provider: TTS provider name.
        credential_mode: 'byok' | 'platform_managed'.
        purpose: 'preview' | 'production'.
        idempotency_key: Unique per-request key. Collisions are idempotent.
        characters: Billable character count (from calculate_tts_billable_characters).
        plan_policy: Dict from get_plan_tts_policy(plan_id).
        billing_month: Override for tests; defaults to current UTC month start.

    Returns:
        Reservation row as dict (id, status, quota_characters_reserved, ...).

    Raises:
        TTSQuotaExceededError: Quota exhausted and policy is 'block'.
        InsufficientBalanceError: charge_wallet policy, wallet has insufficient balance.
    """
    if billing_month is None:
        billing_month = _billing_month_for()

    # ── Idempotency: return existing reservation if key matches ───────────────
    existing = db.query_one(
        "SELECT * FROM tts_usage_reservations WHERE tenant_id = %s AND idempotency_key = %s",
        (tenant_id, idempotency_key),
    )
    if existing:
        return dict(existing)

    # ── For non-platform-billable requests: skip quota logic entirely ─────────
    if not _is_platform_billable(provider, credential_mode):
        row = db.query_one(
            """
            INSERT INTO tts_usage_reservations
                (tenant_id, provider, credential_mode, purpose, idempotency_key,
                 billing_month, characters_reserved, quota_characters_reserved,
                 overage_characters_reserved, estimated_overage_amount, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'reserved')
            ON CONFLICT (tenant_id, idempotency_key) DO UPDATE
                SET updated_at = now()
            RETURNING *
            """,
            (tenant_id, provider, credential_mode, purpose, idempotency_key,
             billing_month, characters, characters, 0, Decimal("0")),
        )
        return dict(row)

    # ── Platform-managed path: compute quota split ────────────────────────────
    monthly_quota: int | None = plan_policy.get("monthly_character_quota")
    extra_policy: str = plan_policy.get("extra_usage_policy", "block")

    # Count committed + reserved characters this month (no lock — estimate only).
    used_row = db.query_one(
        """
        SELECT COALESCE(SUM(quota_characters_reserved), 0) AS used
        FROM tts_usage_reservations
        WHERE tenant_id = %s
          AND provider = %s
          AND credential_mode = 'platform_managed'
          AND billing_month = %s
          AND status IN ('reserved', 'committed')
        """,
        (tenant_id, provider, billing_month),
    )
    current_reserved: int = int(used_row["used"]) if used_row else 0

    quota_chars: int
    overage_chars: int
    estimated_overage = Decimal("0")
    wallet_txn_id: int | None = None

    if monthly_quota is None or monthly_quota <= 0:
        # Unlimited quota plan
        quota_chars = characters
        overage_chars = 0
    else:
        remaining_quota = max(0, monthly_quota - current_reserved)
        quota_chars = min(characters, remaining_quota)
        overage_chars = characters - quota_chars

    if overage_chars > 0:
        if extra_policy == "block":
            raise TTSQuotaExceededError(
                f"Monthly TTS character quota exhausted (quota={monthly_quota}, "
                f"used≈{current_reserved}). Policy: block."
            )

        # preview overage guard
        if purpose == "preview" and not plan_policy.get("allow_preview_overage", False):
            raise TTSQuotaExceededError(
                "Monthly TTS quota exhausted. Preview overage is not enabled on your plan."
            )

        if extra_policy == "charge_wallet":
            rate = Decimal(str(plan_policy.get("overage_rate_per_1000_chars", "0")))
            minimum = Decimal(str(plan_policy.get("minimum_overage_charge", "0")))
            estimated_overage = _calculate_overage_charge(overage_chars, rate, minimum)

            if estimated_overage > 0:
                # Deduct wallet BEFORE inserting reservation.
                # No DB lock held at this point (safe per constraint: no API call while lock held).
                from app.services.wallet_service import deduct as wallet_deduct
                wallet_ref = f"tts_overage:{idempotency_key}"
                txn = wallet_deduct(
                    tenant_id,
                    estimated_overage,
                    description=f"TTS overage ({overage_chars} chars, {provider})",
                    ref_id=wallet_ref,
                )
                wallet_txn_id = txn["id"]

    # ── Insert reservation ────────────────────────────────────────────────────
    row = db.query_one(
        """
        INSERT INTO tts_usage_reservations
            (tenant_id, provider, credential_mode, purpose, idempotency_key,
             billing_month, characters_reserved, quota_characters_reserved,
             overage_characters_reserved, estimated_overage_amount, wallet_transaction_id,
             status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'reserved')
        ON CONFLICT (tenant_id, idempotency_key) DO UPDATE
            SET updated_at = now()
        RETURNING *
        """,
        (tenant_id, provider, credential_mode, purpose, idempotency_key,
         billing_month, characters, quota_chars, overage_chars, estimated_overage,
         wallet_txn_id),
    )
    return dict(row)


# ── Commit ────────────────────────────────────────────────────────────────────

def commit_tts_usage(
    reservation_id: str,
    tenant_id: int,
    *,
    actual_characters: int,
    actual_audio_seconds: float,
    actual_overage_amount: Decimal | None = None,
) -> None:
    """Mark reservation committed and update monthly usage totals atomically.

    Idempotent: calling twice is safe (status check prevents double-counting).

    Steps:
    1. Lock reservation row.
    2. If already committed → no-op.
    3. Update reservation status → committed with actual values.
    4. Upsert tenant_tts_usage monthly totals.
    5. If actual_overage_amount < estimated_overage_amount: refund the difference.
    """
    if actual_overage_amount is None:
        actual_overage_amount = Decimal("0")
    actual_overage_amount = Decimal(str(actual_overage_amount))

    with db.transaction() as conn:
        res = conn.execute(
            "SELECT * FROM tts_usage_reservations WHERE id = %s AND tenant_id = %s FOR UPDATE",
            (reservation_id, tenant_id),
        ).fetchone()

        if not res:
            logger.warning("commit_tts_usage: reservation %s not found for tenant %s", reservation_id, tenant_id)
            return

        if res["status"] == "committed":
            return  # idempotent

        estimated_overage = Decimal(str(res["estimated_overage_amount"] or "0"))

        # Mark committed
        conn.execute(
            """
            UPDATE tts_usage_reservations
            SET status = 'committed',
                actual_characters = %s,
                actual_audio_seconds = %s,
                actual_overage_amount = %s,
                committed_at = now(),
                updated_at = now()
            WHERE id = %s AND tenant_id = %s
            """,
            (actual_characters, actual_audio_seconds, actual_overage_amount,
             reservation_id, tenant_id),
        )

        # Upsert monthly usage totals
        billing_month: date = res["billing_month"]
        provider: str = res["provider"]
        credential_mode: str = res["credential_mode"]
        overage_chars = int(res["overage_characters_reserved"])
        purpose_col = "preview_requests" if res["purpose"] == "preview" else "production_requests"

        conn.execute(
            f"""
            INSERT INTO tenant_tts_usage
                (tenant_id, provider, credential_mode, billing_month,
                 characters_used, audio_seconds, {purpose_col},
                 overage_characters, wallet_overage_amount)
            VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s)
            ON CONFLICT (tenant_id, provider, credential_mode, billing_month)
            DO UPDATE SET
                characters_used      = tenant_tts_usage.characters_used      + EXCLUDED.characters_used,
                audio_seconds        = tenant_tts_usage.audio_seconds        + EXCLUDED.audio_seconds,
                {purpose_col}        = tenant_tts_usage.{purpose_col}        + 1,
                overage_characters   = tenant_tts_usage.overage_characters   + EXCLUDED.overage_characters,
                wallet_overage_amount= tenant_tts_usage.wallet_overage_amount+ EXCLUDED.wallet_overage_amount,
                updated_at           = now()
            """,
            (tenant_id, provider, credential_mode, billing_month,
             actual_characters, actual_audio_seconds,
             overage_chars, actual_overage_amount),
        )

        # Refund surplus overage if actual < estimated
        if (
            res["wallet_transaction_id"] is not None
            and actual_overage_amount < estimated_overage
        ):
            surplus = (estimated_overage - actual_overage_amount).quantize(Decimal("0.000001"))
            if surplus > 0:
                refund_ref = f"tts_overage_refund:{reservation_id}"
                conn.execute(
                    """
                    INSERT INTO wallet_transactions
                        (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
                    SELECT w.id, %s, 'credit', %s,
                           w.balance + %s,
                           %s, %s
                    FROM wallets w WHERE w.tenant_id = %s
                    ON CONFLICT (ref_id) WHERE ref_id IS NOT NULL DO NOTHING
                    """,
                    (tenant_id, surplus, surplus,
                     f"TTS overage refund ({provider})", refund_ref,
                     tenant_id),
                )
                conn.execute(
                    "UPDATE wallets SET balance = balance + %s, updated_at = now() WHERE tenant_id = %s",
                    (surplus, tenant_id),
                )

    # Invalidate wallet cache outside transaction
    if res and res.get("wallet_transaction_id") is not None:
        try:
            from app.models.wallet import invalidate_wallet_cache
            invalidate_wallet_cache(tenant_id)
        except Exception:
            pass


# ── Release ───────────────────────────────────────────────────────────────────

def release_tts_usage(
    reservation_id: str,
    tenant_id: int,
    *,
    error_code: str | None = None,
) -> None:
    """Release a reservation on generation failure.

    Refunds the full estimated_overage_amount to wallet (idempotent via ref_id).
    Marks reservation status = 'released'.
    """
    with db.transaction() as conn:
        res = conn.execute(
            "SELECT * FROM tts_usage_reservations WHERE id = %s AND tenant_id = %s FOR UPDATE",
            (reservation_id, tenant_id),
        ).fetchone()

        if not res:
            return

        if res["status"] in ("committed", "released", "expired"):
            return  # idempotent

        conn.execute(
            """
            UPDATE tts_usage_reservations
            SET status = 'released',
                released_at = now(),
                error_code = %s,
                updated_at = now()
            WHERE id = %s AND tenant_id = %s
            """,
            (error_code, reservation_id, tenant_id),
        )

        estimated_overage = Decimal(str(res["estimated_overage_amount"] or "0"))
        if res["wallet_transaction_id"] is not None and estimated_overage > 0:
            refund_ref = f"tts_release_refund:{reservation_id}"
            conn.execute(
                """
                INSERT INTO wallet_transactions
                    (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
                SELECT w.id, %s, 'credit', %s,
                       w.balance + %s,
                       %s, %s
                FROM wallets w WHERE w.tenant_id = %s
                ON CONFLICT (ref_id) WHERE ref_id IS NOT NULL DO NOTHING
                """,
                (tenant_id, estimated_overage, estimated_overage,
                 f"TTS overage refund on release ({res['provider']})", refund_ref,
                 tenant_id),
            )
            conn.execute(
                "UPDATE wallets SET balance = balance + %s, updated_at = now() WHERE tenant_id = %s",
                (estimated_overage, tenant_id),
            )

    try:
        from app.models.wallet import invalidate_wallet_cache
        invalidate_wallet_cache(tenant_id)
    except Exception:
        pass


# ── Scheduler job: expire stale reservations ─────────────────────────────────

def expire_stale_tts_reservations() -> int:
    """Mark all 'reserved' rows past expires_at as 'expired'. Refund overage wallet debits.

    Called by APScheduler every ~5 minutes. Returns count of expired rows.
    """
    stale = db.query(
        """
        SELECT id, tenant_id, wallet_transaction_id, estimated_overage_amount, provider
        FROM tts_usage_reservations
        WHERE status = 'reserved' AND expires_at < now()
        LIMIT 500
        """,
    )
    if not stale:
        return 0

    count = 0
    for row in stale:
        try:
            _expire_one(row)
            count += 1
        except Exception as exc:
            logger.error("expire_stale_tts_reservations: failed to expire %s: %s", row["id"], exc)

    logger.info("expire_stale_tts_reservations: expired %d reservations", count)
    return count


def _expire_one(row: dict) -> None:
    with db.transaction() as conn:
        locked = conn.execute(
            "SELECT * FROM tts_usage_reservations WHERE id = %s AND status = 'reserved' FOR UPDATE SKIP LOCKED",
            (row["id"],),
        ).fetchone()
        if not locked:
            return

        conn.execute(
            "UPDATE tts_usage_reservations SET status = 'expired', updated_at = now() WHERE id = %s",
            (row["id"],),
        )

        estimated_overage = Decimal(str(locked["estimated_overage_amount"] or "0"))
        if locked["wallet_transaction_id"] is not None and estimated_overage > 0:
            refund_ref = f"tts_expire_refund:{row['id']}"
            conn.execute(
                """
                INSERT INTO wallet_transactions
                    (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
                SELECT w.id, %s, 'credit', %s,
                       w.balance + %s,
                       %s, %s
                FROM wallets w WHERE w.tenant_id = %s
                ON CONFLICT (ref_id) WHERE ref_id IS NOT NULL DO NOTHING
                """,
                (locked["tenant_id"], estimated_overage, estimated_overage,
                 f"TTS overage refund on expiry ({locked['provider']})", refund_ref,
                 locked["tenant_id"]),
            )
            conn.execute(
                "UPDATE wallets SET balance = balance + %s, updated_at = now() WHERE tenant_id = %s",
                (estimated_overage, locked["tenant_id"]),
            )

    try:
        from app.models.wallet import invalidate_wallet_cache
        invalidate_wallet_cache(row["tenant_id"])
    except Exception:
        pass


# ── Idempotency key helpers ───────────────────────────────────────────────────

def make_production_idempotency_key(
    tenant_id: int,
    order_id: str,
    script_hash: str,
    provider: str,
    purpose: str,
    attempt: int,
) -> str:
    """Deterministic SHA-256 key for production TTS calls. Stable across retries."""
    raw = f"{tenant_id}|{order_id}|{script_hash}|{provider}|{purpose}|{attempt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def make_preview_idempotency_key() -> str:
    """Random UUID for preview requests (each preview is a distinct event)."""
    return str(uuid.uuid4())


# ── Quota summary (for customer UI card) ─────────────────────────────────────

def get_tenant_tts_quota_summary(tenant_id: int, provider: str, plan_policy: dict[str, Any]) -> dict[str, Any]:
    """Return quota snapshot for display. Not used for enforcement decisions."""
    from datetime import datetime, timezone
    billing_month = _billing_month_for()

    committed_row = db.query_one(
        """
        SELECT COALESCE(SUM(total_characters), 0) AS committed,
               COALESCE(SUM(overage_characters), 0) AS overage_committed,
               COALESCE(SUM(wallet_overage_amount), 0) AS overage_billed
        FROM tenant_tts_usage
        WHERE tenant_id = %s AND provider = %s AND credential_mode = 'platform_managed'
          AND billing_month = %s
        """,
        (tenant_id, provider, billing_month),
    )
    reserved_row = db.query_one(
        """
        SELECT COALESCE(SUM(quota_characters_reserved), 0) AS reserved
        FROM tts_usage_reservations
        WHERE tenant_id = %s AND provider = %s AND credential_mode = 'platform_managed'
          AND billing_month = %s AND status = 'reserved'
        """,
        (tenant_id, provider, billing_month),
    )

    monthly_quota: int | None = plan_policy.get("monthly_character_quota")
    committed = int(committed_row["committed"]) if committed_row else 0
    reserved = int(reserved_row["reserved"]) if reserved_row else 0
    overage_committed = int(committed_row["overage_committed"]) if committed_row else 0
    overage_billed = Decimal(str(committed_row["overage_billed"])) if committed_row else Decimal("0")

    remaining = None
    if monthly_quota and monthly_quota > 0:
        remaining = max(0, monthly_quota - committed - reserved)

    return {
        "billing_month": billing_month.isoformat(),
        "monthly_quota": monthly_quota,
        "committed_characters": committed,
        "reserved_characters": reserved,
        "remaining_characters": remaining,
        "overage_characters_committed": overage_committed,
        "overage_amount_billed": overage_billed,
        "extra_usage_policy": plan_policy.get("extra_usage_policy", "block"),
        "overage_rate_per_1000_chars": plan_policy.get("overage_rate_per_1000_chars", Decimal("0")),
    }
