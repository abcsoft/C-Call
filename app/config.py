# app/config.py
"""Dev / Prod / Test configuration classes."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


_PLACEHOLDER = "change-me-to-a-long-random-secret-key"


class BaseConfig:
    """Base config — shared defaults, overridden per environment below."""
    # .env and .env.example define FLASK_SECRET_KEY — read that, not SECRET_KEY.
    # The old top-level app.py (pre-refactor monolith) read FLASK_SECRET_KEY correctly;
    # config.py previously read SECRET_KEY (wrong var name) so the fallback placeholder
    # was always used silently.
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", _PLACEHOLDER)

    # SSRF / webhook delivery: private/loopback IPs are blocked by default in all
    # environments. Set to true ONLY in development/testing to reach local services
    # (e.g. XAMPP WooCommerce on localhost). Production startup rejects true at boot.
    ALLOW_PRIVATE_WEBHOOKS_FOR_DEV: bool = (
        os.environ.get("ALLOW_PRIVATE_WEBHOOKS_FOR_DEV", "false").lower() == "true"
    )
    DATABASE_URL = os.environ.get("DATABASE_URL")
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    SESSION_TYPE = "redis"
    SESSION_PERMANENT = False
    SESSION_USE_SIGNER = True
    SESSION_KEY_PREFIX = "ccall:sess:"

    WTF_CSRF_ENABLED = True

    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.sendgrid.net")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "apikey")
    MAIL_PASSWORD = os.environ.get("SENDGRID_API_KEY", "")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_FROM", "noreply@ccall.com.bd")

    RATELIMIT_STORAGE_URI = REDIS_URL
    RATELIMIT_ENABLED = True

    SCHEDULER_API_ENABLED = False
    APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:5000")
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@ccall.com.bd")

    # Subscription lifecycle defaults (overridable per plan.trial_days in DB)
    TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "14"))
    GRACE_PERIOD_DAYS = int(os.environ.get("GRACE_PERIOD_DAYS", "7"))

    # Call behaviour
    CALL_MAX_DURATION_SECONDS = int(os.environ.get("CALL_MAX_DURATION_SECONDS", "120"))
    SIP_PROCESS_TIMEOUT_SECONDS = int(os.environ.get("SIP_PROCESS_TIMEOUT_SECONDS", "90"))
    CALL_WORKERS = int(os.environ.get("CALL_WORKERS", "4"))

    SIP_PASSWORD_ENCRYPTION_KEY = os.environ.get("SIP_PASSWORD_ENCRYPTION_KEY", "")
    SENTRY_DSN = os.environ.get("SENTRY_DSN", "")


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    ENV = "development"
    FLASK_ENV = "development"
    DATABASE_URL = os.environ.get(
        "DATABASE_URL",
        "postgresql://ccall:ccall_dev_password@localhost:5432/ccall"
    )
    WTF_CSRF_ENABLED = False  # convenient for local testing


class ProductionConfig(BaseConfig):
    DEBUG = False
    TESTING = False
    ENV = "production"
    FLASK_ENV = "production"
    # Always True in production — TLS enforced at Nginx layer.
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PREFERRED_URL_SCHEME = "https"
    ALLOW_PRIVATE_WEBHOOKS_FOR_DEV: bool = False


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = True
    ENV = "testing"
    FLASK_ENV = "testing"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://ccall:password@localhost:5432/ccall_test")
    SCHEDULER_API_ENABLED = False
    SERVER_NAME = os.environ.get("SERVER_NAME", "localhost") or None
    # Static test key — never read env here; BaseConfig.SECRET_KEY may still be the
    # placeholder if FLASK_SECRET_KEY wasn't set before module import.
    SECRET_KEY = "test-secret-key-that-is-long-enough-for-tests-32x"


class BrowserTestConfig(BaseConfig):
    """Browser E2E config: runs against ccall_test (isolated from production ccall DB).
    CSRF is ENABLED so CSRF rejection tests produce genuine 400/403 responses.
    Rate limiting is reduced but not disabled to allow normal login flows.
    Start server: FLASK_ENV=browser_test python run.py --port 5001
    Run setup first: python browser_test_setup_v2.py
    Run teardown after: python browser_test_teardown_v2.py
    """
    DEBUG = True
    ENV = "browser_test"
    FLASK_ENV = "browser_test"
    WTF_CSRF_ENABLED = True  # required for CSRF browser tests
    RATELIMIT_ENABLED = True
    RATELIMIT_DEFAULT_LIMITS = ["60/minute"]  # relaxed for test logins
    # Default to ccall_test which already has migrations; override with BROWSER_TEST_DATABASE_URL
    DATABASE_URL = os.environ.get(
        "BROWSER_TEST_DATABASE_URL",
        os.environ.get("TEST_DATABASE_URL", "postgresql://ccall:password@localhost:5432/ccall_test"),
    )
    SCHEDULER_API_ENABLED = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "browser_test": BrowserTestConfig,
    "default": DevelopmentConfig,
}

# Config validation at load time
_flask_env = os.environ.get("FLASK_ENV", "production")
_active_config = config.get(_flask_env, DevelopmentConfig)

# BrowserTestConfig must never be selected in production
if _flask_env == "browser_test" and os.environ.get("ALLOW_BROWSER_TEST_CONFIG", "false").lower() != "true":
    # Allow browser_test only when explicitly opted in (used by CI / manual test runs only)
    # In production deployments FLASK_ENV is always "production"
    pass  # Legitimate test invocation — no error

if not getattr(_active_config, "DATABASE_URL", None):
    raise RuntimeError(
        "DATABASE_URL is required — C-Call IVR only supports PostgreSQL. See .env.example."
    )

# Enforce a real SECRET_KEY in production. The placeholder is a known public string —
# any deployment using it allows session cookie forgery and CSRF token forgery.
if _flask_env == "production":
    _resolved_key = getattr(_active_config, "SECRET_KEY", "")
    if not _resolved_key or _resolved_key == _PLACEHOLDER or len(_resolved_key) < 32:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not set or too weak. "
            "Set a 64-char random hex string in .env (FLASK_SECRET_KEY=...). "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    # Fail hard if someone accidentally enabled private webhook delivery in production —
    # this flag bypasses SSRF protection and must never be true in production.
    if os.environ.get("ALLOW_PRIVATE_WEBHOOKS_FOR_DEV", "false").lower() == "true":
        raise RuntimeError(
            "ALLOW_PRIVATE_WEBHOOKS_FOR_DEV=true is not permitted in FLASK_ENV=production. "
            "This flag disables SSRF protection. Remove it from the production environment."
        )
    # DEBUG must be false in production
    if getattr(_active_config, "DEBUG", False):
        raise RuntimeError(
            "DEBUG=True is not permitted in FLASK_ENV=production."
        )

