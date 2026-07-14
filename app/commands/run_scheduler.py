"""
Production scheduler service.

Run as a singleton — exactly ONE instance must be active at a time.
PostgreSQL advisory locks inside each job body prevent double execution
even if multiple instances are accidentally started.

Usage:
    FLASK_ENV=production python -m app.commands.run_scheduler

Environment:
    ENABLE_SCHEDULER=true   (required — guards against accidental web invocation)
    DATABASE_URL             (required)
    REDIS_URL                (required)
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("ccall.scheduler")


def main() -> None:
    if os.environ.get("ENABLE_SCHEDULER", "false").lower() != "true":
        print(
            "ENABLE_SCHEDULER is not 'true'. "
            "Set ENABLE_SCHEDULER=true to start the scheduler service.",
            file=sys.stderr,
        )
        sys.exit(1)

    from dotenv import load_dotenv
    load_dotenv()

    config_name = os.environ.get("FLASK_ENV", "production")
    from app import create_app
    app = create_app(config_name)

    with app.app_context():
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        from app.extensions import scheduler as scheduler_ext

        db_url = app.config["DATABASE_URL"]
        app.config["SCHEDULER_JOBSTORES"] = {
            "default": SQLAlchemyJobStore(
                url=db_url.replace("postgresql://", "postgresql+psycopg://")
            )
        }
        scheduler_ext.init_app(app)

        scheduler_ext.add_job(
            id="process_due_retries",
            func="app.scheduler:scheduled_process_retries",
            trigger="interval",
            seconds=30,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
            replace_existing=True,
        )
        scheduler_ext.add_job(
            id="retry_failed_webhooks",
            func="app.scheduler:scheduled_retry_webhooks",
            trigger="interval",
            minutes=5,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
            replace_existing=True,
        )
        scheduler_ext.add_job(
            id="cleanup_sessions",
            func="app.scheduler:scheduled_cleanup_sessions",
            trigger="interval",
            hours=1,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
            replace_existing=True,
        )
        scheduler_ext.add_job(
            id="low_balance_alerts",
            func="app.scheduler:scheduled_low_balance_alerts",
            trigger="interval",
            hours=6,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
            replace_existing=True,
        )
        scheduler_ext.add_job(
            id="expire_tts_reservations",
            func="app.scheduler:scheduled_expire_tts_reservations",
            trigger="interval",
            minutes=5,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
            replace_existing=True,
        )
        scheduler_ext.add_job(
            id="subscription_lifecycle",
            func="app.scheduler:scheduled_subscription_lifecycle",
            trigger="interval",
            hours=1,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            replace_existing=True,
        )
        scheduler_ext.add_job(
            id="wallet_reconciliation",
            func="app.services.reconciliation_service:wallet_reconcile_job",
            trigger="interval",
            hours=6,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            replace_existing=True,
        )

        scheduler_ext.start()
        logger.info("Scheduler started. Jobs registered. Running until interrupted.")

        try:
            import time
            while True:
                time.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler shutting down.")
            scheduler_ext.shutdown(wait=True)


if __name__ == "__main__":
    main()
