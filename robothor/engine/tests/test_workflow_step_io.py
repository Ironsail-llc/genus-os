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


# ── review: stored inputs are redacted like every other tool_input ─────


class TestStoredInputIsRedacted:
    """``agent_run_steps.tool_input`` is redacted before it is persisted
    (``redact_tool_arguments``); a workflow step's stored input must be too,
    while the tool itself still receives the real arguments."""

    @pytest.mark.asyncio
    async def test_credential_named_argument_is_stored_as_the_placeholder(
        self, engine_config, no_side_effects
    ) -> None:
        engine = _engine(
            engine_config,
            _definition({"api_key": "{{ key }}", "query": "x"}),
            {"ok": True},
        )
        with _capture_sql() as statements:
            await engine.execute(
                "step-io-flow",
                trigger_type="manual",
                initial_context={"key": "example-secret-value"},
                user_id="alice",
                user_role="owner",
            )

        sql, params = _step_inserts(statements)[0]
        stored = json.loads(_column_value(sql, params, "tool_input"))
        assert stored == {"api_key": "<redacted>", "query": "x"}
        called = engine.runner.registry.execute.call_args.args[1]
        assert called["api_key"] == "example-secret-value"

    @pytest.mark.asyncio
    async def test_vault_set_value_is_stored_as_the_placeholder(
        self, engine_config, no_side_effects
    ) -> None:
        data = {
            "id": "step-io-flow",
            "steps": [
                {
                    "id": "store",
                    "type": "tool",
                    "tool_name": "vault_set",
                    "tool_args": {"key": "example/service", "value": "plain-secret"},
                }
            ],
        }
        engine = _engine(engine_config, data, {"ok": True})
        with _capture_sql() as statements:
            await _run(engine)

        sql, params = _step_inserts(statements)[0]
        stored = json.loads(_column_value(sql, params, "tool_input"))
        assert stored == {"key": "example/service", "value": "<redacted>"}
        assert "plain-secret" not in json.dumps([p for _, p in statements], default=str)

    def test_fallback_insert_redacts_too(self, engine_config) -> None:
        engine = WorkflowEngine(engine_config, runner=MagicMock())
        result = WorkflowStepResult(
            step_id="store",
            step_type=WorkflowStepType.TOOL,
            status=WorkflowStepStatus.COMPLETED,
            tool_name="vault_set",
            tool_input={"key": "example/service", "value": "plain-secret"},
        )
        with _capture_sql(rowcount=0) as statements:
            engine._persist_step(WorkflowRun(workflow_id="step-io-flow"), result)

        sql, params = _step_inserts(statements)[0]
        stored = json.loads(_column_value(sql, params, "tool_input"))
        assert stored["value"] == "<redacted>"


# ── review: an empty ``tool_args:`` must not fail the whole run ────────


class TestEmptyToolArgs:
    @pytest.mark.asyncio
    async def test_yaml_tool_args_with_no_value_does_not_crash_the_run(
        self, engine_config, no_side_effects
    ) -> None:
        """``tool_args:`` with nothing under it parses to ``None``. Rendering
        now happens before the step's ``try``, so it must not raise there and
        take the run down before any step row is written."""
        import yaml

        data = yaml.safe_load(
            "id: step-io-flow\n"
            "steps:\n"
            "  - id: lookup\n"
            "    type: tool\n"
            "    tool_name: search_records\n"
            "    tool_args:\n"
        )
        engine = _engine(engine_config, data, {"ok": True})
        with _capture_sql() as statements:
            run = await _run(engine)

        assert run.status == RunStatus.COMPLETED, run.error_message
        sql, params = _step_inserts(statements)[0]
        assert json.loads(_column_value(sql, params, "tool_input")) == {}


# ── review: stored outputs are redacted like the agent path's ──────────

#: Credential-shaped, never a real key: the redactor matches on the shape.
_FAKE_TOKEN = "ghp_" + "A1b2C3d4E5" * 4


