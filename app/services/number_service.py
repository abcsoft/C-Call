from __future__ import annotations
 
import logging
import re
from decimal import Decimal
from typing import Any
from app.db import db
from app.extensions import redis_ext
from app.models.phone_number import (
    add_phone_number,
    get_phone_number_by_id,
    get_phone_number_by_digits
)

logger = logging.getLogger("ccall")

def validate_pool_phone(number: str) -> str:
    """Validates a CLI/caller-ID pool number. Accepts BD mobile, BD
    landline/PBX/virtual numbers, and generic E.164 — broader than
    customer-facing mobile validation (see order_service.py) because SIP
    providers commonly issue non-mobile-range numbers (e.g. 09602111380)
    as caller ID, not just 01[3-9] mobile numbers."""
    cleaned = re.sub(r"[^\d+]", "", str(number))

    # 1. BD mobile: 01[3-9] + 8 digits = 11 digits total
    bd_mobile = re.match(r"^(?:\+88|88)?(01[3-9]\d{8})$", cleaned)
    if bd_mobile:
        return f"+88{bd_mobile.group(1)}"

    # 2. BD landline / PBX / virtual: 0 + 8 to 10 more digits
    #    (covers 02-xxxxxxx Dhaka, 031-xxxxxx Chattogram,
    #    09xxxxxxxxx virtual/PBX ranges like provider-issued CLIs, etc.)
    bd_landline = re.match(r"^(?:\+88|88)?(0\d{8,10})$", cleaned)
    if bd_landline:
        return f"+88{bd_landline.group(1)}"

    # 3. Generic E.164
    e164_match = re.match(r"^\+[1-9]\d{9,14}$", cleaned)
    if e164_match:
        return cleaned

    raise ValueError(
        "Invalid phone number format. Must be a valid Bangladesh "
        "mobile, landline/PBX number, or E.164 number."
    )

def select_caller_id(tenant_id: int) -> str | None:
    """Select the caller ID (CLI) for a call:
    1. Check for dedicated assigned primary numbers.
    2. Fall back to round-robin among active shared numbers.
    3. Return None if no numbers are available (call_runner will use global SIP_USER).
    """
    # 1. Check customer_number_assignments for tenant: find WHERE is_primary=true AND phone_numbers.status='active'
    dedicated_row = db.query_one(
        """
        SELECT n.number
        FROM phone_numbers n
        JOIN customer_number_assignments a ON n.id = a.phone_number_id
        WHERE a.tenant_id = %s AND a.unassigned_at IS NULL AND n.status = 'active'
        ORDER BY a.is_primary DESC, a.assigned_at DESC
        LIMIT 1
        """,
        (tenant_id,)
    )
    if dedicated_row:
        return dedicated_row["number"]

    # 2. If no dedicated: get shared numbers (phone_numbers WHERE number_type='shared' AND status='active')
    shared_numbers = db.query(
        "SELECT number FROM phone_numbers WHERE number_type = 'shared' AND status = 'active' ORDER BY id ASC"
    )
    if not shared_numbers:
        logger.warning(f"No shared CLI phone numbers available for tenant {tenant_id}")
        return None

    # 3. Round-robin across shared numbers using Redis: INCR "shared_pool_idx:{tenant_id}" % len(shared_numbers)
    try:
        redis_key = f"shared_pool_idx:{tenant_id}"
        idx = redis_ext.client.incr(redis_key)
        selected = shared_numbers[idx % len(shared_numbers)]
        return selected["number"]
    except Exception as exc:
        logger.error(f"Redis round-robin failed for tenant {tenant_id}: {exc}")
        return shared_numbers[0]["number"]

def get_sip_trunk_for_caller_id(caller_id: str) -> dict[str, str] | None:
    """Look up the SIP trunk (domain/port/user/password) backing a caller-ID
    number, via phone_numbers.provider_id -> sip_providers. The number pool
    admin page (sip_providers / phone_numbers) and the actual REGISTER/INVITE
    credentials used by call_runner.sip_config() used to be two disconnected
    stores — a number could be added to the pool and selected as caller_id
    without call_runner ever learning that provider's auth credentials. This
    closes that gap: callers of select_caller_id() should also call this to
    get a sip_override dict to pass into call_runner.start_call()."""
    row = db.query_one(
        """
        SELECT p.sip_domain, p.sip_port, p.sip_user, p.sip_password
        FROM phone_numbers n
        JOIN sip_providers p ON n.provider_id = p.id
        WHERE n.number = %s AND p.status = 'active'
        """,
        (caller_id,)
    )
    if not row or not row.get("sip_domain"):
        return None
    from app.models.sip_provider import decrypt_password
    return {
        "SIP_DOMAIN": row["sip_domain"],
        "SIP_PORT": str(row.get("sip_port") or 5060),
        "SIP_USER": row["sip_user"],
        "SIP_PASSWORD": decrypt_password(row["sip_password"]) if row.get("sip_password") else "",
    }

