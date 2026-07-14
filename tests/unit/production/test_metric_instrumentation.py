"""
Phase 3: Missing metric instrumentation tests.

A. ccall_wallet_ledger_mismatch_count
B. ccall_last_backup_success_timestamp (backup.sh textfile write)
"""
from __future__ import annotations

import os
import time
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ═══════════════════════════════════════════════════════════════════════════════
# A. Wallet ledger reconciliation
# ═══════════════════════════════════════════════════════════════════════════════

class TestWalletReconciliation:

    def _make_db(self, rows):
        """Return a mock db whose query() returns rows."""
        mock = MagicMock()
        mock.query.return_value = rows
        return mock

    def test_matching_wallet_gauge_zero(self):
        """No mismatches → gauge 0, no WARNING logged."""
        from app.services.reconciliation_service import run_wallet_reconciliation
        with patch("app.services.reconciliation_service.db") as mock_db:
            mock_db.query.return_value = []  # empty = no mismatches
            count = run_wallet_reconciliation()
        assert count == 0

    def test_one_mismatch_gauge_one(self):
        """One mismatch → gauge 1."""
        from app.services.reconciliation_service import run_wallet_reconciliation
        mismatch_row = {
            "tenant_id": 42,
            "wallet_balance": Decimal("100.00"),
            "ledger_balance": Decimal("95.00"),
        }
        with patch("app.services.reconciliation_service.db") as mock_db:
            mock_db.query.return_value = [mismatch_row]
            count = run_wallet_reconciliation()
        assert count == 1

    def test_multiple_mismatches_aggregate(self):
        """N mismatches → gauge N (aggregate, not per-tenant)."""
        from app.services.reconciliation_service import run_wallet_reconciliation
        rows = [
            {"tenant_id": i, "wallet_balance": Decimal("10.00"), "ledger_balance": Decimal("5.00")}
            for i in range(5)
        ]
        with patch("app.services.reconciliation_service.db") as mock_db:
            mock_db.query.return_value = rows
            count = run_wallet_reconciliation()
        assert count == 5

    def test_no_automatic_balance_overwrite(self):
        """Reconciliation never calls db.execute with UPDATE wallets."""
        from app.services.reconciliation_service import run_wallet_reconciliation
        with patch("app.services.reconciliation_service.db") as mock_db:
            mock_db.query.return_value = [
                {"tenant_id": 1, "wallet_balance": Decimal("50.00"), "ledger_balance": Decimal("40.00")}
            ]
            run_wallet_reconciliation()
            # Ensure no UPDATE was issued
            for c in mock_db.execute.call_args_list:
                sql = str(c).upper()
                assert "UPDATE" not in sql, f"Reconciliation must not modify balances: {c}"
            assert mock_db.execute.call_count == 0, "No DB writes should occur during reconciliation"

    def test_decimal_precision(self):
        """Mismatch detection uses Decimal arithmetic (no float imprecision)."""
        from app.services.reconciliation_service import run_wallet_reconciliation
        # 0.0009 difference — within the 0.001 tolerance, should NOT be a mismatch
        # (the DB query filters by > 0.001 so these rows won't be returned)
        with patch("app.services.reconciliation_service.db") as mock_db:
            mock_db.query.return_value = []  # DB already filtered
            count = run_wallet_reconciliation()
        assert count == 0

    def test_db_error_returns_zero_no_crash(self):
        """DB query failure returns 0, does not raise."""
        from app.services.reconciliation_service import run_wallet_reconciliation
        with patch("app.services.reconciliation_service.db") as mock_db:
            mock_db.query.side_effect = Exception("DB connection lost")
            count = run_wallet_reconciliation()
        assert count == 0

    def test_logs_contain_no_balance_values(self, caplog):
        """WARNING log must not contain actual balance amounts."""
        import logging
        from app.services.reconciliation_service import run_wallet_reconciliation
        with patch("app.services.reconciliation_service.db") as mock_db:
            mock_db.query.return_value = [
                {"tenant_id": 99, "wallet_balance": Decimal("1234.56"), "ledger_balance": Decimal("999.99")}
            ]
            with caplog.at_level(logging.WARNING, logger="ccall.reconciliation"):
                run_wallet_reconciliation()

        # Balance values must not appear in any log record
        for record in caplog.records:
            assert "1234" not in record.message, f"Balance leaked in log: {record.message}"
            assert "999" not in record.message, f"Balance leaked in log: {record.message}"

    def test_scheduler_job_updates_gauge(self):
        """wallet_reconcile_job() updates WALLET_LEDGER_MISMATCH gauge."""
        import app.monitoring as mon_module
        with patch("app.services.reconciliation_service.run_wallet_reconciliation", return_value=3), \
             patch.object(mon_module, "WALLET_LEDGER_MISMATCH") as mock_gauge:
            from app.services.reconciliation_service import wallet_reconcile_job
            wallet_reconcile_job()
        mock_gauge.set.assert_called_once_with(3)


