# C-Call — Database Test Report

| Backend | Scope & Command | Status |
|---|---|---|
| PostgreSQL (`ccall` & `ccall_test`) | `migrations/run_migrations.py` (`001`–`028` applied idempotently with advisory lock `99999`) | **PASS** |
| PostgreSQL Integration & Isolation | `pytest tests/unit/test_tenant_isolation.py tests/integration/test_order_api.py tests/integration/test_call_queue_integration.py` | **PASS** |
| SQLite (`schema.sql`) | `pytest tests/unit/test_v2_security_and_reliability.py -k test_sqlite_legacy_schema_independent` (fresh temp `.db`, schema init, read/write) | **PASS** |
| `app/` Runtime on SQLite | `app/db.py` uses `psycopg3` / PostgreSQL-specific SQL (`ON CONFLICT`, `TIMESTAMPTZ`, `TEXT[]`) | **NOT TESTED** (PostgreSQL-only runtime by design) |
