from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.db import db
from app.exceptions import InsufficientBalanceError
from app.models.wallet import get_wallet, invalidate_wallet_cache

logger = logging.getLogger("ccall")


def check_balance(tenant_id: int, required_amount: Decimal) -> bool:
    """Check if the tenant has at least the required amount, using row-level lock."""
    required_amount = Decimal(str(required_amount))
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT balance FROM wallets WHERE tenant_id = %s FOR UPDATE",
            (tenant_id,)
        ).fetchone()
        if not row:
            return False
        return Decimal(str(row["balance"])) >= required_amount


def deduct(tenant_id: int, amount: Decimal, description: str = '', ref_id: str | None = None) -> dict[str, Any]:
    """Atomic balance deduction. Raises InsufficientBalanceError if balance is too low."""
    amount = Decimal(str(amount))
    if amount < 0:
        raise ValueError("Deduction amount cannot be negative.")
    
    with db.transaction() as conn:
        wallet = conn.execute(
            "SELECT id, balance FROM wallets WHERE tenant_id = %s FOR UPDATE",
            (tenant_id,)
        ).fetchone()
        if not wallet:
            raise ValueError(f"Wallet for tenant {tenant_id} not found.")
        
        current_balance = Decimal(str(wallet["balance"]))
        if current_balance < amount:
            raise InsufficientBalanceError(current_balance, amount)
            
        new_balance = current_balance - amount
        
        conn.execute(
            "UPDATE wallets SET balance = %s, updated_at = now() WHERE tenant_id = %s",
            (new_balance, tenant_id)
        )
        
        cursor = conn.execute(
            """
            INSERT INTO wallet_transactions (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
            VALUES (%s, %s, 'debit', %s, %s, %s, %s)
            RETURNING id, wallet_id, tenant_id, type, amount, balance_after, description, ref_id, created_at
            """,
            (wallet["id"], tenant_id, amount, new_balance, description, ref_id)
        )
        txn = cursor.fetchone()

    invalidate_wallet_cache(tenant_id)

    # Check low balance alert asynchronously/post-deduction
    try:
        check_low_balance_alert(tenant_id)
    except Exception as exc:
        logger.error(f"Failed to check low balance alert for tenant {tenant_id}: {exc}")

    return dict(txn)


def credit(tenant_id: int, amount: Decimal, description: str = '', ref_id: str | None = None) -> dict[str, Any]:
    """Atomic balance credit."""
    amount = Decimal(str(amount))
    if amount < 0:
        raise ValueError("Credit amount cannot be negative.")
        
    with db.transaction() as conn:
        wallet = conn.execute(
            "SELECT id, balance FROM wallets WHERE tenant_id = %s FOR UPDATE",
            (tenant_id,)
        ).fetchone()
        if not wallet:
            raise ValueError(f"Wallet for tenant {tenant_id} not found.")
            
        current_balance = Decimal(str(wallet["balance"]))
        new_balance = current_balance + amount
        
        conn.execute(
            "UPDATE wallets SET balance = %s, updated_at = now() WHERE tenant_id = %s",
            (new_balance, tenant_id)
        )
        
        cursor = conn.execute(
            """
            INSERT INTO wallet_transactions (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
            VALUES (%s, %s, 'credit', %s, %s, %s, %s)
            RETURNING id, wallet_id, tenant_id, type, amount, balance_after, description, ref_id, created_at
            """,
            (wallet["id"], tenant_id, amount, new_balance, description, ref_id)
        )
        txn = cursor.fetchone()

    invalidate_wallet_cache(tenant_id)
    return dict(txn)


def refund(tenant_id: int, amount: Decimal, ref_id: str | None = None, description: str = '') -> dict[str, Any]:
    """Atomic balance refund."""
    amount = Decimal(str(amount))
    if amount < 0:
        raise ValueError("Refund amount cannot be negative.")
        
    with db.transaction() as conn:
        wallet = conn.execute(
            "SELECT id, balance FROM wallets WHERE tenant_id = %s FOR UPDATE",
            (tenant_id,)
        ).fetchone()
        if not wallet:
            raise ValueError(f"Wallet for tenant {tenant_id} not found.")
            
        current_balance = Decimal(str(wallet["balance"]))
        new_balance = current_balance + amount
        
        conn.execute(
            "UPDATE wallets SET balance = %s, updated_at = now() WHERE tenant_id = %s",
            (new_balance, tenant_id)
        )
        
        cursor = conn.execute(
            """
            INSERT INTO wallet_transactions (wallet_id, tenant_id, type, amount, balance_after, description, ref_id)
            VALUES (%s, %s, 'refund', %s, %s, %s, %s)
            RETURNING id, wallet_id, tenant_id, type, amount, balance_after, description, ref_id, created_at
            """,
            (wallet["id"], tenant_id, amount, new_balance, description, ref_id)
        )
        txn = cursor.fetchone()

    invalidate_wallet_cache(tenant_id)
    return dict(txn)


def get_balance(tenant_id: int) -> Decimal:
    """Get current wallet balance for the tenant."""
    wallet = get_wallet(tenant_id)
    if not wallet:
        return Decimal("0.0000")
    return Decimal(str(wallet["balance"]))


def get_transactions(tenant_id: int, page: int = 1, per_page: int = 20) -> dict[str, Any]:
    """Retrieve paginated transactions for a tenant."""
    from app.models.wallet import get_transactions as db_get_txns, get_transactions_count

    total = get_transactions_count(tenant_id)
    data = db_get_txns(tenant_id, page=page, per_page=per_page)

    return {
        "data": [dict(t) for t in data],
        "total": total
    }


def check_low_balance_alert(tenant_id: int) -> None:
    """Trigger low balance alert email once every 24h if balance dips below threshold."""
    wallet = get_wallet(tenant_id)
    if not wallet:
        return
        
    balance = Decimal(str(wallet["balance"]))
    threshold = Decimal(str(wallet["low_balance_threshold"] or 50.00))
    
    if balance < threshold:
        from app.extensions import redis_ext
        try:
            redis_client = redis_ext.client
            redis_key = f"low_balance_alert_sent:{tenant_id}"
            
            # Atomic lock/flag check in Redis to ensure alert is sent only once per 24 hours (86400s)
            if not redis_client.get(redis_key):
                from app.models.tenant import get_tenant_by_id
                tenant = get_tenant_by_id(tenant_id)
                if not tenant:
                    return
                    
                tenant_email = tenant["email"]
                business_name = tenant["business_name"]
                
                from flask_mail import Message
                from app.extensions import mail
                
                msg = Message(
                    subject="[C-Call IVR] Low Wallet Balance Alert",
                    recipients=[tenant_email],
                    body=f"Dear {business_name},\n\n"
                         f"Your C-Call wallet balance is low.\n"
                         f"Current Balance: {balance} BDT\n"
                         f"Alert Threshold: {threshold} BDT\n\n"
                         f"Please recharge your wallet from the panel to ensure uninterrupted IVR services.\n\n"
                         f"Best regards,\n"
                         f"C-Call IVR Team"
                )
                mail.send(msg)
                redis_client.setex(redis_key, 86400, "1")
                logger.info(f"Low balance alert email sent to tenant {tenant_id} ({tenant_email})")
        except Exception as exc:
            # Log and suppress all email/redis exceptions to prevent blocking primary business logic
            logger.error(f"Failed to check/send low balance alert for tenant {tenant_id}: {exc}")