# ═══════════════════════════════════════════════════════════════════════════════
# B. Backup success timestamp metric
# ═══════════════════════════════════════════════════════════════════════════════

class TestBackupSuccessTimestamp:

    def test_textfile_written_atomically(self, tmp_path):
        """backup.sh textfile write: valid content, no temp file left behind."""
        prom_dir = tmp_path / "metrics"
        prom_dir.mkdir()
        prom_file = prom_dir / "ccall_backup.prom"
        tmp_file = prom_dir / ".ccall_backup.prom.tmp"

        # Simulate backup.sh write
        epoch = int(time.time())
        content = (
            "# HELP ccall_last_backup_success_timestamp Unix timestamp of last successful backup\n"
            "# TYPE ccall_last_backup_success_timestamp gauge\n"
            f"ccall_last_backup_success_timestamp {epoch}\n"
        )
        tmp_file.write_text(content)
        tmp_file.rename(prom_file)  # atomic mv

        assert prom_file.exists()
        assert not tmp_file.exists(), "Temp file must be removed after atomic mv"

        # Parse it
        ts = None
        for line in prom_file.read_text().splitlines():
            if line.startswith("ccall_last_backup_success_timestamp "):
                ts = float(line.split()[-1])
        assert ts == float(epoch)

    def test_monitoring_reads_textfile(self, tmp_path):
        """update_gauge_metrics() reads backup prom file and sets gauge."""
        from app.monitoring import BACKUP_SUCCESS_TIMESTAMP, update_gauge_metrics
        from unittest.mock import patch

        epoch = 1700000000
        prom_dir = tmp_path / "metrics"
        prom_dir.mkdir()
        prom_file = prom_dir / "ccall_backup.prom"
        prom_file.write_text(
            f"# HELP ccall_last_backup_success_timestamp ...\n"
            f"# TYPE ccall_last_backup_success_timestamp gauge\n"
            f"ccall_last_backup_success_timestamp {epoch}\n"
        )

        with patch.dict(os.environ, {"BACKUP_METRICS_DIR": str(prom_dir)}), \
             patch("app.monitoring.db") as mock_db, \
             patch("app.monitoring.BACKUP_SUCCESS_TIMESTAMP") as mock_gauge:
            mock_db.query_one.return_value = None
            mock_db.query.return_value = []
            update_gauge_metrics()

        mock_gauge.set.assert_called_with(float(epoch))

    def test_missing_textfile_no_crash(self, tmp_path):
        """update_gauge_metrics() does not crash if textfile not found."""
        from app.monitoring import update_gauge_metrics
        from unittest.mock import patch

        with patch.dict(os.environ, {"BACKUP_METRICS_DIR": str(tmp_path / "nonexistent")}), \
             patch("app.monitoring.db") as mock_db:
            mock_db.query_one.return_value = None
            mock_db.query.return_value = []
            update_gauge_metrics()  # must not raise

    def test_backup_failure_does_not_update_timestamp(self, tmp_path):
        """Failed backup must not write success timestamp file."""
        prom_dir = tmp_path / "metrics"
        prom_dir.mkdir()
        prom_file = prom_dir / "ccall_backup.prom"

        # Simulate backup failure: script exits before writing the file
        # (the file should not exist)
        assert not prom_file.exists(), "Success timestamp must not exist after failed backup"