class TestStoredOutputIsRedacted:
    """The agent path runs ``redact_secrets`` on a tool result before it is
    recorded; a workflow step's stored output and output text must follow the
    same rule, since the run-detail routes serve both."""

    @pytest.mark.asyncio
    async def test_credential_in_a_tool_result_is_not_stored(
        self, engine_config, no_side_effects
    ) -> None:
        engine = _engine(
            engine_config,
            _definition({"query": "x"}),
            {"stdout": f"token={_FAKE_TOKEN}", "exit_code": 0},
        )
        with _capture_sql() as statements:
            run = await _run(engine)

        assert run.status == RunStatus.COMPLETED, run.error_message
        final = [(s, p) for s, p in _step_updates(statements) if "tool_output" in s][0]
        stored = json.loads(_set_value(*final, "tool_output"))
        assert _FAKE_TOKEN not in json.dumps(stored)
        assert "[REDACTED" in stored["stdout"]
        assert stored["exit_code"] == 0
        text = _set_value(*final, "output_text")
        assert text is not None and _FAKE_TOKEN not in text

    def test_fallback_insert_redacts_the_output_too(self, engine_config) -> None:
        engine = WorkflowEngine(engine_config, runner=MagicMock())
        result = WorkflowStepResult(
            step_id="lookup",
            step_type=WorkflowStepType.TOOL,
            status=WorkflowStepStatus.COMPLETED,
            tool_name="search_records",
            tool_input={"query": "x"},
            tool_output={"stdout": f"token={_FAKE_TOKEN}"},
            output_text=f"token={_FAKE_TOKEN}",
        )
        with _capture_sql(rowcount=0) as statements:
            engine._persist_step(WorkflowRun(workflow_id="step-io-flow"), result)

        sql, params = _step_inserts(statements)[0]
        assert _FAKE_TOKEN not in json.dumps(params, default=str)


# ── review: stored step io is always JSONB-safe ───────────────────────


def _strict_loads(text: str) -> Any:
    """``json.loads`` as Postgres JSONB reads it: no NaN or Infinity."""

    def _reject(name: str) -> Any:
        raise ValueError(f"JSONB rejects {name}")

    return json.loads(text, parse_constant=_reject)


class TestStepIOIsJsonbSafe:
    def test_a_nul_character_is_dropped(self) -> None:
        text = workflow_module._capped_json({"body": "before\x00after"})
        assert text is not None
        assert "\\u0000" not in text
        assert _strict_loads(text) == {"body": "beforeafter"}

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_number_is_stored_as_valid_json(self, value: float) -> None:
        text = workflow_module._capped_json({"score": value, "ok": True})
        assert text is not None
        _strict_loads(text)  # raises on NaN / Infinity

    @pytest.mark.asyncio
    async def test_a_nul_in_a_rendered_argument_still_writes_the_step_row(
        self, engine_config, no_side_effects
    ) -> None:
        engine = _engine(engine_config, _definition({"query": "{{ who }}"}), {"ok": True})
        with _capture_sql() as statements:
            await engine.execute(
                "step-io-flow",
                trigger_type="manual",
                initial_context={"who": "Al\x00ice"},
                user_id="alice",
                user_role="owner",
            )
        sql, params = _step_inserts(statements)[0]
        stored = _column_value(sql, params, "tool_input")
        assert "\\u0000" not in stored
        assert _strict_loads(stored) == {"query": "Alice"}


# ── review: a malformed tool_args fails the step, never escapes it ──────


class TestMalformedToolArgs:
    @pytest.mark.parametrize("bad", [["a", "b"], "a string", 7])
    def test_parse_workflow_rejects_a_non_mapping_tool_args(self, bad: Any) -> None:
        data = _definition({})
        data["steps"][0]["tool_args"] = bad
        with pytest.raises(ValueError, match="tool_args"):
            parse_workflow(data)

    @pytest.mark.asyncio
    async def test_a_tool_args_that_cannot_render_fails_the_step_with_a_row(
        self, engine_config, no_side_effects
    ) -> None:
        """A definition built past the parser can still carry a non-mapping
        ``tool_args``; the step must end FAILED with a row, as it did before
        rendering moved ahead of the step's ``try``."""
        engine = _engine(engine_config, _definition({"query": "x"}), {"ok": True})
        wf = engine._workflows["step-io-flow"]
        wf.steps[0].tool_args = ["not", "a", "mapping"]  # type: ignore[assignment]
        run = WorkflowRun(workflow_id=wf.id, context={})
        with _capture_sql() as statements:
            result = await engine._execute_single_step(wf.steps[0], run, wf)

        assert result.status == WorkflowStepStatus.FAILED
        assert _step_inserts(statements), "no step row was written"
        engine.runner.registry.execute.assert_not_called()
