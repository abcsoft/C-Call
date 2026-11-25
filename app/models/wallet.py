from __future__ import annotations

import json
import logging
from typing import Any
from decimal import Decimal
from app.db import db
from app.exceptions import InsufficientBalanceError

logger = logging.getLogger("ccall")

_WALLET_CACHE_TTL = 8  # seconds — short because balance changes on every call


def _wallet_cache_key(tenant_id: int) -> str:
    return f"wallet:{tenant_id}"


def invalidate_wallet_cache(tenant_id: int) -> None:
    """Delete the cached wallet row for tenant_id. Called by every deduct/credit/refund."""
    try:
        from app.extensions import redis_ext
        redis_ext.client.delete(_wallet_cache_key(tenant_id))
    except Exception as exc:
        logger.warning("wallet cache invalidation failed for tenant %s: %s", tenant_id, exc)


def get_wallet(tenant_id: int) -> dict[str, Any] | None:
    """Return wallet row, cached in Redis for 8s. Short TTL because balance is
    used for live pre-call balance checks — stale data must not block a call."""
    try:
        from app.extensions import redis_ext
        cached = redis_ext.client.get(_wallet_cache_key(tenant_id))
        if cached:
            row = json.loads(cached.decode("utf-8") if isinstance(cached, bytes) else cached)
            # Re-hydrate Decimal fields so callers always get correct types
            for field in ("balance", "low_balance_threshold", "total_credited", "total_debited"):
                if row.get(field) is not None:
                    row[field] = Decimal(str(row[field]))
            return row
    except Exception as exc:
        logger.warning("wallet cache read failed for tenant %s: %s", tenant_id, exc)

    row = db.query_one("SELECT * FROM wallets WHERE tenant_id = %s", (tenant_id,))
    if row:
        try:
            from app.extensions import redis_ext
            # Serialize Decimal/datetime as string so json round-trips cleanly.
            import datetime
            serializable = {
                k: str(v) if isinstance(v, (Decimal, datetime.datetime, datetime.date)) else v
                for k, v in row.items()
            }
            redis_ext.client.setex(_wallet_cache_key(tenant_id), _WALLET_CACHE_TTL, json.dumps(serializable))
        except Exception as exc:
            logger.warning("wallet cache write failed for tenant %s: %s", tenant_id, exc)
    return row

def update_low_balance_threshold(tenant_id: int, threshold: Decimal) -> None:
    db.execute(
        "UPDATE wallets SET low_balance_threshold = %s, updated_at = now() WHERE tenant_id = %s",
        (threshold, tenant_id)
    )
    invalidate_wallet_cache(tenant_id)


def credit_balance(tenant_id: int, amount: Decimal, description: str, ref_id: str | None = None) -> Decimal:
    """Atomic wallet credit. Uses SELECT FOR UPDATE transaction."""
    with db.transaction() as conn:
        wallet = conn.execute(
            "SELECT id, balance FROM wallets WHERE tenant_id = %s FOR UPDATE",
            (tenant_id,)
        ).fetchone()
        if not wallet:
            raise ValueError(f"Wallet for tenant {tenant_id} not found.")
        
        balance = Decimal(wallet["balance"])
        new_balance = balance + amount
        
        conn.execute(
            "UPDATE wallets SET balance = %s, updated_at = now() WHERE tenant_id = %s",
            (new_balance, tenant_id)
        )
        
        conn.execute(
            """
            INSERT INTO wallet_transactions (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
            VALUES (%s, %s, 'credit', %s, %s, %s, %s)
            """,
            (wallet["id"], tenant_id, amount, new_balance, description, ref_id)
        )
    invalidate_wallet_cache(tenant_id)
    return new_balance


