"""Regression checks for production deployment manifests, firewall scripts, and call monitor lifecycle."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent


def test_compose_production_build_env_file_isolation_and_monitor():
    text = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    assert "build: ." in text
    assert "required: false" in text
    assert "ccall_production" in text
    assert "ENABLE_CALL_MONITOR: \"true\"" in text
    assert "127.0.0.1:5000:5000" in text
    assert "internal: true" in text


def test_compose_coolify_staging_database_and_port_isolation():
    text = (ROOT / "docker-compose.staging.coolify.yml").read_text(encoding="utf-8")
    assert "POSTGRES_DB: ccall_staging" in text
    assert "POSTGRES_USER: ccall_staging_app" in text
    assert "DATABASE_URL: ${STAGING_DATABASE_URL}" in text
    assert "staging_postgres_data:/var/lib/postgresql/data" in text
    assert "ports: []" in text
    assert "traefik.enable=true" in text


def test_deploy_production_supports_source_build_and_post_compose_network():
    text = (ROOT / "scripts" / "deploy_production.sh").read_text(encoding="utf-8")
    assert "BUILD_FROM_SOURCE" in text
    assert "--network ccall_internal" not in text
    assert text.index("up -d postgres redis") < text.index("run --rm migrate")
    assert "--profile backup" in text


def test_provision_firewall_prevents_public_rtp_and_protects_docker_user():
    text = (ROOT / "scripts" / "provision_production_server.sh").read_text(encoding="utf-8")
    assert "net.ipv4.ip_forward = 1" in text
    assert "net.ipv4.ip_forward = 0" not in text
    assert "\nufw allow 10000:20000/udp" not in text
    assert "SIP_CARRIER_CIDRS" in text
    assert "DOCKER-USER" in text
    assert "5432,6379,5000,9090,9093" in text


def test_call_monitor_leadership_and_graceful_shutdown(app, monkeypatch):
    import psycopg
    from app.services import order_service

    mock_conn = MagicMock()
    mock_conn.closed = False
    mock_conn.execute.return_value.fetchone.return_value = (True,)

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/ccall_test")
    order_service._monitor_active = False
    order_service._monitor_thread = None

    with (
        patch.object(psycopg, "connect", return_value=mock_conn),
        patch.object(order_service.db, "execute"),
        patch.object(order_service.db, "query", return_value=[]),
    ):
        order_service.start_call_monitor(app)
        assert order_service._monitor_active is True
        deadline = time.monotonic() + 2.0
        while not mock_conn.execute.called and time.monotonic() < deadline:
            time.sleep(0.02)
        order_service.stop_call_monitor(wait=True, timeout=3.0)

    assert order_service._monitor_active is False
    assert not order_service._monitor_thread.is_alive()
    sql_calls = [call.args[0] for call in mock_conn.execute.call_args_list if call.args]
    assert any("pg_try_advisory_lock" in s for s in sql_calls)
    assert any("pg_advisory_unlock" in s for s in sql_calls)
    mock_conn.close.assert_called_once()


def test_call_worker_stops_call_monitor_on_exit(app, monkeypatch):
    from app.commands import run_call_worker
    from app.services import order_service

    monkeypatch.setenv("ENABLE_CALL_WORKER", "true")
    monkeypatch.setenv("ENABLE_CALL_MONITOR", "true")
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.setattr("sys.argv", ["run_call_worker", "--max-jobs", "0"])

    fake_redis = SimpleNamespace(ping=lambda: True)
    with (
        patch("app.create_app", return_value=app),
        patch("app.extensions.redis_ext._client", fake_redis),
        patch.object(order_service, "start_call_monitor") as mock_start,
        patch.object(order_service, "stop_call_monitor") as mock_stop,
        patch.object(run_call_worker, "_lease_job", return_value=None),
    ):
        run_call_worker.main()

    mock_start.assert_called_once_with(app)
    mock_stop.assert_called_once_with(wait=True, timeout=5.0)


def test_compose_coolify_lowmem_staging_limits_and_isolation():
    text = (ROOT / "docker-compose.staging.lowmem.yml").read_text(encoding="utf-8")
    assert "WEB_WORKERS: \"1\"" in text
    assert "WEB_THREADS: \"2\"" in text
    assert "CALL_WORKERS: \"1\"" in text
    assert "replicas: 1" in text
    assert "shared_buffers=32MB" in text
    assert "--maxmemory 48mb" in text
    assert "127.0.0.1:${STAGING_LOOPBACK_PORT:-5050}:5000" in text
    assert "ccall_staging_internal" in text
    assert "memory: 220M" in text
    assert "memory: 160M" in text

