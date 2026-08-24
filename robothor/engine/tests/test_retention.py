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


class TestDelphiPolicies:
    """Delphi shadow-pipeline tables must have retention (they grew unbounded)."""

    def test_market_snapshots_policy(self):
        policy = RETENTION_POLICY["delphi_market_snapshots"]
        assert policy["days"] == 30
        assert policy["timestamp_col"] == "ts"

    def test_intents_policy(self):
        policy = RETENTION_POLICY["delphi_intents"]
        assert policy["days"] == 90
        assert policy["timestamp_col"] == "created_at"

    def test_estimates_policy(self):
        policy = RETENTION_POLICY["delphi_estimates"]
        assert policy["days"] == 90
        assert policy["timestamp_col"] == "ts"

    def test_delphi_policies_are_deletes(self):
        for table in ("delphi_market_snapshots", "delphi_intents", "delphi_estimates"):
            assert RETENTION_POLICY[table].get("action", "delete") == "delete"


class TestMemoryFactsEmbeddingPolicy:
    """Inactive facts older than 90 days lose their embedding (UPDATE, not DELETE)."""

    def test_policy_shape(self):
        policy = RETENTION_POLICY["memory_facts"]
        assert policy["days"] == 90
        assert policy["timestamp_col"] == "updated_at"
        assert policy["action"] == "update"
        assert policy["set_clause"] == "embedding = NULL"

    def test_policy_only_touches_inactive_rows_with_embeddings(self):
        extra = RETENTION_POLICY["memory_facts"]["extra_where"]
        assert "is_active = FALSE" in extra
        assert "embedding IS NOT NULL" in extra


class TestUpdateAction:
    @patch("robothor.db.connection.get_connection")
    def test_update_action_emits_update_sql(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 7
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = lambda s: s
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_get_conn.return_value = mock_conn

        changed = _cleanup_table(
            "memory_facts",
            days=90,
            timestamp_col="updated_at",
            action="update",
            set_clause="embedding = NULL",
            extra_where="is_active = FALSE AND embedding IS NOT NULL",
        )

        assert changed == 7
        sql = mock_cursor.execute.call_args[0][0]
        assert sql.lstrip().startswith("UPDATE memory_facts SET embedding = NULL")
        assert "DELETE" not in sql
        assert "is_active = FALSE AND embedding IS NOT NULL" in sql

    def test_update_action_requires_set_clause(self):
        with pytest.raises(ValueError, match="set_clause"):
            _cleanup_table(
                "memory_facts",
                days=90,
                timestamp_col="updated_at",
                action="update",
            )

    def test_rejects_unknown_action(self):
        with pytest.raises(ValueError, match="action"):
            _cleanup_table(
                "audit_log",
                days=90,
                timestamp_col="timestamp",
                action="truncate",
            )

    def test_delete_action_rejects_set_clause(self):
        with pytest.raises(ValueError, match="set_clause"):
            _cleanup_table(
                "audit_log",
                days=90,
                timestamp_col="timestamp",
                set_clause="embedding = NULL",
            )


class TestPolicyColumnsMatchSchema:
    """Every policy's timestamp column must exist on the live table (information_schema)."""

    def test_timestamp_columns_exist(self):
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                cur = conn.cursor()
                for table, policy in RETENTION_POLICY.items():
                    cur.execute(
                        """
                        SELECT column_name FROM information_schema.columns
                        WHERE table_name = %s
                        """,
                        (table,),
                    )
                    cols = {r[0] for r in cur.fetchall()}
                    if not cols:
                        continue  # table not present in this schema (e.g. instance-land)
                    assert policy["timestamp_col"] in cols, (
                        f"{table}: timestamp_col {policy['timestamp_col']!r} not in schema"
                    )
        except Exception as e:  # pragma: no cover - environment-dependent
            if isinstance(e, AssertionError):
                raise
            pytest.skip(f"database unavailable: {e}")


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
        with patch("robothor.engine.messaging.purge_old_messages", return_value=0):
            results = run_retention_cleanup()
        # +1: agent_messages purges via messaging.purge_old_messages, outside
        # RETENTION_POLICY (it carries two clocks the table loop can't express).
        assert len(results) == len(RETENTION_POLICY) + 1
        assert all(v == 0 for v in results.values())

    @patch("robothor.engine.retention._cleanup_table")
    def test_handles_per_table_failure(self, mock_cleanup):
        """One table failing doesn't stop cleanup of others."""

        def side_effect(table, **kwargs):
            if table == "telemetry":
                raise RuntimeError("connection lost")
            return 10

        mock_cleanup.side_effect = side_effect
        with patch("robothor.engine.messaging.purge_old_messages", return_value=10):
            results = run_retention_cleanup()

        assert results["telemetry"] == -1  # failure marker
        # All other tables should succeed
        for table, count in results.items():
            if table != "telemetry":
                assert count == 10

    @patch("robothor.engine.retention._cleanup_table")
    def test_returns_correct_counts(self, mock_cleanup):
        mock_cleanup.side_effect = lambda table, **kwargs: 500 if table == "agent_run_steps" else 0
        with patch("robothor.engine.messaging.purge_old_messages", return_value=0):
            results = run_retention_cleanup()
        assert results["agent_run_steps"] == 500
        assert results["audit_log"] == 0


class TestAgentMessagesRetention:
    """agent_messages purges via messaging.purge_old_messages, not _cleanup_table.

    The messaging module owns the two-clock policy (delivered 7d, undelivered
    30d with per-recipient logging); retention just invokes it on the daily
    sweep so durable mail cannot accumulate forever.
    """

    @patch("robothor.engine.messaging.purge_old_messages", return_value=42)
    @patch("robothor.engine.retention._cleanup_table", return_value=0)
    def test_daily_sweep_purges_agent_messages(self, mock_cleanup, mock_purge):
        results = run_retention_cleanup()
        assert results["agent_messages"] == 42
        mock_purge.assert_called_once_with()

    @patch(
        "robothor.engine.messaging.purge_old_messages",
        side_effect=RuntimeError("db down"),
    )
    @patch("robothor.engine.retention._cleanup_table", return_value=10)
    def test_agent_messages_failure_never_breaks_the_sweep(self, mock_cleanup, mock_purge):
        results = run_retention_cleanup()
        assert results["agent_messages"] == -1
        assert results["agent_run_steps"] == 10