def deduct_balance(tenant_id: int, amount: Decimal, description: str, ref_id: str | None = None) -> Decimal:
    """Atomic wallet deduction. Uses SELECT FOR UPDATE transaction. Throws InsufficientBalanceError."""
    with db.transaction() as conn:
        wallet = conn.execute(
            "SELECT id, balance FROM wallets WHERE tenant_id = %s FOR UPDATE",
            (tenant_id,)
        ).fetchone()
        if not wallet:
            raise ValueError(f"Wallet for tenant {tenant_id} not found.")
        
        balance = Decimal(wallet["balance"])
        if balance < amount:
            raise InsufficientBalanceError("Insufficient wallet balance for this operation.")
            
        new_balance = balance - amount
        
        conn.execute(
            "UPDATE wallets SET balance = %s, updated_at = now() WHERE tenant_id = %s",
            (new_balance, tenant_id)
        )
        
        conn.execute(
            """
            INSERT INTO wallet_transactions (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
            VALUES (%s, %s, 'debit', %s, %s, %s, %s)
            """,
            (wallet["id"], tenant_id, amount, new_balance, description, ref_id)
        )
    invalidate_wallet_cache(tenant_id)
    return new_balance


def refund_balance(tenant_id: int, amount: Decimal, description: str, ref_id: str | None = None) -> Decimal:
    """Atomic wallet refund (adds balance as type=refund)."""
    with db.transaction() as conn:
        wallet = conn.execute(
            "SELECT id, balance FROM wallets WHERE tenant_id = %s FOR UPDATE",
            (tenant_id,)
        ).fetchone()
        if not wallet:
            raise ValueError(f"Wallet for tenant {tenant_id} not found.")
        
        balance = Decimal(wallet["balance"])
        new_balance = balance + amount
        
        conn.execute(
            "UPDATE wallets SET balance = %s, updated_at = now() WHERE tenant_id = %s",
            (new_balance, tenant_id)
        )
        
        conn.execute(
            """
            INSERT INTO wallet_transactions (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
            VALUES (%s, %s, 'refund', %s, %s, %s, %s)
            """,
            (wallet["id"], tenant_id, amount, new_balance, description, ref_id)
        )
    invalidate_wallet_cache(tenant_id)
    return new_balance

def get_transactions(tenant_id: int, page: int = 1, per_page: int = 20) -> list[dict[str, Any]]:
    """Fetch paginated transactions by tenant_id (was wallet_id — standardized so
    count and data queries use the same key, fixing pagination total mismatch)."""
    offset = (page - 1) * per_page
    return db.query(
        "SELECT * FROM wallet_transactions WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (tenant_id, per_page, offset)
    )

def get_transactions_by_tenant(tenant_id: int, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM wallet_transactions WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (tenant_id, limit, offset)
    )

def get_transactions_count(tenant_id: int) -> int:
    row = db.query_one("SELECT COUNT(*) FROM wallet_transactions WHERE tenant_id = %s", (tenant_id,))
    return int(row["count"]) if row else 0

def create_wallet(tenant_id: int) -> int:
    return db.insert_returning_id(
        "INSERT INTO wallets (tenant_id, balance, currency, low_balance_threshold) VALUES (%s, 0.0000, 'BDT', 50.0000) RETURNING id",
        (tenant_id,)
    )

def update_balance(tenant_id: int, new_balance: Decimal) -> None:
    db.execute(
        "UPDATE wallets SET balance = %s, updated_at = now() WHERE tenant_id = %s",
        (new_balance, tenant_id)
    )
    invalidate_wallet_cache(tenant_id)

# --- Payment Intents CRUD ---

def create_payment_intent(tenant_id: int, gateway: str, amount: Decimal, gateway_ref: str | None = None) -> int:
    return db.insert_returning_id(
        """
        INSERT INTO payment_intents (tenant_id, gateway, amount, gateway_ref, status)
        VALUES (%s, %s, %s, %s, 'pending')
        RETURNING id
        """,
        (tenant_id, gateway, amount, gateway_ref)
    )

def update_payment_intent_status(intent_id: int, status: str, gateway_ref: str | None = None, payload: dict | None = None) -> None:
    import json
    db.execute(
        """
        UPDATE payment_intents
        SET status = %s, gateway_ref = COALESCE(%s, gateway_ref), payload = %s, updated_at = now()
        WHERE id = %s
        """,
        (status, gateway_ref, json.dumps(payload) if payload else None, intent_id)
    )

def get_payment_intent_by_ref(gateway_ref: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM payment_intents WHERE gateway_ref = %s", (gateway_ref,))

def get_payment_intent_by_id(intent_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM payment_intents WHERE id = %s", (intent_id,))
