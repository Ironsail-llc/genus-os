"""Tests for the data retention system."""

from __future__ import annotations

from collections import OrderedDict
from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.retention import (
    _ALLOWED_TABLES,
    RETENTION_POLICY,
    _cleanup_table,
    run_retention_cleanup,
)

# ─── Policy Configuration Tests ─────────────────────────────────────


class TestRetentionPolicy:
    def test_policy_is_ordered_dict(self):
        assert isinstance(RETENTION_POLICY, OrderedDict)

    def test_all_tables_in_allowlist(self):
        for table in RETENTION_POLICY:
            assert table in _ALLOWED_TABLES

    def test_required_fields(self):
        for table, policy in RETENTION_POLICY.items():
            assert "days" in policy, f"{table} missing 'days'"
            assert "timestamp_col" in policy, f"{table} missing 'timestamp_col'"
            assert isinstance(policy["days"], int), f"{table} days must be int"
            assert policy["days"] > 0, f"{table} days must be positive"

    def test_children_before_parents(self):
        """Child tables (steps, checkpoints) must appear before parent (agent_runs)."""
        tables = list(RETENTION_POLICY.keys())
        steps_idx = tables.index("agent_run_steps")
        checkpoints_idx = tables.index("agent_run_checkpoints")
        runs_idx = tables.index("agent_runs")
        assert steps_idx < runs_idx, "agent_run_steps must come before agent_runs"
        assert checkpoints_idx < runs_idx, "agent_run_checkpoints must come before agent_runs"

    def test_parent_tables_have_status_filter(self):
        """Parent tables should only delete terminal-status runs."""
        runs_policy = RETENTION_POLICY["agent_runs"]
        assert "extra_where" in runs_policy
        assert "completed" in runs_policy["extra_where"]
        assert "running" not in runs_policy["extra_where"]
        assert "pending" not in runs_policy["extra_where"]

    def test_federation_events_synced_only(self):
        """Federation events should only delete already-synced events."""
        fed_policy = RETENTION_POLICY["federation_events"]
        assert "synced_at IS NOT NULL" in fed_policy.get("extra_where", "")


# ─── Cleanup Table Tests ────────────────────────────────────────────


class TestCleanupTable:
    def test_rejects_unknown_table(self):
        with pytest.raises(ValueError, match="not in the retention allowlist"):
            _cleanup_table("evil_table", days=30, timestamp_col="created_at")

    @patch("robothor.db.connection.get_connection")
    def test_single_batch_cleanup(self, mock_get_conn):
        """When fewer rows than batch_size, runs one batch and stops."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 42  # less than batch_size
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        deleted = _cleanup_table("audit_log", days=90, timestamp_col="timestamp", batch_size=5000)

        assert deleted == 42
        assert mock_cursor.execute.call_count == 1
        assert mock_conn.commit.call_count == 1

    @patch("robothor.db.connection.get_connection")
    def test_multi_batch_cleanup(self, mock_get_conn):
        """When rows exceed batch_size, loops until final partial batch."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        # First batch: full (5000), second batch: partial (123)
        mock_cursor.rowcount = 5000
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                mock_cursor.rowcount = 123

        mock_cursor.execute.side_effect = side_effect
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        deleted = _cleanup_table("audit_log", days=90, timestamp_col="timestamp", batch_size=5000)

        assert deleted == 5000 + 123
        assert mock_cursor.execute.call_count == 2
        assert mock_conn.commit.call_count == 2

    @patch("robothor.db.connection.get_connection")
    def test_extra_where_in_query(self, mock_get_conn):
        """extra_where clause is included in the DELETE."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        _cleanup_table(
            "agent_runs",
            days=180,
            timestamp_col="created_at",
            extra_where="status IN ('completed', 'failed')",
        )

        sql = mock_cursor.execute.call_args[0][0]
        assert "status IN ('completed', 'failed')" in sql


# ─── Orchestrator Tests ─────────────────────────────────────────────


class TestRunRetentionCleanup:
    @patch("robothor.engine.retention._cleanup_table")
    def test_processes_all_tables(self, mock_cleanup):
        mock_cleanup.return_value = 0
        results = run_retention_cleanup()
        assert len(results) == len(RETENTION_POLICY)
        assert all(v == 0 for v in results.values())

    @patch("robothor.engine.retention._cleanup_table")
    def test_handles_per_table_failure(self, mock_cleanup):
        """One table failing doesn't stop cleanup of others."""

        def side_effect(table, **kwargs):
            if table == "telemetry":
                raise RuntimeError("connection lost")
            return 10

        mock_cleanup.side_effect = side_effect
        results = run_retention_cleanup()

        assert results["telemetry"] == -1  # failure marker
        # All other tables should succeed
        for table, count in results.items():
            if table != "telemetry":
                assert count == 10

    @patch("robothor.engine.retention._cleanup_table")
    def test_returns_correct_counts(self, mock_cleanup):
        mock_cleanup.side_effect = lambda table, **kwargs: 500 if table == "agent_run_steps" else 0
        results = run_retention_cleanup()
        assert results["agent_run_steps"] == 500
        assert results["audit_log"] == 0
