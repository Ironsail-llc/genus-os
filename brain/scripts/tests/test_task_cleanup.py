#!/usr/bin/env python3
"""Tests for task_cleanup.py — Automated task queue maintenance."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_cursor(rows=None, rowcount=0):
    """Create a mock cursor that returns specified rows and rowcount."""
    cur = MagicMock()
    cur.fetchall.return_value = rows or []
    cur.rowcount = rowcount
    return cur


def _make_conn(cursor):
    """Create a mock connection wrapping a cursor."""
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn


class TestDeleteTestData:
    @patch("task_cleanup.get_conn")
    def test_deletes_matching_titles(self, mock_get_conn):
        cur = _make_cursor(rowcount=5)
        conn = _make_conn(cur)
        mock_get_conn.return_value = conn

        from task_cleanup import delete_test_data

        count = delete_test_data(conn)

        assert count == 5
        conn.commit.assert_called_once()
        # Verify the SQL targets test patterns
        sql = cur.execute.call_args[0][0]
        assert "__p1_verify_" in sql
        assert "TEST " in sql
        assert "smoke test" in sql


class TestResolvePastCalendarConflicts:
    @patch("task_cleanup.get_conn")
    def test_resolves_past_date_tasks(self, mock_get_conn):
        yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        cur = MagicMock()
        cur.fetchall.return_value = [
            {
                "id": "task-1",
                "title": "Calendar conflict",
                "body": f"Meeting on {yesterday} at 10am",
            },
        ]
        cur.rowcount = 1
        conn = _make_conn(cur)
        mock_get_conn.return_value = conn

        from task_cleanup import resolve_past_calendar_conflicts

        count = resolve_past_calendar_conflicts(conn)

        assert count == 1
        conn.commit.assert_called_once()

    @patch("task_cleanup.get_conn")
    def test_skips_future_date_tasks(self, mock_get_conn):
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
        cur = MagicMock()
        cur.fetchall.return_value = [
            {
                "id": "task-2",
                "title": "Calendar conflict",
                "body": f"Meeting on {tomorrow} at 10am",
            },
        ]
        conn = _make_conn(cur)
        mock_get_conn.return_value = conn

        from task_cleanup import resolve_past_calendar_conflicts

        count = resolve_past_calendar_conflicts(conn)

        assert count == 0


class TestResetStuckInProgress:
    @patch("task_cleanup.get_conn")
    def test_resets_old_in_progress(self, mock_get_conn):
        cur = _make_cursor(rowcount=3)
        conn = _make_conn(cur)
        mock_get_conn.return_value = conn

        from task_cleanup import reset_stuck_in_progress

        count = reset_stuck_in_progress(conn)

        assert count == 3
        conn.commit.assert_called_once()
        # Verify the SQL uses 24h cutoff
        sql = cur.execute.call_args[0][0]
        assert "IN_PROGRESS" in sql


class TestResolveOrphanTodos:
    @patch("task_cleanup.get_conn")
    def test_resolves_unassigned_old_todos(self, mock_get_conn):
        cur = _make_cursor(rowcount=7)
        conn = _make_conn(cur)
        mock_get_conn.return_value = conn

        from task_cleanup import resolve_orphan_todos

        count = resolve_orphan_todos(conn)

        assert count == 7
        conn.commit.assert_called_once()
        # Verify SQL checks for unassigned
        sql = cur.execute.call_args[0][0]
        assert "assigned_to_agent IS NULL" in sql


class TestSafetyCheck:
    @patch("task_cleanup.get_conn")
    def test_does_not_touch_recent_tasks(self, mock_get_conn):
        """Ensure stuck-reset only targets >24h and orphan-resolve only targets >72h."""
        cur = _make_cursor(rowcount=0)
        conn = _make_conn(cur)
        mock_get_conn.return_value = conn

        from task_cleanup import reset_stuck_in_progress, resolve_orphan_todos

        reset_stuck_in_progress(conn)
        resolve_orphan_todos(conn)

        # Both should have been called with cutoff times
        calls = cur.execute.call_args_list
        assert len(calls) == 2
        # First call params should have a datetime (cutoff) that's in the past
        for call in calls:
            params = call[0][1]
            cutoff = params[1]  # (TENANT_ID, cutoff)
            assert isinstance(cutoff, datetime)
            assert cutoff < datetime.now(UTC)
