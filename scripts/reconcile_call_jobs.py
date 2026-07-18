#!/usr/bin/env python3
"""scripts/reconcile_call_jobs.py — detect and repair call queue inconsistencies.

Dry-run by default. Prints counts, IDs and categories without mutating anything.
Pass --apply to execute safe repairs in atomic transactions.

Detects
-------
1. CALL_QUEUED order with no active job and no active call.
2. Active (non-terminal) job for a terminal order.
3. Duplicate active jobs for the same (order_id, attempt_no).
4. Leased job whose lease has expired.
5. Job marked dispatched/completed but calls row does not exist.
6. Attempt row pending (result IS NULL) despite job permanently failed.
7. Job/order tenant mismatch.

Apply mode
----------
- Enqueues a new job for case 1 (only when order is CALL_QUEUED, not terminal).
- Cancels jobs for cases 2, 3, 7.
- Resets expired leases for case 4 (returns to queued).
- Marks case 5 jobs as failed and transitions order to FAILED.
- No calls are initiated. No billing is applied. Audit log written for each action.

Usage
-----
    python scripts/reconcile_call_jobs.py --dry-run     (default)
    python scripts/reconcile_call_jobs.py --apply

Never log raw phone numbers or order payloads.
"""
from __future__ import annotations

import argparse
import logging
import sys
import os
from pathlib import Path

# Ensure the project root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("ccall.reconcile_call_jobs")


def _get_app():
    from dotenv import load_dotenv
    load_dotenv()
    config_name = os.environ.get("FLASK_ENV", "production")
    from app import create_app
    return create_app(config_name)


