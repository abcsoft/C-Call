# app/__init__.py
"""Flask Application Factory — create_app() builds and wires the Flask app."""
from __future__ import annotations

import os
from typing import Any

from flask import Flask, request, jsonify, render_template, g
from flask_limiter.util import get_remote_address
from flask_babel import Babel

from app.config import config as _config_map
from app.db import db
from app.extensions import (
    limiter,
    login_manager,
    mail,
    redis_ext,
    scheduler as scheduler_ext,
    server_session,
    db_pool,
    babel,
    csrf
)



def scrub_sensitive_data(event, hint):
    import re
    def scrub_val(val):
        if isinstance(val, str):
            return re.sub(r"(?:\+?88)?01[3-9]\d{8}", "[SCRUBBED_PHONE]", val)
        elif isinstance(val, dict):
            return {k: scrub_val(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [scrub_val(item) for item in val]
        return val

    # Scrub request headers
    request_data = event.get("request", {})
    headers = request_data.get("headers", {})
    for key in list(headers.keys()):
        if key.lower() in ("authorization", "x-api-key"):
            del headers[key]

    return scrub_val(event)


def phone_masking_processor(logger, log_method, event_dict):
    import re
    def mask_val(val):
        if isinstance(val, str):
            val = re.sub(r'\b01([3-9])\d{4}(\d{4})\b', r'01\1****\2', val)
            val = re.sub(r'\+8801([3-9])\d{4}(\d{4})\b', r'+8801\1****\2', val)
            val = re.sub(r'\b8801([3-9])\d{4}(\d{4})\b', r'8801\1****\2', val)
            return val
        elif isinstance(val, dict):
            return {k: mask_val(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [mask_val(item) for item in val]
        return val

    for key, val in list(event_dict.items()):
        event_dict[key] = mask_val(val)
    return event_dict


def setup_logging(app):
    import logging
    import sys
    import structlog
    
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        phone_masking_processor,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]
    
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True
    )
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    # Clear existing handlers
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)
        
    # Handler 1: StreamHandler (INFO -> stdout)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    
    # Handler 2: FileHandler (WARNING -> file)
    log_dir = os.path.join(app.root_path, "instance")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.FileHandler(os.path.join(log_dir, "app.log"))
    file_handler.setLevel(logging.WARNING)
    
    is_prod = app.config.get("ENV") == "production" or not app.config.get("DEBUG")
    
    # Formatter using structlog ProcessorFormatter
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            phone_masking_processor,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer() if is_prod else structlog.dev.ConsoleRenderer()
        ]
    )
    stdout_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)
    
    root_logger.addHandler(stdout_handler)
    root_logger.addHandler(file_handler)


