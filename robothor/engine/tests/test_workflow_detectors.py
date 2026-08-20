"""Tests for the workflow-health detectors (stuck runs + failure streaks).

These mirror the zombie-runner detector tests: read-only observers over
workflow_runs that fire fingerprinted, deduped alerts and never mutate
state (the daemon reaper owns the state transition).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import detectors


@pytest.fixture(autouse=True)
def _clear_dedup():
    detectors._dedup.clear()
    yield
    detectors._dedup.clear()


class TestStuckWorkflowDetector:
    @pytest.mark.asyncio
    async def test_fires_for_stuck_runs_with_fingerprint(self) -> None:
        stuck = [
            {
                "id": "wfrun-1",
                "workflow_id": "email-pipeline",
                "started_at": "2026-08-20T00:00:00",
                "age_s": 7200,
            }
        ]
        with (
            patch("robothor.engine.detectors.check_stuck_workflow_runs", return_value=stuck),
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
        ):
            assert await detectors.stuck_workflow_detector() == 1
        mock_alert.assert_awaited_once()
        level, title, body = mock_alert.await_args.args
        assert level == "warning"
        assert "email-pipeline" in body
        assert "wfrun-1" in body
        assert "workflow-stuck:wfrun-1" in detectors._dedup

    @pytest.mark.asyncio
    async def test_dedup_suppresses_repeat(self) -> None:
        stuck = [{"id": "wfrun-1", "workflow_id": "wf", "started_at": None, "age_s": 9000}]
        with (
            patch("robothor.engine.detectors.check_stuck_workflow_runs", return_value=stuck),
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
        ):
            assert await detectors.stuck_workflow_detector() == 1
            assert await detectors.stuck_workflow_detector() == 0
        assert mock_alert.await_count == 1

    @pytest.mark.asyncio
    async def test_kill_switch(self, monkeypatch) -> None:
        monkeypatch.setenv("ROBOTHOR_DETECTORS_ENABLED", "0")
        with patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert:
            assert await detectors.stuck_workflow_detector() == 0
            assert await detectors.workflow_failure_streak_detector() == 0
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_query_failure_returns_zero(self) -> None:
        with (
            patch(
                "robothor.engine.detectors.check_stuck_workflow_runs",
                side_effect=Exception("db down"),
            ),
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
        ):
            assert await detectors.stuck_workflow_detector() == 0
        mock_alert.assert_not_called()


class TestWorkflowFailureStreakDetector:
    @pytest.mark.asyncio
    async def test_fires_for_repeated_failures(self) -> None:
        streaks = [
            {
                "workflow_id": "monthly-goal-review",
                "streak": 4,
                "last_error": "Step review failed: Agent config not found: retired-agent",
            }
        ]
        with (
            patch(
                "robothor.engine.detectors.check_workflow_failure_streaks",
                return_value=streaks,
            ),
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
        ):
            assert await detectors.workflow_failure_streak_detector() == 1
        mock_alert.assert_awaited_once()
        level, title, body = mock_alert.await_args.args
        assert level == "warning"
        assert "monthly-goal-review" in title
        assert "retired-agent" in body
        assert "workflow-failing:monthly-goal-review" in detectors._dedup

    @pytest.mark.asyncio
    async def test_dedup_suppresses_repeat(self) -> None:
        streaks = [{"workflow_id": "wf-x", "streak": 3, "last_error": "boom"}]
        with (
            patch(
                "robothor.engine.detectors.check_workflow_failure_streaks",
                return_value=streaks,
            ),
            patch("robothor.engine.alerts.alert", new=AsyncMock()) as mock_alert,
        ):
            assert await detectors.workflow_failure_streak_detector() == 1
            assert await detectors.workflow_failure_streak_detector() == 0
        assert mock_alert.await_count == 1


class TestCheckQueries:
    """The check_* helpers run real SQL against workflow_runs — verify the
    query shape with a fake connection (rows are aggregated in SQL)."""

    def test_check_stuck_workflow_runs_queries_running_rows(self) -> None:
        executed: list[str] = []

        class _Cur:
            def execute(self, sql, params=None):
                executed.append(sql)

            def fetchall(self):
                return []

        class _Conn:
            def cursor(self, **kw):
                return _Cur()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("robothor.db.connection.get_connection", return_value=_Conn()):
            assert detectors.check_stuck_workflow_runs() == []
        assert executed
        sql = executed[0]
        assert "workflow_runs" in sql
        assert "'running'" in sql

    def test_check_workflow_failure_streaks_queries_terminal_rows(self) -> None:
        executed: list[str] = []

        class _Cur:
            def execute(self, sql, params=None):
                executed.append(sql)

            def fetchall(self):
                return []

        class _Conn:
            def cursor(self, **kw):
                return _Cur()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("robothor.db.connection.get_connection", return_value=_Conn()):
            assert detectors.check_workflow_failure_streaks() == []
        assert executed
        sql = executed[0]
        assert "workflow_runs" in sql
        assert "failed" in sql