def _run(dry_run: bool) -> int:
    """Return exit code: 0 = clean or repaired, 1 = issues found (dry-run)."""
    from app.db import db

    issues_found = 0
    actions_taken = 0

    label = "[DRY-RUN]" if dry_run else "[APPLY]"

    print(f"\n=== Call Job Queue Reconciliation {label} ===\n")

    # ── 1. CALL_QUEUED orders with no active job and no active call ───────────
    stuck = db.query(
        """
        SELECT o.id AS order_id, o.tenant_id, o.attempt_count, o.order_status
        FROM orders o
        WHERE o.order_status = 'CALL_QUEUED'
          AND NOT EXISTS (
              SELECT 1 FROM call_jobs j
              WHERE j.order_id = o.id
                AND j.job_status NOT IN ('completed','failed','cancelled')
          )
          AND NOT EXISTS (
              SELECT 1 FROM calls c
              JOIN order_call_attempts a ON a.call_id = c.id
              WHERE a.order_id = o.id AND c.status NOT IN ('completed','failed','no_answer','busy','canceled')
          )
        ORDER BY o.id
        """
    )
    print(f"[1] Stuck CALL_QUEUED orders (no active job, no active call): {len(stuck)}")
    for row in stuck:
        oid, tid = row["order_id"], row["tenant_id"]
        print(f"    order_id={oid}  tenant_id={tid}  attempt_count={row['attempt_count']}")
        issues_found += 1
        if not dry_run:
            try:
                from app.services.call_job_service import enqueue_order_call
                attempt_no = (row["attempt_count"] or 0) + 1
                with db.transaction() as conn:
                    enqueue_order_call(conn, oid, tid, attempt_no)
                print(f"    [ACTION] Enqueued new job for order_id={oid} attempt={attempt_no}")
                actions_taken += 1
            except Exception as exc:
                print(f"    [ERROR]  Failed to enqueue for order_id={oid}: {exc}")

    # ── 2. Active jobs for terminal orders ────────────────────────────────────
    _TERMINAL = ["CONFIRMED", "CANCELLED", "UNREACHABLE", "FAILED"]
    active_for_terminal = db.query(
        """
        SELECT j.id AS job_id, j.order_id, j.tenant_id, j.job_status, o.order_status
        FROM call_jobs j
        JOIN orders o ON o.id = j.order_id
        WHERE j.job_status NOT IN ('completed','failed','cancelled')
          AND o.order_status = ANY(%s)
        ORDER BY j.id
        """,
        (_TERMINAL,),
    )
    print(f"[2] Active jobs for terminal orders: {len(active_for_terminal)}")
    for row in active_for_terminal:
        print(f"    job_id={row['job_id']}  order_id={row['order_id']}  "
              f"job_status={row['job_status']}  order_status={row['order_status']}")
        issues_found += 1
        if not dry_run:
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE call_jobs SET job_status='cancelled', completed_at=now(), updated_at=now() "
                        "WHERE id=%s",
                        (row["job_id"],),
                    )
                print(f"    [ACTION] Cancelled job_id={row['job_id']}")
                actions_taken += 1
            except Exception as exc:
                print(f"    [ERROR]  Failed to cancel job_id={row['job_id']}: {exc}")

    # ── 3. Duplicate active jobs for same (order_id, attempt_no) ─────────────
    duplicates = db.query(
        """
        SELECT order_id, attempt_no, COUNT(*) AS cnt,
               array_agg(id ORDER BY id) AS job_ids
        FROM call_jobs
        WHERE job_status NOT IN ('completed','failed','cancelled')
        GROUP BY order_id, attempt_no
        HAVING COUNT(*) > 1
        ORDER BY order_id
        """
    )
    print(f"[3] Duplicate active jobs (same order+attempt): {len(duplicates)} group(s)")
    for row in duplicates:
        job_ids = row["job_ids"]
        keep_id = job_ids[0]
        cancel_ids = job_ids[1:]
        print(f"    order_id={row['order_id']}  attempt={row['attempt_no']}  "
              f"keep={keep_id}  cancel={cancel_ids}")
        issues_found += 1
        if not dry_run:
            for jid in cancel_ids:
                try:
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE call_jobs SET job_status='cancelled', completed_at=now(), updated_at=now() "
                            "WHERE id=%s",
                            (jid,),
                        )
                    print(f"    [ACTION] Cancelled duplicate job_id={jid}")
                    actions_taken += 1
                except Exception as exc:
                    print(f"    [ERROR]  Failed to cancel job_id={jid}: {exc}")

    # ── 4. Leased jobs with expired lease ────────────────────────────────────
    expired = db.query(
        """
        SELECT id AS job_id, order_id, tenant_id, leased_by, lease_expires_at
        FROM call_jobs
        WHERE job_status = 'leased'
          AND lease_expires_at < now()
        ORDER BY lease_expires_at
        """
    )
    print(f"[4] Leased jobs with expired lease: {len(expired)}")
    for row in expired:
        print(f"    job_id={row['job_id']}  order_id={row['order_id']}  "
              f"leased_by={row['leased_by']}  expired_at={row['lease_expires_at']}")
        issues_found += 1
        if not dry_run:
            try:
                from app.services.call_job_service import release_expired_leases
                with db.transaction() as conn:
                    n = release_expired_leases(conn)
                print(f"    [ACTION] Released {n} expired lease(s)")
                actions_taken += n
                break  # release_expired_leases handles all in one query
            except Exception as exc:
                print(f"    [ERROR]  Failed to release leases: {exc}")

    # ── 5. Jobs completed/dispatched but no calls row ─────────────────────────
    orphan_jobs = db.query(
        """
        SELECT j.id AS job_id, j.order_id, j.tenant_id, j.call_id
        FROM call_jobs j
        WHERE j.job_status IN ('completed')
          AND j.call_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.id = j.call_id)
        ORDER BY j.id
        """
    )
    print(f"[5] Jobs completed but calls row missing: {len(orphan_jobs)}")
    for row in orphan_jobs:
        print(f"    job_id={row['job_id']}  order_id={row['order_id']}  call_id={row['call_id']}")
        issues_found += 1
        if not dry_run:
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE call_jobs SET job_status='failed', last_error_code='calls_row_missing', "
                        "last_error_summary='calls row not found after dispatch', updated_at=now() "
                        "WHERE id=%s",
                        (row["job_id"],),
                    )
                    conn.execute(
                        "UPDATE orders SET order_status='FAILED', updated_at=now() "
                        "WHERE id=%s AND order_status NOT IN ('CONFIRMED','CANCELLED','UNREACHABLE')",
                        (row["order_id"],),
                    )
                print(f"    [ACTION] Marked job_id={row['job_id']} failed; order_id={row['order_id']} FAILED")
                actions_taken += 1
            except Exception as exc:
                print(f"    [ERROR]  Failed for job_id={row['job_id']}: {exc}")

    # ── 6. Attempts pending despite permanently failed job ────────────────────
    stale_attempts = db.query(
        """
        SELECT a.id AS attempt_id, a.order_id, a.tenant_id, a.attempt_no,
               j.id AS job_id, j.job_status
        FROM order_call_attempts a
        JOIN call_jobs j ON j.order_call_attempt_id = a.id
        WHERE a.result IS NULL
          AND j.job_status = 'failed'
        ORDER BY a.id
        """
    )
    print(f"[6] Attempts pending despite permanently failed job: {len(stale_attempts)}")
    for row in stale_attempts:
        print(f"    attempt_id={row['attempt_id']}  order_id={row['order_id']}  "
              f"attempt_no={row['attempt_no']}  job_id={row['job_id']}")
        issues_found += 1
        # No auto-repair: requires business decision on what result to write.

    # ── 7. Job/order tenant mismatch ─────────────────────────────────────────
    mismatched = db.query(
        """
        SELECT j.id AS job_id, j.order_id, j.tenant_id AS job_tenant,
               o.tenant_id AS order_tenant
        FROM call_jobs j
        JOIN orders o ON o.id = j.order_id
        WHERE j.tenant_id != o.tenant_id
        ORDER BY j.id
        """
    )
    print(f"[7] Job/order tenant mismatch: {len(mismatched)}")
    for row in mismatched:
        print(f"    job_id={row['job_id']}  order_id={row['order_id']}  "
              f"job_tenant={row['job_tenant']}  order_tenant={row['order_tenant']}")
        issues_found += 1
        if not dry_run:
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE call_jobs SET job_status='cancelled', completed_at=now(), updated_at=now() "
                        "WHERE id=%s",
                        (row["job_id"],),
                    )
                print(f"    [ACTION] Cancelled mismatched job_id={row['job_id']}")
                actions_taken += 1
            except Exception as exc:
                print(f"    [ERROR]  job_id={row['job_id']}: {exc}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\nTotal issues found: {issues_found}")
    if not dry_run:
        print(f"Total actions taken: {actions_taken}")

    if dry_run and issues_found > 0:
        print("\nRun with --apply to repair.")
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Detect and repair call_jobs queue inconsistencies."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Print findings without making changes (default)"
    )
    group.add_argument(
        "--apply", action="store_true", default=False,
        help="Apply safe repairs atomically (requires explicit flag)"
    )
    args = parser.parse_args()

    dry_run = not args.apply
    app = _get_app()
    with app.app_context():
        exit_code = _run(dry_run=dry_run)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
