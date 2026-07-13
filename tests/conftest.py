# tests/conftest.py
"""Pytest fixtures and configuration for tests."""
from __future__ import annotations

import os
import pytest
import psycopg
from app import create_app
from app.db import db as app_db

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://ccall:password@localhost:5432/ccall_test"
)


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
        return cur.fetchone()[0] >= 1


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Drop and recreate public schema, and run migrations in order.
    Skips rebuild if schema is already up to date (speeds up re-runs).
    Pass --force-rebuild to always rebuild.
    """
    import glob
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

    # Patch _dispatch_pool.submit to run work synchronously.
    # prevent_background_threads patches threading.Thread.start to run targets inline;
    # but ThreadPoolExecutor._worker is an infinite loop — running it inline hangs forever.
    # Replacing submit avoids starting any executor worker threads at all.
    import concurrent.futures
    from app.services import order_service

    def _sync_submit(fn, *args, **kwargs):
        f = concurrent.futures.Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as exc:
            f.set_exception(exc)
        return f

    monkeypatch.setattr(order_service._dispatch_pool, "submit", _sync_submit)


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
