"""A `running` row that never produced a step is reaped after fifteen minutes.

Live, 2026-09-14 21:03 ET: a `main` channel_event run sat `running` for 104
minutes with zero `agent_run_steps` rows. The reaper only touches a live run
past its agent's wall-clock ceiling (up to two hours for main, plus grace),
so the row outlived a deploy's twenty-minute drain and the deploy restarted
nothing — after `pnpm build` had already replaced the bundle under the old
app server. A run that has recorded nothing in fifteen minutes is not doing
work; its first step lands within seconds when the runner is alive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from robothor.engine import daemon


class _Cursor:
    def __init__(self, stale_rows: list[tuple[Any, ...]]) -> None:
        self.stale_rows = stale_rows
        self.executed: list[str] = []
        self.params: list[Any] = []
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)
        self.params.append(params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        # The reaper's SELECT is the one that reads agent_runs; the workflow
        # reaper runs first and reads nothing back through fetchall.
        return self.stale_rows


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.commits = 0

    def cursor(self, **_kwargs: Any) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _run_reaper(age_seconds: float, steps: list[dict[str, Any]]) -> _Cursor:
    started = datetime.now(UTC) - timedelta(seconds=age_seconds)
    cursor = _Cursor([("11111111-1111-4111-8111-111111111111", "main", started)])
    conn = _Conn(cursor)
    with (
        patch("robothor.db.connection.get_connection", return_value=conn),
        patch.object(daemon, "_cleanup_stale_workflow_runs", return_value=0),
        patch.object(daemon, "_DAEMON_START_TS", None),
        patch("robothor.engine.tracking.list_steps", return_value=steps),
        patch.object(daemon, "stale_run_cutoff_seconds", return_value=7200),
        patch("robothor.engine.dedup.release_sync"),
    ):
        daemon._cleanup_stale_runs()
    return cursor


def _agent_run_updates(cursor: _Cursor) -> list[str]:
    return [s for s in cursor.executed if "UPDATE agent_runs" in s]


def test_a_zero_step_run_past_fifteen_minutes_is_reaped_as_no_steps() -> None:
    cursor = _run_reaper(age_seconds=20 * 60, steps=[])
    updates = _agent_run_updates(cursor)
    assert updates, "a run with no steps for 20 minutes must be reaped"
    params = cursor.params[cursor.executed.index(updates[0])]
    assert params[1] == "no_steps"
    assert "no steps recorded" in params[0]


def test_a_zero_step_run_under_fifteen_minutes_is_left_alone() -> None:
    cursor = _run_reaper(age_seconds=10 * 60, steps=[])
    assert not _agent_run_updates(cursor)


def test_a_run_with_steps_still_waits_for_its_own_ceiling() -> None:
    cursor = _run_reaper(age_seconds=20 * 60, steps=[{"step_type": "llm_call"}])
    assert not _agent_run_updates(cursor), "healthy work under its ceiling is never reaped"


def test_the_zero_step_cutoff_is_fifteen_minutes() -> None:
    assert daemon.NO_STEP_REAP_SECONDS == 15 * 60
    # The scan floor must let the reaper see such rows at all.
    assert daemon.REAP_MIN_SCAN_SECONDS <= daemon.NO_STEP_REAP_SECONDS