def get_sip_provider_id_for_caller_id(caller_id: str) -> int | None:
    """Which sip_providers row backs this caller-ID number — for tagging
    calls.sip_gateway_id in the Live Call Monitor. Kept separate from
    get_sip_trunk_for_caller_id() since that dict gets dumped wholesale
    into a subprocess's environment variables (direct calls) — adding a
    non-SIP_* key there would leak into the child process's env."""
    row = db.query_one(
        "SELECT p.id FROM phone_numbers n JOIN sip_providers p ON n.provider_id = p.id WHERE n.number = %s",
        (caller_id,)
    )
    return row["id"] if row else None

def assign_number(tenant_id: int | None, phone_number_id: int, number_type: str) -> dict:
    """Assign a phone number to a tenant as a dedicated or shared channel."""
    with db.transaction() as conn:
        # Check: number exists and status=active
        number = conn.execute("SELECT * FROM phone_numbers WHERE id = %s FOR UPDATE", (phone_number_id,)).fetchone()
        if not number:
            raise ValueError("Phone number not found.")
        if number["status"] != "active":
            raise ValueError("Phone number is not active.")

        if number_type == "shared":
            # Unassign any active assignment for that phone_number_id
            conn.execute(
                """
                UPDATE customer_number_assignments
                SET unassigned_at = now()
                WHERE phone_number_id = %s AND unassigned_at IS NULL
                """,
                (phone_number_id,)
            )
            # Update phone_numbers.number_type
            conn.execute("UPDATE phone_numbers SET number_type = 'shared' WHERE id = %s", (phone_number_id,))
        elif number_type == "dedicated":
            if not tenant_id:
                raise ValueError("Tenant ID is required for dedicated number assignment.")
            
            # Ensure no other active assignment exists
            existing = conn.execute(
                "SELECT * FROM customer_number_assignments WHERE phone_number_id = %s AND unassigned_at IS NULL FOR UPDATE",
                (phone_number_id,)
            ).fetchone()
            
            if existing:
                if existing["tenant_id"] != tenant_id:
                    raise ValueError("Phone number is already assigned to a different tenant.")
                # Already assigned to the same tenant, do not insert
            else:
                # Check if tenant has any active primary dedicated numbers
                has_primary = conn.execute(
                    "SELECT id FROM customer_number_assignments WHERE tenant_id = %s AND is_primary = true AND unassigned_at IS NULL",
                    (tenant_id,)
                ).fetchone()
                is_primary = not bool(has_primary)

                conn.execute(
                    """
                    INSERT INTO customer_number_assignments (tenant_id, phone_number_id, is_primary, assigned_at)
                    VALUES (%s, %s, %s, now())
                    """,
                    (tenant_id, phone_number_id, is_primary)
                )

            # Update phone_numbers.number_type if changing
            conn.execute("UPDATE phone_numbers SET number_type = 'dedicated' WHERE id = %s", (phone_number_id,))
        else:
            raise ValueError(f"Invalid number type: {number_type}")

        # Retrieve and return updated number record
        updated_number = conn.execute(
            """
            SELECT n.*, a.tenant_id, a.is_primary
            FROM phone_numbers n
            LEFT JOIN customer_number_assignments a ON n.id = a.phone_number_id AND a.unassigned_at IS NULL
            WHERE n.id = %s
            """,
            (phone_number_id,)
        ).fetchone()
        return dict(updated_number)

def unassign_number(tenant_id: int, phone_number_id: int) -> None:
    """Set unassigned_at = now() for number assignment. Do NOT delete history."""
    db.execute(
        """
        UPDATE customer_number_assignments
        SET unassigned_at = now()
        WHERE tenant_id = %s AND phone_number_id = %s AND unassigned_at IS NULL
        """,
        (tenant_id, phone_number_id)
    )

def add_to_pool(number: str, provider_id: int, number_type: str, monthly_cost: Decimal) -> dict:
    """Validate, check unique constraint, and insert phone number into the pool."""
    normalized = validate_pool_phone(number)
    
    existing = get_phone_number_by_digits(normalized)
    if existing:
        raise ValueError("Phone number already exists in the pool.")

    number_id = add_phone_number(
        number=normalized,
        provider_id=provider_id,
        number_type=number_type,
        status="active",
        monthly_cost=monthly_cost
    )
    res = get_phone_number_by_id(number_id)
    if not res:
        raise ValueError("Failed to retrieve newly added phone number.")
    return res

