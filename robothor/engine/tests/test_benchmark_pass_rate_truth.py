"""``benchmark_results.pass_rate`` must mean passed / total_cases.

The defect this pins, observed on the live table 2026-08-21:

- ``pass_rate`` stored the *weighted partial-credit aggregate* while
  ``passed``/``failed`` were counted against a 0.70 threshold.  1956 of 2367
  rows disagreed with their own ``passed/total_cases``; agent-architect
  reported 0.5908 against a true 0.2857.  The Telegram ``/goals`` command
  took the numerator from one column and the percentage from the other and
  printed ``0/4 (18%)`` for crm-hygiene.
- A suite that exhausted its cost budget after task 1 appended the remaining
  tasks as ``{"skipped": True}`` and then filtered them out of the denominator,
  so dying early scored 1/1 = 100%.
- Every exception in the LLM-judge path returned 0.5, so a rate-limited judge
  was indistinguishable from a mediocre agent.
- ``sum(scores) / len(rubric)`` could exceed 1.0 when the judge returned more
  scores than the rubric had items.
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext

CTX = ToolContext(agent_id="auto-agent", workspace="/tmp/test-workspace")

_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+benchmark_results\s*\((?P<cols>[^)]*)\)",
    re.IGNORECASE,
)


# ─── Fakes ──────────────────────────────────────────────────────────


def _mock_blocks() -> tuple[dict[str, str], Any, Any]:
    """In-memory replacement for the memory-block store."""
    store: dict[str, str] = {}

    def read_block(name: str) -> dict[str, Any]:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-08-21T00:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict[str, Any]:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


@pytest.fixture
def captured_inserts(monkeypatch) -> list[tuple[str, Any]]:
    """Capture every statement ``_benchmark_run`` sends to the database.

    Patches ``robothor.db.connection.get_connection`` — the module the handler
    imports from — not the ``robothor.crm.dal`` re-export, which is how 709
    synthetic rows once reached production.
    """
    calls: list[tuple[str, Any]] = []

    class _FakeCursor:
        def execute(self, sql: str, params: Any = None) -> None:
            calls.append((sql, params))

        def fetchone(self) -> None:
            return None

        def __enter__(self) -> _FakeCursor:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    class _FakeConn:
        def get_dsn_parameters(self) -> dict[str, str]:
            return {"dbname": "robothor_test"}

        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

        def commit(self) -> None:
            return None

        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    import robothor.db.connection as _conn_mod

    monkeypatch.setattr(_conn_mod, "get_connection", lambda *a, **kw: _FakeConn())
    return calls


def _inserted_row(calls: list[tuple[str, Any]]) -> dict[str, Any]:
    """Return the benchmark_results INSERT as a column -> value mapping."""
    for sql, params in calls:
        match = _INSERT_RE.search(sql)
        if not match:
            continue
        cols = [c.strip() for c in match.group("cols").split(",") if c.strip()]
        assert params is not None, "INSERT ran with no parameters"
        assert len(cols) == len(params), f"{len(cols)} columns vs {len(params)} params"
        return dict(zip(cols, params, strict=True))
    raise AssertionError("no benchmark_results INSERT was issued")


def _mock_run(output_text: str = "ok", cost: float = 0.0) -> MagicMock:
    run = MagicMock()
    run.output_text = output_text
    run.total_cost_usd = cost
    run.steps = [MagicMock()]
    run.status = MagicMock(value="completed")
    return run


async def _run_suite(
    tasks: list[dict[str, Any]],
    *,
    outputs: list[str] | None = None,
    cost_per_task: float = 0.0,
    suite_max_cost: float = 10.0,
) -> dict[str, Any]:
    """Execute ``tasks`` through ``_benchmark_run`` with a mocked runner."""
    from robothor.engine.tools.handlers.benchmark import _benchmark_run

    store, read_fn, write_fn = _mock_blocks()
    store["benchmark:main:test-suite"] = json.dumps(
        {
            "id": "test-suite",
            "agent_id": "main",
            "max_cost_usd": suite_max_cost,
            "tasks": tasks,
        }
    )

    runs = [_mock_run(o, cost_per_task) for o in (outputs or ["ok"] * len(tasks))]
    mock_runner = MagicMock()
    mock_runner.execute = AsyncMock(side_effect=runs)
    mock_runner.config = MagicMock()
    mock_runner.config.manifest_dir = "/tmp"

    agent_config = MagicMock()
    agent_config.max_iterations = 10

    with (
        patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        patch("robothor.memory.blocks.write_block", side_effect=write_fn),
        patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=mock_runner),
        patch("robothor.engine.config.load_agent_config", return_value=agent_config),
    ):
        return await _benchmark_run(
            {"agent_id": "main", "suite_id": "test-suite", "tag": "probe"},
            CTX,
        )


def _task(task_id: str, must_contain: list[str], weight: float = 1.0) -> dict[str, Any]:
    return {
        "id": task_id,
        "prompt": "do the job",
        "category": "correctness",
        "weight": weight,
        "expected": {"must_contain": must_contain},
    }


# ─── pass_rate is a pass rate ───────────────────────────────────────


class TestPassRateColumn:
    @pytest.mark.asyncio
    async def test_pass_rate_is_passed_over_total_cases(self, captured_inserts):
        """The headline column is the count ratio, not the partial-credit mean."""
        tasks = [
            _task("t1", ["alpha"]),
            # Two patterns, one satisfied — partial credit 0.5, a failed case.
            _task("t2", ["beta", "gamma"]),
        ]
        result = await _run_suite(tasks, outputs=["alpha", "beta only"])

        row = _inserted_row(captured_inserts)
        assert row["total_cases"] == 2
        assert row["passed"] == 1
        assert row["failed"] == 1
        assert row["pass_rate"] == pytest.approx(0.5)
        assert result["pass_rate"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_aggregate_score_keeps_the_partial_credit_number(self, captured_inserts):
        """Partial credit survives, in its own column, under its own name."""
        tasks = [_task("t1", ["alpha"]), _task("t2", ["beta", "gamma"])]
        await _run_suite(tasks, outputs=["alpha", "beta only"])

        row = _inserted_row(captured_inserts)
        # (1.0 + 0.5) / 2 weighted aggregate — the historical `pass_rate` value.
        assert row["aggregate_score"] == pytest.approx(0.75)
        assert row["pass_rate"] != row["aggregate_score"]


# ─── budget-exhausted tasks stay in the denominator ─────────────────


class TestSkippedTasksCount:
    @pytest.mark.asyncio
    async def test_budget_exhausted_tasks_are_failures_not_absences(self, captured_inserts):
        """A suite that dies after task 1 must not record 1/1 = 100%."""
        tasks = [_task("t1", ["alpha"]), _task("t2", ["beta"]), _task("t3", ["gamma"])]
        result = await _run_suite(
            tasks,
            outputs=["alpha", "beta", "gamma"],
            cost_per_task=0.9,
            suite_max_cost=0.5,
        )

        row = _inserted_row(captured_inserts)
        assert row["total_cases"] == 3, "skipped tasks were dropped from the denominator"
        assert row["passed"] == 1
        assert row["failed"] == 2
        # Stored to 4dp, like every other rate on the row.
        assert row["pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
        # Skip telemetry survives the fix.
        assert result["tasks_skipped"] == 2
        assert result["tasks_run"] == 1
        skipped = [r for r in result["task_results"] if r.get("skipped")]
        assert len(skipped) == 2
        assert all(r["reason"] for r in skipped)

    @pytest.mark.asyncio
    async def test_skipped_tasks_drag_the_aggregate_down_too(self, captured_inserts):
        tasks = [_task("t1", ["alpha"]), _task("t2", ["beta"])]
        await _run_suite(tasks, outputs=["alpha", "beta"], cost_per_task=0.9, suite_max_cost=0.5)
        row = _inserted_row(captured_inserts)
        assert row["aggregate_score"] == pytest.approx(0.5)


# ─── judge failures are not a mediocre grade ────────────────────────


class TestJudgeErrors:
    @pytest.mark.asyncio
    async def test_llm_exception_is_an_error_not_half_a_point(self):
        from robothor.engine.tools.handlers.benchmark import _judge_output

        with patch(
            "litellm.acompletion", new_callable=AsyncMock, side_effect=Exception("429 rate limit")
        ):
            outcome = await _judge_output("output", ["a", "b"], "model")

        assert outcome.score is None
        assert outcome.error is not None
        assert "429" in outcome.error

    @pytest.mark.asyncio
    async def test_empty_response_is_an_error(self):
        from robothor.engine.tools.handlers.benchmark import _judge_output

        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = None

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
            outcome = await _judge_output("output", ["a", "b"], "model")

        assert outcome.score is None
        assert outcome.error is not None

    @pytest.mark.asyncio
    async def test_score_count_mismatch_never_exceeds_one(self):
        """Four scores against a two-item rubric used to grade 2.0."""
        from robothor.engine.tools.handlers.benchmark import _judge_output

        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = json.dumps({"scores": [1, 1, 1, 1]})

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
            outcome = await _judge_output("output", ["a", "b"], "model")

        assert outcome.score is None
        assert outcome.error is not None

    @pytest.mark.asyncio
    async def test_well_formed_response_still_scores(self):
        from robothor.engine.tools.handlers.benchmark import _judge_output

        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = json.dumps({"scores": [1, 0, 1]})

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=response):
            outcome = await _judge_output("output", ["a", "b", "c"], "model")

        assert outcome.error is None
        assert outcome.score == pytest.approx(2 / 3)

    @pytest.mark.asyncio
    async def test_score_task_async_propagates_the_judge_error(self):
        from robothor.engine.tools.handlers.benchmark import JudgeOutcome, _score_task_async

        expected = {
            "must_contain": ["reply"],
            "judge": {"rubric": ["Clear?"], "threshold": 0.7},
        }
        with patch(
            "robothor.engine.tools.handlers.benchmark._judge_output",
            new_callable=AsyncMock,
            return_value=JudgeOutcome(score=None, error="judge unreachable"),
        ):
            outcome = await _score_task_async("a reply", expected, {})

        assert outcome.judge_error == "judge unreachable"
        # The judge check could not be satisfied, so it does not count as met.
        assert outcome.score == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_judge_error_is_counted_failed_and_surfaced(self, captured_inserts):
        """A rate-limited judge must not look like a mediocre agent."""
        from robothor.engine.tools.handlers.benchmark import JudgeOutcome

        tasks = [
            {
                "id": "t1",
                "prompt": "write something",
                "category": "quality",
                "weight": 1.0,
                "expected": {
                    "must_contain": ["alpha"],
                    "judge": {"rubric": ["Clear?"], "threshold": 0.7},
                },
            }
        ]
        with patch(
            "robothor.engine.tools.handlers.benchmark._judge_output",
            new_callable=AsyncMock,
            return_value=JudgeOutcome(score=None, error="429 rate limit"),
        ):
            result = await _run_suite(tasks, outputs=["alpha"])

        assert result["judge_errors"] == 1
        row = _inserted_row(captured_inserts)
        assert row["judge_errors"] == 1
        assert row["passed"] == 0
        assert row["failed"] == 1
        graded = result["task_results"][0]
        assert graded["judge_error"] == "429 rate limit"
