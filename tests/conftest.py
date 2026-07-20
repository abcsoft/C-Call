# tests/conftest.py
"""Pytest fixtures and configuration for tests."""
from __future__ import annotations

import os
import pytest
import psycopg
from app import create_app
from app.db import db as app_db

# TEST_DATABASE_URL must be supplied by the CI/developer environment.
# The placeholder below is intentionally non-connectable so that a missing
# env var causes an immediate connection error rather than silently using
# a committed default password.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://ccall:CHANGE_ME_TEST_DB_PASSWORD@localhost:5432/ccall_test",
)


class _MockPubSub:
    """Minimal PubSub stub for MockRedis — subscribe/unsubscribe are no-ops."""

    def subscribe(self, *channels, **handlers):
        pass

    def unsubscribe(self, *channels):
        pass

    def get_message(self, ignore_subscribe_messages=False, timeout=0):
        return None

    def listen(self):
        return iter([])

    def close(self):
        pass

    def __iter__(self):
        return iter([])


class MockRedis:
    """In-memory Redis mock matching redis-py's API for both redis_ext._client and SESSION_REDIS.

    Flask-Session calls set(name=..., value=..., ex=...) and get(name=...) using keyword args.
    App code calls incr/get/set/delete/setex/expire/ping with positional args.
    Using a module-level class avoids creating 100+ real Redis ConnectionPool objects across
    the test suite, which caused session-Redis lookup hangs on test #97+ in combined runs.
    """

    def __init__(self):
        self.data: dict = {}

    # redis-py uses `name` as the key parameter (not `key`)
    def incr(self, name, amount=1):
        self.data[name] = self.data.get(name, 0) + amount
        return self.data[name]

    def decr(self, name, amount=1):
        self.data[name] = self.data.get(name, 0) - amount
        return self.data[name]

    def get(self, name):
        return self.data.get(name)

    def set(self, name, value, ex=None, px=None, nx=False, xx=False, keepttl=False, get=False, exat=None, pxat=None):
        if nx and name in self.data:
            return None
        if xx and name not in self.data:
            return None
        self.data[name] = value
        return True

    def delete(self, *names):
        count = 0
        for name in names:
            if name in self.data:
                del self.data[name]
                count += 1
        return count

    def setex(self, name, time, value):
        self.data[name] = value
        return True

    def expire(self, name, time, nx=False, xx=False, gt=False, lt=False):
        return True

    def exists(self, *names):
        return sum(1 for n in names if n in self.data)

    def ping(self):
        return True

    def keys(self, pattern="*"):
        return list(self.data.keys())

    def scan_iter(self, match=None, count=None):
        for key in self.data:
            if match is None or key.startswith(match.rstrip("*")):
                yield key

    def hset(self, name, key=None, value=None, mapping=None):
        if name not in self.data or not isinstance(self.data[name], dict):
            self.data[name] = {}
        if mapping:
            self.data[name].update(mapping)
        elif key is not None:
            self.data[name][key] = value
        return 1

    def hget(self, name, key):
        return self.data.get(name, {}).get(key)

    def hgetall(self, name):
        return dict(self.data.get(name, {}))

    def hdel(self, name, *keys):
        h = self.data.get(name, {})
        count = 0
        for k in keys:
            if k in h:
                del h[k]
                count += 1
        return count

    def pubsub(self, **kwargs):
        return _MockPubSub()

    def publish(self, channel, message):
        return 0


def _schema_is_current(conn) -> bool:
    """Return True if ccall_test already has all migration tables including migration 017."""
    with conn.cursor() as cur:
        # Check for migration 015 tables
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name IN ('plan_tts_policies', 'platform_secrets', 'tenant_tts_usage', 'tenants', 'orders')
        """)
        count = cur.fetchone()[0]
        if count < 5:
            return False
        # Check for migration 016 column
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public'
            AND table_name = 'plans'
            AND column_name = 'archived_at'
        """)
        if cur.fetchone()[0] < 1:
            return False
        # Check for migration 017 table
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name = 'tts_usage_reservations'
        """)
        if cur.fetchone()[0] < 1:
            return False
        # Check for migration 018 tables (subscriptions, subscription_events)
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name IN ('subscriptions', 'subscription_events', 'subscription_payments')
        """)
        if cur.fetchone()[0] < 3:
            return False
        # Check for migration 019 column (past_due_ends_at on subscriptions)
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public'
            AND table_name = 'subscriptions'
            AND column_name = 'past_due_ends_at'
        """)
        if cur.fetchone()[0] < 1:
            return False
        # Check for call_jobs table (added in later migration)
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name = 'call_jobs'
        """)
        return cur.fetchone()[0] >= 1


