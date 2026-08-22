"""A closed task must be traceable back to the run that closed it.

`agent_runs.task_id` was NULL on all 44,611 rows ever written. The runner
carried `run.task_id` in memory and used it to resolve the originating CRM
task, but never wrote it back: `create_run` inserts the row BEFORE the
auto-task exists, so the INSERT always had NULL, and nothing updated it
afterwards.

The cost was an audit hole. When measuring how many task closures were
backed by real work, the link from a closed task to the run that closed it
had to be reconstructed by string-matching resolution text — because the
foreign key that should have answered it was empty on every row.

History is unrecoverable and is deliberately not backfilled.
"""

from __future__ import annotations

import inspect

from robothor.engine import tracking


class TestUpdateRunAcceptsTaskId:
    """The write path must exist at all — it did not before."""

    def test_update_run_takes_a_task_id_parameter(self) -> None:
        params = inspect.signature(tracking.update_run).parameters
        assert "task_id" in params, (
            "update_run cannot persist task_id, so a run started from a task "
            "leaves agent_runs.task_id NULL and the closure is unauditable"
        )

    def test_task_id_is_written_when_supplied(self) -> None:
        """A supplied task_id reaches the UPDATE statement."""
        captured: dict[str, object] = {}

        class _Cur:
            rowcount = 1

            def execute(self, sql: str, values: tuple) -> None:
                captured["sql"] = sql
                captured["values"] = values

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Conn:
            def cursor(self):
                return _Cur()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        # Patch the name BOUND IN tracking, not the source module: tracking
        # does `from robothor.db.connection import get_connection` at module
        # scope, so rebinding the origin leaves this caller untouched. That
        # exact re-export trap let benchmark tests write the production
        # database for months (PR #300).
        original = tracking.get_connection
        tracking.get_connection = lambda *a, **k: _Conn()  # type: ignore[assignment]
        try:
            tracking.update_run("run-1", task_id="task-abc")
        finally:
            tracking.get_connection = original  # type: ignore[assignment]

        assert "task_id = %s" in str(captured.get("sql", "")), (
            f"task_id missing from the UPDATE: {captured.get('sql')!r}"
        )
        assert "task-abc" in captured.get("values", ()), captured.get("values")

    def test_none_task_id_is_not_written(self) -> None:
        """Passing None must not blank an existing value."""
        source = inspect.getsource(tracking.update_run)
        assert "if val is not None" in source, (
            "update_run must skip None fields, or an unrelated update would "
            "null out a task_id that was already recorded"
        )


class TestRunnerPersistsTheAutoTaskId:
    """The runner must write the id back after it creates the auto-task."""

    def test_runner_updates_the_row_after_creating_the_task(self) -> None:
        """Structural: the failure mode is a missing write, which a mocked
        end-to-end test would paper over exactly as it did before."""
        from robothor.engine import runner as runner_mod

        source = inspect.getsource(runner_mod)
        marker = "session.run.task_id = task_id if isinstance(task_id, str) else None"
        assert marker in source, "auto-task assignment moved; update this test"

        after = source.split(marker, 1)[1][:600]
        assert "update_run(" in after and "task_id=" in after, (
            "the runner sets run.task_id in memory but never persists it — "
            "agent_runs.task_id stays NULL and the closure cannot be audited"
        )
