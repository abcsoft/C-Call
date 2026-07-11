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


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Drop and recreate public schema, and run migrations in order."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DROP SCHEMA IF EXISTS public CASCADE;
                CREATE SCHEMA public;
                GRANT ALL ON SCHEMA public TO public;
            """)
            
        migrations_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "migrations"))
        import glob
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
    flask_app = create_app("testing")
    flask_app.config["PROPAGATE_EXCEPTIONS"] = True
    yield flask_app
    from app.extensions import db_pool
    if db_pool._pool:
        try:
            db_pool._pool.close()
        except Exception:
            pass
        db_pool._pool = None


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
        
    def mock_start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)
            
    monkeypatch.setattr(threading.Thread, "start", mock_start)


@pytest.fixture(autouse=True)
def mock_tts_and_call_runner(monkeypatch):
    """Globally mock TTS audio generation, call starting, and Redis to avoid external dependencies."""
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

    # Mock Redis client
    from app.extensions import redis_ext
    class MockRedis:
        def __init__(self):
            self.data = {}
        def incr(self, key):
            self.data[key] = self.data.get(key, 0) + 1
            return self.data[key]
        def get(self, key):
            return self.data.get(key)
        def set(self, key, value):
            self.data[key] = value
        def delete(self, key):
            if key in self.data:
                del self.data[key]
                return 1
            return 0
        def setex(self, name, time, value):
            self.data[name] = value
            return True
        def expire(self, key, time):
            return True
        def ping(self):
            return True
            
    monkeypatch.setattr(redis_ext, "_client", MockRedis())


@pytest.fixture(autouse=True)
def db_conn(request, app):
    """Intercept all psycopg pool checkouts to run each test in a transaction that rolls back at teardown."""
    if "real_db" in request.keywords:
        yield
        return
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