_PLACEHOLDER = "CHANGE_ME_TEST_DB_PASSWORD"


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Drop and recreate public schema, and run migrations in order.
    Skips rebuild if schema is already up to date (speeds up re-runs).
    Pass --force-rebuild to always rebuild.
    """
    import glob
    if _PLACEHOLDER in TEST_DATABASE_URL:
        # TEST_DATABASE_URL not configured — unit tests run, DB tests will fail on connect.
        # Set TEST_DATABASE_URL in .env to run integration tests.
        return
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        if _schema_is_current(conn):
            return  # schema already up to date — skip rebuild

        with conn.cursor() as cur:
            # Terminate any open connections to the test DB so DROP SCHEMA doesn't block
            cur.execute("""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
            """)
            cur.execute("""
                DROP SCHEMA IF EXISTS public CASCADE;
                CREATE SCHEMA public;
                GRANT ALL ON SCHEMA public TO public;
            """)

        migrations_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "migrations"))
        for path in sorted(glob.glob(os.path.join(migrations_dir, "*.sql"))):
            with open(path, "r", encoding="utf-8") as f:
                sql = f.read()
            with conn.transaction():
                conn.execute(sql)


@pytest.fixture
def app():
    """Create Flask application configured for Testing."""
    os.environ["FLASK_ENV"] = "testing"
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ.setdefault("FLASK_SECRET_KEY", "test-secret-key-that-is-long-enough-for-tests-32x")
    # Fernet key for platform_secrets — test only, never use in prod
    os.environ.setdefault(
        "PLATFORM_SECRET_ENCRYPTION_KEY",
        "kf5PjsX2XFRAHr4o5KMstpKlovTExW7PLCg-y6Hqa88="  # test-only Fernet key
    )
    # Inject MockRedis BEFORE create_app so that init_app picks it up as SESSION_REDIS.
    # This prevents real Redis ConnectionPool accumulation across 100+ tests, which caused
    # session-lookup hangs during CSRF validation on combined test runs.
    from app.extensions import redis_ext
    mock_redis_instance = MockRedis()
    redis_ext._client = mock_redis_instance

    flask_app = create_app("testing")
    # Patch Flask-Session's internal client reference to the mock.
    # init_app creates a real Redis client and stores it in session_interface.client;
    # our config patch is too late. Replacing the attribute on the already-created
    # RedisSessionInterface avoids creating real ConnectionPools per test.
    try:
        flask_app.session_interface.client = mock_redis_instance
    except AttributeError:
        pass
    flask_app.config["SESSION_REDIS"] = mock_redis_instance
    flask_app.config["PROPAGATE_EXCEPTIONS"] = True
    yield flask_app
    # Close DB pool
    from app.extensions import db_pool
    if db_pool._pool:
        try:
            db_pool._pool.close()
        except Exception:
            pass
        db_pool._pool = None
    # Reset redis_ext._client so next test's init_app starts clean
    redis_ext._client = None


@pytest.fixture
def client(app):
    """Flask client fixture."""
    return app.test_client()


@pytest.fixture(autouse=True)
def prevent_background_threads(request, monkeypatch):
    """Prevent background threads from executing during tests to avoid connection leaks.
    If marked with real_db, allow real threads.
    Otherwise, run them synchronously.
    """
    if "real_db" in request.keywords:
        return
    import threading
    if not hasattr(threading.Thread, "_original_start"):
        threading.Thread._original_start = threading.Thread.start
        
    _real_start = threading.Thread._original_start

    _LIBRARY_MODULES = (
        "psycopg_pool",
        "psycopg.",
        "asyncio",
        "apscheduler",
        "concurrent.futures",
        "threading",
        "multiprocessing",
    )

    def mock_start(self):
        # Let library threads run as real threads (infinite-loop workers/schedulers).
        # For all other threads: start as a real thread but join immediately to force
        # sequential execution (no concurrent access to the shared mock DB connection).
        # Joining inside mock_start means th.join() calls in test code are instant no-ops.
        target_module = getattr(self._target, "__module__", "") or ""
        is_library = any(target_module.startswith(m) for m in _LIBRARY_MODULES)
        if is_library:
            _real_start(self)
            return
        if self._target:
            _real_start(self)
            if not self.daemon:
                # Join non-daemon threads to enforce sequential execution and prevent
                # concurrent access to the shared mock DB connection.
                # Daemon threads (infinite-loop monitors) are left to run in background.
                self.join()

    monkeypatch.setattr(threading.Thread, "start", mock_start)


@pytest.fixture(autouse=True)
def mock_tts_and_call_runner(request, monkeypatch):
    """Globally mock TTS audio generation, call starting, and Redis to avoid external dependencies."""
    # Skip for pure unit tests that don't use the app fixture
    if "app" not in request.fixturenames:
        return
    request.getfixturevalue("app")
    from app.db import db
    from call_engine import call_runner
    
    # Mock TTS audio generation
    monkeypatch.setattr(
        "app.services.order_service.generate_order_audio",
        lambda tenant_id, text, *args, **kwargs: "audio.ulaw"
    )
    
    # Mock call_runner.start_call to insert call row and return its ID
    def mock_start_call(number, flow_id, name="", *args, **kwargs):
        return db.insert_returning_id(
            "INSERT INTO calls (number, status, direction, talk_seconds) VALUES (%s, 'calling', 'outbound', 0) RETURNING id",
            (number,)
        )
    monkeypatch.setattr(call_runner, "start_call", mock_start_call)

    # Patch redis_ext._client with a fresh per-test MockRedis (isolated counters).
    # Also update the session interface so Flask-Session uses the same mock, not real Redis.
    from app.extensions import redis_ext
    flask_app = request.getfixturevalue("app")
    per_test_mock = MockRedis()
    monkeypatch.setattr(redis_ext, "_client", per_test_mock)
    try:
        flask_app.session_interface.client = per_test_mock
    except AttributeError:
        pass

    # _dispatch_pool removed — dispatch is now durable via call_jobs queue.


@pytest.fixture(autouse=True)
def db_conn(request):
    """Intercept all psycopg pool checkouts to run each test in a transaction that rolls back at teardown.
    Skipped automatically for tests that don't use the app fixture (pure unit tests with mocks).
    """
    if "real_db" in request.keywords:
        yield
        return
    # Only wrap in a transaction if the test actually needs the app (and thus the DB pool)
    if "app" not in request.fixturenames:
        yield
        return
    # Ensure app fixture has run so the pool is initialized
    request.getfixturevalue("app")
    from app.db import db
    import psycopg
    pool = db.get_pool()
    
    # Establish transaction connection
    with pool.connection() as conn:
        try:
            with conn.transaction():
                # Monkeypatch the pool checkout to return this connection in a mock context manager
                original_connection = pool.connection

                class MockConnectionContext:
                    def __init__(self, c):
                        self.c = c
                    def __enter__(self):
                        return self.c
                    def __exit__(self, exc_type, exc_val, exc_tb):
                        pass

                pool.connection = lambda: MockConnectionContext(conn)
                
                try:
                    yield conn
                finally:
                    pool.connection = original_connection
                
                raise psycopg.Rollback()
        except psycopg.Rollback:
            pass



@pytest.fixture
def create_tenant(app):
    """Factory fixture to register a new tenant and an admin user."""
    def _create(name: str, email: str) -> tuple[dict, dict]:
        from app.services.auth_service import register_tenant
        tenant, user = register_tenant({
            "business_name": name,
            "email": email,
            "password": "Password123!",
            "phone": "01712345678"
        })
        return tenant, user
    return _create


@pytest.fixture
def auth_client(client, app):
    """Factory fixture to log in a test client for a specific tenant/user."""
    def _auth(tenant_user_pair: tuple[dict, dict]):
        tenant, user = tenant_user_pair
        # Verify email in DB first
        from app.db import db
        db.execute("UPDATE users SET email_verified = true WHERE id = %s", (user["id"],))
        db.execute("UPDATE tenants SET status = 'active' WHERE id = %s", (tenant["id"],))
        
        # Log out any existing session first
        client.get("/auth/logout")
        
        client.post("/auth/login", data={
            "email": user["email"],
            "password": "Password123!"
        })
        return client
    return _auth


@pytest.fixture
def committed_billing_case(app):
    """Independent, committed tenant+wallet+call rows for concurrency/crash tests.

    Required behavior:
    1. Uses an independent psycopg connection (never the monkeypatched pool).
    2. Inserts tenant, wallet, rate, and call rows.
    3. Commits before yielding — every pool connection can see the data.
    4. Yields {tenant_id, call_id, initial_balance, test_run_id}.
    5. Cleans up all test-owned rows after yield, committed.
    6. No shared-database manual cleanup needed.

    Use with @pytest.mark.real_db so prevent_background_threads and db_conn
    do not interfere with concurrency or subprocess tests.
    """
    import secrets
    from decimal import Decimal
    import psycopg
    import psycopg.rows

    test_run_id = secrets.token_hex(8)
    initial_balance = Decimal("200.00")
    rate_per_min = Decimal("0.80")
    created: dict = {}

    with psycopg.connect(
        TEST_DATABASE_URL,
        autocommit=False,
        row_factory=psycopg.rows.dict_row,
    ) as setup_conn:
        with setup_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (business_name, email, status)"
                " VALUES (%s, %s, 'active') RETURNING id",
                (f"CBT_{test_run_id}", f"cbt_{test_run_id}@test.example"),
            )
            tid = cur.fetchone()["id"]
            created["tenant_id"] = tid

            cur.execute(
                "INSERT INTO wallets (tenant_id, balance) VALUES (%s, %s) RETURNING id",
                (tid, initial_balance),
            )
            created["wallet_id"] = cur.fetchone()["id"]

            cur.execute(
                "INSERT INTO customer_rates"
                " (tenant_id, rate_per_min, rate_per_call, min_charge,"
                "  billing_pulse_seconds, minimum_billable_seconds, charge_failed_calls)"
                " VALUES (%s, %s, 0.00, %s, 60, 60, false)"
                " ON CONFLICT (tenant_id) DO UPDATE SET"
                "  rate_per_min = EXCLUDED.rate_per_min,"
                "  min_charge   = EXCLUDED.min_charge",
                (tid, rate_per_min, rate_per_min),
            )

            cur.execute(
                "INSERT INTO calls"
                " (tenant_id, number, status, talk_seconds, call_type)"
                " VALUES (%s, '01700000000', 'completed', 60, 'ivr')"
                " RETURNING id",
                (tid,),
            )
            created["call_id"] = cur.fetchone()["id"]
            created["initial_balance"] = initial_balance
            created["test_run_id"] = test_run_id

        setup_conn.commit()  # visible to all pool connections

    yield created

    # Cleanup — use a transaction with deferred FK checks
    with psycopg.connect(TEST_DATABASE_URL, autocommit=False) as clean_conn:
        tid = created["tenant_id"]
        # Reset all FK and refund columns on calls before deleting wallet_transactions.
        # Must clear refund_status → 'none' together with refund_transaction_id and
        # refunded_at to satisfy CHECK constraints calls_refund_txn_required and
        # calls_refund_timestamp_required (which are NOT DEFERRABLE).
        clean_conn.execute(
            "UPDATE calls SET"
            "  wallet_transaction_id = NULL,"
            "  refund_status         = 'none',"
            "  refund_transaction_id = NULL,"
            "  refunded_at           = NULL,"
            "  refund_reason_code    = NULL,"
            "  refund_source         = NULL"
            " WHERE tenant_id = %s",
            (tid,),
        )
        clean_conn.execute("DELETE FROM wallet_transactions WHERE tenant_id = %s", (tid,))
        clean_conn.execute("DELETE FROM audit_log WHERE target_tenant_id = %s", (tid,))
        clean_conn.execute("DELETE FROM order_call_attempts WHERE tenant_id = %s", (tid,))
        clean_conn.execute("DELETE FROM calls WHERE tenant_id = %s", (tid,))
        clean_conn.execute("DELETE FROM wallets WHERE tenant_id = %s", (tid,))
        clean_conn.execute("DELETE FROM customer_rates WHERE tenant_id = %s", (tid,))
        clean_conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))
        clean_conn.commit()


@pytest.fixture
def api_client(client, app):
    """Factory fixture to inject X-API-Key credentials into the request environment."""
    def _api(tenant_user_pair: tuple[dict, dict]):
        tenant, user = tenant_user_pair
        from app.services.auth_service import generate_api_key
        # Verify email in DB first
        from app.db import db
        db.execute("UPDATE users SET email_verified = true WHERE id = %s", (user["id"],))
        db.execute("UPDATE tenants SET status = 'active' WHERE id = %s", (tenant["id"],))
        
        raw_key = generate_api_key(tenant["id"], name="Integration Test Key")
        client.environ_base["HTTP_X_API_KEY"] = raw_key
        return client
    return _api