def create_app(config_name: str = "production") -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__, template_folder="templates", static_folder="../static")
    
    # Load configuration
    app.config.from_object(_config_map[config_name])

    # Sentry DSN init
    sentry_dsn = app.config.get("SENTRY_DSN")
    if sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=sentry_dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.1,
            before_send=scrub_sensitive_data
        )

    # Setup structured logging
    setup_logging(app)

    # --- Extensions Init --------------------------------------------------
    db_pool.init_app(app)
    db.init_app(app)
    redis_ext.init_app(app)
    server_session.init_app(app)
    login_manager.init_app(app)
    limiter.init_app(app)
    mail.init_app(app)
    babel.init_app(app, locale_selector=lambda: "bn")
    csrf.init_app(app)

    # APScheduler and call monitor are NOT started here.
    # The web process has ENABLE_SCHEDULER=false and ENABLE_CALL_MONITOR=false.
    # Dedicated service processes start their own role via:
    #   python -m app.commands.run_scheduler
    #   python -m app.commands.run_call_worker
    # This ensures create_app() starts zero background threads/jobs,
    # so pytest, migration scripts, and web workers are all side-effect-free.

    # --- Blueprints Registration -----------------------------------------
    from app.blueprints.admin import admin_bp
    from app.blueprints.api_public import api_bp
    from app.blueprints.auth import auth_bp
    from app.blueprints.customer import customer_bp
    from app.monitoring import metrics_bp
    from app.health import health_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(customer_bp, url_prefix="/")
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    app.register_blueprint(metrics_bp)
    app.register_blueprint(health_bp)

    csrf.exempt(api_bp)
    csrf.exempt(metrics_bp)
    csrf.exempt(health_bp)
    limiter.exempt(metrics_bp)
    limiter.exempt(health_bp)

    # Rate limits: /auth/login and /auth/register are limited per-route
    # (see app/blueprints/auth/routes.py — per IP, via the default
    # get_remote_address key_func). These two are blueprint-wide instead,
    # with custom key functions per spec.
    def _api_key_rate_key():
        return request.headers.get("X-API-Key") or get_remote_address()

    def _admin_session_rate_key():
        from flask import session
        return f"admin:{session.get('_user_id') or get_remote_address()}"

    limiter.limit("100/minute", key_func=_api_key_rate_key)(api_bp)
    limiter.limit("200/minute", key_func=_admin_session_rate_key)(admin_bp)


    # --- Middleware & Security --------------------------------------------
    from app.middleware.security import register_security_headers
    register_security_headers(app)

    # --- Request Lifecycle & Context Hooks ---------------------------------
    # Tenant resolution (X-API-Key / session) + structlog binding all live
    # in app/middleware/tenant_context.py now — single before_request hook,
    # single api_keys lookup per request (require_api_key in
    # app/blueprints/auth/decorators.py reads g.tenant_id / g.api_key_error
    # instead of re-querying).
    from app.middleware.tenant_context import register_tenant_context
    register_tenant_context(app)

    @app.after_request
    def log_api_request(response):
        if hasattr(g, "request_id"):
            response.headers["X-Request-ID"] = g.request_id
            
        # Record API request metrics
        try:
            from app.monitoring import API_REQUESTS
            endpoint = request.endpoint or request.path or "unknown"
            API_REQUESTS.labels(
                method=request.method,
                endpoint=endpoint,
                status=str(response.status_code)
            ).inc()
        except Exception:
            pass
            
        return response

    @app.teardown_request
    def clear_request_context(exception=None):
        import structlog
        structlog.contextvars.clear_contextvars()

    @app.teardown_appcontext
    def teardown_db(exception: Exception | None = None):
        """Close DB connections / release any resources on request teardown."""
        # Note: psycopg_pool manages connection check-in/check-out automatically.
        pass

    @app.template_filter("time_ago")
    def time_ago_filter(dt):
        if not dt:
            return "—"
        import datetime
        if isinstance(dt, str):
            try:
                dt = datetime.datetime.fromisoformat(dt)
            except ValueError:
                return dt
        now = datetime.datetime.now(datetime.timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        diff = now - dt
        seconds = int(diff.total_seconds())
        if seconds < 0:
            seconds = abs(seconds)
            if seconds < 60:
                return "in a few seconds"
            elif seconds < 3600:
                return f"in {seconds // 60} min"
            elif seconds < 86400:
                return f"in {seconds // 3600} hours"
            else:
                return f"in {seconds // 86400} days"
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            return f"{seconds // 60} minutes ago"
        elif seconds < 86400:
            return f"{seconds // 3600} hours ago"
        else:
            return f"{seconds // 86400} days ago"

    @app.template_filter("basename")
    def basename_filter(value):
        import os
        return os.path.basename(value) if value else ""

    # --- Error Handlers ----------------------------------------------------
    def render_error(message: str, code: int, status_str: str) -> tuple[Any, int]:
        # Return JSON if request Accept header contains application/json, else HTML template
        if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
            return jsonify({"success": False, "error": message, "code": status_str}), code
        try:
            return render_template(f"errors/{code}.html", message=message), code
        except Exception:
            return f"<h1>Error {code}</h1><p>{message}</p>", code

    @app.errorhandler(400)
    def bad_request_handler(error):
        message = getattr(error, "description", "Bad Request")
        return render_error(message, 400, "BAD_REQUEST")

    @app.errorhandler(401)
    def unauthorized_handler(error):
        message = getattr(error, "description", "Unauthorized")
        return render_error(message, 401, "UNAUTHORIZED")

    @app.errorhandler(403)
    def forbidden_handler(error):
        message = getattr(error, "description", "Forbidden")
        return render_error(message, 403, "FORBIDDEN")

    @app.errorhandler(404)
    def not_found_handler(error):
        message = getattr(error, "description", "Not Found")
        return render_error(message, 404, "NOT_FOUND")

    @app.errorhandler(429)
    def rate_limit_handler(error):
        message = getattr(error, "description", "Too Many Requests")
        return render_error(message, 429, "TOO_MANY_REQUESTS")

    @app.errorhandler(500)
    def internal_error_handler(error):
        message = getattr(error, "description", "Internal Server Error")
        return render_error(message, 500, "INTERNAL_SERVER_ERROR")


    @app.route("/health")
    def health():
        return {"success": True, "data": {"status": "ok"}}

    @app.cli.command("backfill-secrets")
    def backfill_secrets():
        """Backfill webhook secrets for existing tenants."""
        import secrets
        import json
        from app.db import db
        from app.models.tenant import invalidate_tenant_caches
        tenants = db.query("SELECT id, settings FROM tenants")
        count = 0
        for t in tenants:
            settings = t["settings"] or {}
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except Exception:
                    settings = {}
            if "webhook_secret" not in settings or not settings["webhook_secret"].startswith("whsec_"):
                settings["webhook_secret"] = f"whsec_{secrets.token_hex(32)}"
                db.execute(
                    "UPDATE tenants SET settings = %s, updated_at = now() WHERE id = %s",
                    (json.dumps(settings), t["id"])
                )
                invalidate_tenant_caches(t["id"])
                count += 1
        print(f"[+] Successfully backfilled webhook secrets for {count} tenants.")

    return app
