"""A workflow run records what each tool step was given and what it returned.

``workflow_run_steps`` has always had ``tool_name``, ``tool_input`` and
``tool_output`` columns, but the step row was written with none of the first
two: a failed or surprising run could say *that* a step failed, never what it
was asked to do. These tests pin the three claims:

* a tool step's row carries its tool name and rendered arguments from the
  moment it is dispatched, and its output once it ends;
* the stored input and output are size-capped with a visible truncation
  marker, and an oversized payload never fails the run;
* the result object itself carries the input, so every writer of the row
  (including the fallback INSERT) can record it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import workflow as workflow_module
from robothor.engine.models import (
    RunStatus,
    WorkflowRun,
    WorkflowStepResult,
    WorkflowStepStatus,
    WorkflowStepType,
)
from robothor.engine.workflow import WorkflowEngine, parse_workflow

#: The documented cap: 64 KB of JSON per stored input or output.
STEP_IO_MAX_BYTES = 64 * 1024

# ── fake persistence ───────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, statements: list[tuple[str, tuple]], rowcount: int) -> None:
        self._statements = statements
        self.rowcount = rowcount

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None


class _FakeConn:
    def __init__(self, statements: list[tuple[str, tuple]], rowcount: int) -> None:
        self._statements = statements
        self._rowcount = rowcount

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._statements, self._rowcount)

    def commit(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextmanager
def _capture_sql(rowcount: int = 1):
    statements: list[tuple[str, tuple]] = []
    with patch(
        "robothor.db.connection.get_connection",
        lambda *a, **k: _FakeConn(statements, rowcount),
    ):
        yield statements


def _step_inserts(statements):
    return [(sql, p) for sql, p in statements if sql.startswith("INSERT INTO workflow_run_steps")]


def _step_updates(statements):
    return [(sql, p) for sql, p in statements if sql.startswith("UPDATE workflow_run_steps")]


def _column_value(sql: str, params: tuple, column: str) -> Any:
    """The parameter bound to ``column`` in an ``INSERT (cols) VALUES (%s, ...)``."""
    cols = sql.split("(", 1)[1].split(")", 1)[0]
    names = [c.strip() for c in cols.split(",")]
    assert column in names, f"{column} is not written by: {sql}"
    return params[names.index(column)]


def _set_value(sql: str, params: tuple, column: str) -> Any:
    """The parameter bound to ``column = %s`` in an ``UPDATE ... SET``."""
    set_clause = sql.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    names = [part.split("=")[0].strip() for part in set_clause.split(",")]
    assert column in names, f"{column} is not written by: {sql}"
    return params[names.index(column)]


# ── engine harness ─────────────────────────────────────────────────────


@pytest.fixture
def no_side_effects():
    with (
        patch("robothor.engine.workflow.WorkflowEngine._notify_run_failure"),
        patch("robothor.engine.dedup.try_acquire", new=AsyncMock(return_value=True)),
        patch("robothor.engine.dedup.release", new=AsyncMock()),
    ):
        yield


def _definition(tool_args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "step-io-flow",
        "steps": [
            {
                "id": "lookup",
                "type": "tool",
                "tool_name": "search_records",
                "tool_args": tool_args,
            }
        ],
    }


def _engine(engine_config, data: dict[str, Any], tool_result: Any) -> WorkflowEngine:
    runner = MagicMock()
    runner.registry.execute = AsyncMock(return_value=tool_result)
    engine = WorkflowEngine(engine_config, runner=runner)
    wf = parse_workflow(data)
    engine._workflows[wf.id] = wf
    return engine


async def _run(engine: WorkflowEngine) -> WorkflowRun:
    return await engine.execute(
        "step-io-flow",
        trigger_type="manual",
        initial_context={"who": "Alice"},
        user_id="alice",
        user_role="owner",
    )


# ── AC1: tool name, input and output are recorded ──────────────────────


class TestToolStepRecordsItsIO:
    @pytest.mark.asyncio
    async def test_step_start_row_carries_tool_name_and_rendered_input(
        self, engine_config, no_side_effects
    ) -> None:
        engine = _engine(
            engine_config,
            _definition({"query": "owner {{ who }}", "limit": 5}),
            {"results": [{"name": "Alice"}]},
        )
        with _capture_sql() as statements:
            run = await _run(engine)

        assert run.status == RunStatus.COMPLETED
        inserts = _step_inserts(statements)
        assert inserts, "no step row was written at dispatch"
        sql, params = inserts[0]
        assert _column_value(sql, params, "tool_name") == "search_records"
        stored = json.loads(_column_value(sql, params, "tool_input"))
        assert stored == {"query": "owner Alice", "limit": 5}

    @pytest.mark.asyncio
    async def test_step_end_row_carries_tool_output(self, engine_config, no_side_effects) -> None:
        engine = _engine(
            engine_config,
            _definition({"query": "x"}),
            {"results": [{"name": "Bob"}]},
        )
        with _capture_sql() as statements:
            await _run(engine)

        updates = [(sql, p) for sql, p in _step_updates(statements) if "tool_output" in sql]
        assert updates, "the step's terminal UPDATE does not write tool_output"
        sql, params = updates[0]
        assert json.loads(_set_value(sql, params, "tool_output")) == {"results": [{"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_failed_tool_step_still_records_its_input_and_error(
        self, engine_config, no_side_effects
    ) -> None:
        engine = _engine(
            engine_config,
            _definition({"query": "x"}),
            {"error": "upstream refused"},
        )
        with _capture_sql() as statements:
            run = await _run(engine)

        assert run.status == RunStatus.FAILED
        sql, params = _step_inserts(statements)[0]
        assert json.loads(_column_value(sql, params, "tool_input")) == {"query": "x"}
        final = [(s, p) for s, p in _step_updates(statements) if "tool_output" in s][0]
        assert _set_value(*final, "error_message") == "upstream refused"

    @pytest.mark.asyncio
    async def test_result_carries_tool_name_and_input(self, engine_config) -> None:
        engine = _engine(engine_config, _definition({"query": "{{ who }}"}), {"ok": True})
        wf = engine._workflows["step-io-flow"]
        run = WorkflowRun(workflow_id=wf.id, context={"who": "Alice"})
        with _capture_sql():
            result = await engine._execute_single_step(wf.steps[0], run, wf)
        assert result.tool_name == "search_records"
        assert result.tool_input == {"query": "Alice"}

    def test_fallback_insert_writes_tool_name_and_input(self, engine_config) -> None:
        """No ``running`` row to update: the INSERT must still record the input."""
        engine = WorkflowEngine(engine_config, runner=MagicMock())
        result = WorkflowStepResult(
            step_id="lookup",
            step_type=WorkflowStepType.TOOL,
            status=WorkflowStepStatus.COMPLETED,
            tool_name="search_records",
            tool_input={"query": "x"},
            tool_output={"ok": True},
        )
        with _capture_sql(rowcount=0) as statements:
            engine._persist_step(WorkflowRun(workflow_id="step-io-flow"), result)

        sql, params = _step_inserts(statements)[0]
        assert _column_value(sql, params, "tool_name") == "search_records"
        assert json.loads(_column_value(sql, params, "tool_input")) == {"query": "x"}

    @pytest.mark.asyncio
    async def test_non_tool_step_writes_no_tool_columns(
        self, engine_config, no_side_effects
    ) -> None:
        data = {"id": "step-io-flow", "steps": [{"id": "n", "type": "noop"}]}
        engine = _engine(engine_config, data, {})
        with _capture_sql() as statements:
            await _run(engine)
        sql, params = _step_inserts(statements)[0]
        assert _column_value(sql, params, "tool_name") is None
        assert _column_value(sql, params, "tool_input") is None


# ── AC2: oversized payloads are capped, the run still completes ────────


class TestStepIOIsCapped:
    def test_the_cap_is_64_kb(self) -> None:
        assert getattr(workflow_module, "STEP_IO_MAX_BYTES", None) == STEP_IO_MAX_BYTES

    @pytest.mark.asyncio
    async def test_oversized_input_is_stored_truncated_with_a_marker(
        self, engine_config, no_side_effects
    ) -> None:
        big = "x" * (STEP_IO_MAX_BYTES * 2)
        engine = _engine(engine_config, _definition({"body": big}), {"ok": True})
        with _capture_sql() as statements:
            run = await _run(engine)

        assert run.status == RunStatus.COMPLETED
        sql, params = _step_inserts(statements)[0]
        raw = _column_value(sql, params, "tool_input")
        assert len(raw.encode()) <= STEP_IO_MAX_BYTES + 1024
        stored = json.loads(raw)
        assert stored["_truncated"] is True
        assert stored["original_bytes"] > STEP_IO_MAX_BYTES
        assert stored["preview"].startswith('{"body": "xxx')

    @pytest.mark.asyncio
    async def test_oversized_output_is_stored_truncated_with_a_marker(
        self, engine_config, no_side_effects
    ) -> None:
        big = {"stdout": "y" * (STEP_IO_MAX_BYTES * 2), "exit_code": 0}
        engine = _engine(engine_config, _definition({"q": "x"}), big)
        with _capture_sql() as statements:
            run = await _run(engine)

        assert run.status == RunStatus.COMPLETED
        sql, params = [(s, p) for s, p in _step_updates(statements) if "tool_output" in s][0]
        raw = _set_value(sql, params, "tool_output")
        assert len(raw.encode()) <= STEP_IO_MAX_BYTES + 1024
        stored = json.loads(raw)
        assert stored["_truncated"] is True
        assert stored["original_bytes"] > STEP_IO_MAX_BYTES

    @pytest.mark.asyncio
    async def test_input_under_the_cap_is_stored_whole(
        self, engine_config, no_side_effects
    ) -> None:
        payload = {"body": "z" * 1000}
        engine = _engine(engine_config, _definition(payload), {"ok": True})
        with _capture_sql() as statements:
            await _run(engine)
        sql, params = _step_inserts(statements)[0]
        assert json.loads(_column_value(sql, params, "tool_input")) == payload