def get_pool(page: int = 1, per_page: int = 20, filters: dict | None = None) -> dict[str, Any]:
    """Retrieve filtered, paginated phone number pool joined with assignments, tenants, and call counts."""
    filters = filters or {}
    where_clauses = []
    params = []

    status_filter = filters.get("status")
    if status_filter == "unassigned":
        where_clauses.append("n.number_type = 'dedicated' AND n.status = 'active' AND a.id IS NULL")
    elif status_filter in ("active", "inactive"):
        where_clauses.append("n.status = %s")
        params.append(status_filter)

    number_type_filter = filters.get("number_type")
    if number_type_filter:
        where_clauses.append("n.number_type = %s")
        params.append(number_type_filter)

    search = filters.get("search")
    if search:
        where_clauses.append("(n.number ILIKE %s OR t.business_name ILIKE %s)")
        params.append(f"%{search}%")
        params.append(f"%{search}%")

    where_str = ""
    if where_clauses:
        where_str = "WHERE " + " AND ".join(where_clauses)

    count_query = f"""
        SELECT COUNT(DISTINCT n.id) as total
        FROM phone_numbers n
        LEFT JOIN customer_number_assignments a ON n.id = a.phone_number_id AND a.unassigned_at IS NULL
        LEFT JOIN tenants t ON a.tenant_id = t.id
        {where_str}
    """
    total_row = db.query_one(count_query, tuple(params))
    total = total_row["total"] if total_row else 0

    offset = (page - 1) * per_page
    data_query = f"""
        SELECT
            n.*,
            p.name as provider_name,
            t.business_name,
            a.tenant_id,
            a.is_primary,
            COALESCE(c.call_count_today, 0) as call_count_today
        FROM phone_numbers n
        LEFT JOIN sip_providers p ON n.provider_id = p.id
        LEFT JOIN customer_number_assignments a ON n.id = a.phone_number_id AND a.unassigned_at IS NULL
        LEFT JOIN tenants t ON a.tenant_id = t.id
        LEFT JOIN (
            SELECT caller_id, COUNT(*) AS call_count_today
            FROM calls
            WHERE started_at >= CURRENT_DATE
            GROUP BY caller_id
        ) c ON c.caller_id = n.number
        {where_str}
        ORDER BY n.id DESC
        LIMIT %s OFFSET %s
    """
    data_params = list(params) + [per_page, offset]
    data = db.query(data_query, tuple(data_params))

    return {
        "data": data,
        "total": total
    }


def get_pool_stats() -> dict[str, int]:
    """Counts for the numbers page stats row."""
    row = db.query_one(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE n.status = 'active') AS active,
            COUNT(*) FILTER (WHERE n.number_type = 'dedicated') AS dedicated,
            COUNT(*) FILTER (WHERE n.number_type = 'shared') AS shared,
            COUNT(*) FILTER (WHERE n.number_type = 'dedicated' AND n.status = 'active' AND a.id IS NULL) AS unassigned
        FROM phone_numbers n
        LEFT JOIN customer_number_assignments a ON n.id = a.phone_number_id AND a.unassigned_at IS NULL
        """
    ) or {}
    return {
        "total": int(row.get("total") or 0),
        "active": int(row.get("active") or 0),
        "dedicated": int(row.get("dedicated") or 0),
        "shared": int(row.get("shared") or 0),
        "unassigned": int(row.get("unassigned") or 0),
    }


def bulk_import_numbers(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Import multiple phone numbers from parsed CSV rows.

    Each row: {number, provider_id, number_type, monthly_cost}. Invalid or
    duplicate rows are skipped (not fatal) — caller gets a summary."""
    imported: list[str] = []
    skipped: list[dict[str, str]] = []

    for row in rows:
        raw_number = str(row.get("number", "")).strip()
        try:
            provider_id = int(row.get("provider_id") or 0)
            number_type = (row.get("number_type") or "shared").strip().lower()
            if number_type not in ("shared", "dedicated"):
                number_type = "shared"
            monthly_cost = Decimal(str(row.get("monthly_cost") or "0"))
            if not provider_id:
                raise ValueError("provider_id is required")
            add_to_pool(raw_number, provider_id, number_type, monthly_cost)
            imported.append(raw_number)
        except Exception as exc:
            skipped.append({"number": raw_number, "reason": str(exc)})

    return {"imported": imported, "skipped": skipped, "imported_count": len(imported), "skipped_count": len(skipped)}
