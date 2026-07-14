# E:\c-call-ivr\migrations\run_migrations.py
"""Apply migrations/*.sql against DATABASE_URL, in filename order, once each.

Tracks applied migrations in a `schema_migrations` table so re-running is
a no-op for files already applied. Each migration runs in its own
transaction — a failure rolls back that file only, leaving earlier
migrations committed.

A PostgreSQL advisory lock (key 99999) prevents concurrent migration runs.
The lock is session-scoped and released automatically on disconnect.

Usage:
    python migrations/run_migrations.py [--dry-run]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get("DATABASE_URL")

# Dedicated advisory lock key for migrations — must not collide with
# APScheduler lock (12345) or monitor lock (54321).
_MIGRATION_LOCK_KEY = 99999


def _ensure_tracking_table(conn: "psycopg.Connection") -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _applied_migrations(conn: "psycopg.Connection") -> set[str]:
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def main() -> None:
    if not DATABASE_URL:
        print("DATABASE_URL is not set. Set it in .env before running migrations.")
        sys.exit(1)

    dry_run = "--dry-run" in sys.argv
    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        print("No migration files found.")
        return

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        # ── Acquire exclusive advisory lock before touching schema ────────────
        # pg_try_advisory_lock is non-blocking: returns false immediately if
        # another session holds the lock (e.g. parallel CI run or admin op).
        # The lock is automatically released when the connection closes.
        if dry_run:
            # Dry-run inspects state without modifying schema — skip lock so
            # operators can check pending migrations on a live system.
            pass
        else:
            row = conn.execute(
                "SELECT pg_try_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,)
            ).fetchone()
            acquired = row[0] if row else False
            if not acquired:
                print(
                    f"ERROR: Another migration process is running "
                    f"(advisory lock {_MIGRATION_LOCK_KEY} held). "
                    "Wait for it to finish, or check for dead sessions with:\n"
                    "  SELECT pid, query, state FROM pg_stat_activity "
                    "  WHERE query LIKE '%pg_advisory_lock%';"
                )
                sys.exit(2)
            print(f"Acquired migration lock (key={_MIGRATION_LOCK_KEY})")
        # ─────────────────────────────────────────────────────────────────────

        with conn.transaction():
            _ensure_tracking_table(conn)

        applied = _applied_migrations(conn)

        pending = [f for f in sql_files if f.name not in applied]
        if not pending:
            print("Nothing to apply — all migrations already up to date.")
            return

        print(f"Pending migrations: {[f.name for f in pending]}")
        if dry_run:
            print("--dry-run: not applying.")
            return

        for path in pending:
            print(f"Applying {path.name} ...")
            sql = path.read_text(encoding="utf-8")
            try:
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s)",
                        (path.name,),
                    )
            except Exception as exc:
                print(f"FAILED on {path.name}: {exc}")
                # Advisory lock released automatically on connection close.
                sys.exit(1)
            print(f"  OK: {path.name}")

    # Advisory lock released automatically when `conn` closes (context exit).
    print("All pending migrations applied.")


if __name__ == "__main__":
    main()
