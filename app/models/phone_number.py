from __future__ import annotations

from typing import Any
from decimal import Decimal
from app.db import db

def add_phone_number(number: str, provider_id: int, number_type: str = "shared", status: str = "active", monthly_cost: Decimal = Decimal("0.0")) -> int:
    return db.insert_returning_id(
        """
        INSERT INTO phone_numbers (number, provider_id, number_type, status, monthly_cost)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (number, provider_id, number_type, status, monthly_cost)
    )

def get_phone_number_by_id(number_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM phone_numbers WHERE id = %s", (number_id,))

def get_phone_number_by_digits(number: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM phone_numbers WHERE number = %s", (number,))

def get_all_phone_numbers() -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT n.*, p.name as provider_name, a.tenant_id, t.business_name
        FROM phone_numbers n
        LEFT JOIN sip_providers p ON n.provider_id = p.id
        LEFT JOIN customer_number_assignments a ON n.id = a.phone_number_id AND a.unassigned_at IS NULL
        LEFT JOIN tenants t ON a.tenant_id = t.id
        ORDER BY n.number ASC
        """
    )

def assign_dedicated_number(phone_number_id: int, tenant_id: int, is_primary: bool = True) -> int:
    with db.transaction() as conn:
        # Check if already assigned to someone else
        existing = conn.execute(
            "SELECT id FROM customer_number_assignments WHERE phone_number_id = %s AND unassigned_at IS NULL",
            (phone_number_id,)
        ).fetchone()
        if existing:
            raise ValueError("Phone number is already assigned to a tenant.")
            
        # Update number type to dedicated
        conn.execute("UPDATE phone_numbers SET number_type = 'dedicated' WHERE id = %s", (phone_number_id,))
        
        # Insert assignment
        cursor = conn.execute(
            """
            INSERT INTO customer_number_assignments (tenant_id, phone_number_id, is_primary, assigned_at)
            VALUES (%s, %s, %s, now())
            RETURNING id
            """,
            (tenant_id, phone_number_id, is_primary)
        )
        return cursor.fetchone()["id"]

def unassign_dedicated_number(phone_number_id: int, tenant_id: int) -> None:
    db.execute(
        """
        UPDATE customer_number_assignments
        SET unassigned_at = now()
        WHERE phone_number_id = %s AND tenant_id = %s AND unassigned_at IS NULL
        """,
        (phone_number_id, tenant_id)
    )

def get_numbers_assigned_to_tenant(tenant_id: int) -> list[dict[str, Any]]:
    return db.query(
        """
        SELECT n.*
        FROM phone_numbers n
        JOIN customer_number_assignments a ON n.id = a.phone_number_id
        WHERE a.tenant_id = %s AND a.unassigned_at IS NULL AND n.status = 'active'
        """,
        (tenant_id,)
    )

def get_shared_numbers() -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM phone_numbers WHERE number_type = 'shared' AND status = 'active' ORDER BY id ASC"
    )

def update_phone_number_status(number_id: int, status: str) -> None:
    db.execute("UPDATE phone_numbers SET status = %s WHERE id = %s", (status, number_id))

def update_phone_number(number_id: int, number_type: str, status: str, monthly_cost: Decimal, provider_id: int | None = None) -> None:
    if provider_id:
        db.execute(
            """
            UPDATE phone_numbers
            SET number_type = %s, status = %s, monthly_cost = %s, provider_id = %s
            WHERE id = %s
            """,
            (number_type, status, monthly_cost, provider_id, number_id)
        )
    else:
        db.execute(
            """
            UPDATE phone_numbers
            SET number_type = %s, status = %s, monthly_cost = %s
            WHERE id = %s
            """,
            (number_type, status, monthly_cost, number_id)
        )
