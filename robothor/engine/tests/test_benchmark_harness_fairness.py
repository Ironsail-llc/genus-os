"""The benchmark harness must not fail an agent for the harness's own limits.

Three defects, all of which produce a low grade that says nothing about the
agent:

A. The sub-agent tool allow-list was hand-maintained and had drifted. Pure
   reads that agents' documented procedures depend on were stripped
   (``get_stats``, ``list_agent_reviews``, ``devops_query_metrics`` …) while
   ``create_goal`` — a write the registry force-adds after the manifest
   filter — was handed to every benchmark sub-agent.

B. The LLM judge saw ``output[:3000]``. A 5000-char answer reached the grader
   without its conclusion, and a 4-item rubric at a 0.7 threshold fails the
   whole case on one rubric item that lived in the missing tail.

C. A hardcoded 240s per-task cap against a production fleet that runs with
   ``timeout_seconds: 0``. Worse, the kill was scored: the runner absorbs the
   cancellation and returns a TIMEOUT run with empty output, so the vacuous
   ``must_not_contain`` checks passed and the case was filed as partial credit
   on a wrong answer rather than as a harness timeout.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext

CTX = ToolContext(agent_id="auto-agent", workspace="/tmp/test-workspace")


# ─── Helpers (mirrors of test_benchmark.py's, kept local for isolation) ──


def _mock_blocks():
    store: dict[str, str] = {}

    def read_block(name: str) -> dict:
        if name in store:
            return {"content": store[name], "last_written_at": "2026-04-03T00:00:00"}
        return {"error": f"Block '{name}' not found"}

    def write_block(name: str, content: str) -> dict:
        store[name] = content
        return {"success": True, "block_name": name}

    return store, read_block, write_block


def _block_patches(read_fn, write_fn):
    return (
        patch("robothor.memory.blocks.read_block", side_effect=read_fn),
        patch("robothor.memory.blocks.write_block", side_effect=write_fn),
    )


def _make_mock_run(output_text="ok", cost=0.01, steps=2, status="completed"):
    run = MagicMock()
    run.output_text = output_text
    run.total_cost_usd = cost
    run.steps = [MagicMock()] * steps
    run.status = MagicMock(value=status)
    run.id = "run-abc"
    run.error_message = None
    return run


def _agent_cfg(tools_allowed: list[str]):
    cfg = MagicMock()
    cfg.max_iterations = 10
    cfg.cost_budget_usd = 1.0
    cfg.tools_allowed = list(tools_allowed)
    cfg.tools_denied = []
    cfg.is_benchmark = False
    return cfg


@pytest.fixture(autouse=True)
def _isolate_benchmark_results_db(monkeypatch):
    """Never let these tests write to a real ``benchmark_results`` table."""

    class _FakeCursor:
        def execute(self, *a, **kw):
            return None

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeConn:
        def get_dsn_parameters(self):
            return {"dbname": "robothor_test"}

        def cursor(self, *args, **kwargs):
            # Tolerate cursor_factory=… so a stray patch can never turn into a
            # confusing TypeError in an unrelated module.
            return _FakeCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import robothor.db.connection as _conn_mod

    def _fake_get_connection(*a, **kw):
        return _FakeConn()

    # Claim the real function's module identity. ``tests/conftest_integration``
    # sweeps ``sys.modules`` for stragglers by ``__module__`` and rebinds them
    # to its proxy; without this, a module that happens to import
    # ``get_connection`` for the first time DURING one of these tests keeps the
    # fake for the rest of the session.
    _fake_get_connection.__module__ = "robothor.db.connection"
    monkeypatch.setattr(_conn_mod, "get_connection", _fake_get_connection)


async def _run_suite(suite: dict[str, Any], mock_runner, agent_cfg) -> dict[str, Any]:
    store, read_fn, write_fn = _mock_blocks()
    store[f"benchmark:{suite['agent_id']}:{suite['id']}"] = json.dumps(suite)
    p1, p2 = _block_patches(read_fn, write_fn)
    from robothor.engine.tools.handlers.benchmark import _benchmark_run

    with (
        p1,
        p2,
        patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=mock_runner),
        patch("robothor.engine.config.load_agent_config", return_value=agent_cfg),
    ):
        return await _benchmark_run(
            {"agent_id": suite["agent_id"], "suite_id": suite["id"], "tag": "t"}, CTX
        )


def _runner(execute) -> MagicMock:
    runner = MagicMock()
    runner.execute = execute
    runner.config = MagicMock()
    runner.config.manifest_dir = "/tmp"
    return runner


# ═══ A. The tool allow-list ══════════════════════════════════════════════


class TestReadOnlyToolsReachTheAgent:
    """Pure reads an agent's documented procedure needs must survive the harness."""

    @pytest.mark.parametrize(
        ("tool", "why"),
        [
            ("get_knowledge_gaps", "curiosity-engine step 1 of 7"),
            ("get_stats", "curiosity-engine step 1 of 7"),
            ("list_agent_reviews", "agent-architect must cite a review_id"),
            ("get_agent_review", "agent-architect must cite a review_id"),
            ("get_fleet_achievement_score", "agent-architect fleet triage"),
            ("experiment_status", "agent-architect dispatch check"),
            ("devops_query_metrics", "devops-analyst trend analysis"),
            ("render_devops_report", "devops-analyst report shape self-check"),
        ],
    )
    def test_pure_read_is_not_stripped(self, tool: str, why: str):
        from robothor.engine.tools.handlers.benchmark import _benchmark_tools_denied

        denied = _benchmark_tools_denied([tool, "read_file"])
        assert tool not in denied, f"benchmark harness strips read-only {tool} ({why})"

    def test_write_tool_is_still_stripped(self):
        from robothor.engine.tools.handlers.benchmark import _benchmark_tools_denied

        denied = set(_benchmark_tools_denied(["exec", "gws_gmail_send", "delete_person"]))
        assert {"exec", "gws_gmail_send", "delete_person"} <= denied

    def test_receive_agent_messages_is_not_read_only(self):
        """``messenger.receive`` is ``rpop`` — a destructive read.

        It was classified in ``READONLY_TOOLS``, which plan mode trusts and
        (since this change) the benchmark allow-list derives from. A benchmark
        sub-run of an agent would have drained that agent's real Redis inbox,
        and plan mode — whose whole promise is "look, don't touch" — would too.
        """
        from robothor.engine.tools.constants import READONLY_TOOLS
        from robothor.engine.tools.handlers.benchmark import (
            _BENCHMARK_READONLY_TOOLS,
            _benchmark_tools_denied,
        )

        assert "receive_agent_messages" not in READONLY_TOOLS
        assert "receive_agent_messages" not in _BENCHMARK_READONLY_TOOLS
        assert "receive_agent_messages" in set(
            _benchmark_tools_denied(["receive_agent_messages", "read_file"])
        )

    def test_goal_writes_denied_even_when_the_manifest_never_declared_them(self):
        """``create_goal``/``update_goal`` are force-added by the registry.

        ``ToolRegistry._get_filtered_names`` appends GOAL_TOOLS *after*
        intersecting ``tools_allowed``, so a benchmark sub-agent whose manifest
        never asked for them still gets them unless they are named explicitly
        in ``tools_denied``. Production transcripts show benchmark sub-runs of
        agent-architect calling ``update_goal`` — a durable write made by an
        agent that was supposed to be read-only.
        """
        from robothor.engine.tools.handlers.benchmark import _benchmark_tools_denied

        denied = set(_benchmark_tools_denied(["read_file", "search_memory"]))
        assert "create_goal" in denied
        assert "update_goal" in denied
        assert "get_goal" not in denied, "reading the goal has no side effect"


class TestToolClassificationParity:
    """A newly registered tool must be classified, not silently defaulted."""

    def test_every_registered_tool_is_classified(self):
        from robothor.api.mcp import get_tool_definitions
        from robothor.engine.benchmark_sandbox import (
            EXTERNAL_SIDE_EFFECT_TOOLS,
            SANDBOX_WRITE_TOOLS,
            benchmark_allowed_tools,
        )
        from robothor.engine.tools.handlers.benchmark import _BENCHMARK_EXCLUDED_TOOLS
        from robothor.engine.tools.schemas import get_engine_schemas

        registered = {d["name"] for d in get_tool_definitions()} | set(get_engine_schemas())
        classified = (
            benchmark_allowed_tools(sandbox=True)
            | SANDBOX_WRITE_TOOLS
            | EXTERNAL_SIDE_EFFECT_TOOLS
            | _BENCHMARK_EXCLUDED_TOOLS
        )
        unclassified = sorted(registered - classified)
        assert not unclassified, (
            "these tools are neither benchmark-allowed nor deliberately excluded — "
            "classify them in robothor/engine/tools/constants.py (READONLY_TOOLS) or "
            f"_BENCHMARK_EXCLUDED_TOOLS: {unclassified}"
        )

    def test_excluded_and_allowed_do_not_overlap(self):
        from robothor.engine.benchmark_sandbox import benchmark_allowed_tools
        from robothor.engine.tools.handlers.benchmark import _BENCHMARK_EXCLUDED_TOOLS

        overlap = sorted(benchmark_allowed_tools(sandbox=True) & _BENCHMARK_EXCLUDED_TOOLS)
        assert not overlap, f"tool both allowed and excluded: {overlap}"

    def test_allow_list_is_derived_not_hand_copied(self):
        """The benchmark allow-list must be a superset of the shared read-only set.

        Minus what the benchmark deliberately withholds. A hand-maintained
        second copy is what rotted the first time.
        """
        from robothor.engine.tools.constants import READONLY_TOOLS
        from robothor.engine.tools.handlers.benchmark import (
            _BENCHMARK_READONLY_TOOLS,
            _BENCHMARK_WITHHELD_READS,
        )

        missing = sorted(READONLY_TOOLS - _BENCHMARK_WITHHELD_READS - _BENCHMARK_READONLY_TOOLS)
        assert not missing, f"read-only tools missing from the benchmark allow-list: {missing}"


# ═══ B. The judge window ═════════════════════════════════════════════════


class TestJudgeSeesTheWholeAnswer:
    async def _capture_judge_prompt(self, output: str) -> str:
        from robothor.engine.tools.handlers.benchmark import _judge_output

        captured: dict[str, str] = {}

        async def _fake_acompletion(**kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = '{"scores": [1]}'
            return resp

        with patch("litellm.acompletion", side_effect=_fake_acompletion):
            await _judge_output(output, ["says something"], "test/model")
        return captured["prompt"]

    @pytest.mark.asyncio
    async def test_five_thousand_char_output_reaches_the_judge_intact(self):
        """The 8 worst-affected cases average ~5.2K chars. All of it must arrive."""
        output = "OPENING-MARKER\n" + ("filler line\n" * 400) + "\nCONCLUSION-MARKER"
        assert 3000 < len(output) < 12000
        prompt = await self._capture_judge_prompt(output)
        assert "OPENING-MARKER" in prompt
        assert "CONCLUSION-MARKER" in prompt, (
            "the judge never saw the conclusion — rubric items about the "
            "recommendation fail on a correct answer"
        )

    @pytest.mark.asyncio
    async def test_oversized_output_keeps_head_and_tail(self):
        """Past the window, keep both ends: conclusions live at the end."""
        output = "OPENING-MARKER\n" + ("x" * 200000) + "\nCONCLUSION-MARKER"
        prompt = await self._capture_judge_prompt(output)
        assert "OPENING-MARKER" in prompt
        assert "CONCLUSION-MARKER" in prompt
        assert "omitted" in prompt.lower(), "elision must be marked so the judge knows"
        assert len(prompt) < 40000, "the window must still bound judge cost"

    def test_window_is_a_named_constant(self):
        from robothor.engine.tools.handlers.benchmark import _JUDGE_OUTPUT_CHARS

        assert _JUDGE_OUTPUT_CHARS >= 12000


# ═══ C. The per-task wall-clock cap ══════════════════════════════════════


def _timeout_suite(**suite_extra: Any) -> dict[str, Any]:
    suite = {
        "id": "s-timeout",
        "agent_id": "main",
        "max_cost_usd": 1.0,
        "tasks": [
            {
                "id": "slow-task",
                "prompt": "think hard",
                "category": "correctness",
                "weight": 1.0,
                "expected": {"must_contain": ["alpha"], "must_not_contain": ["beta"]},
            }
        ],
    }
    suite.update(suite_extra)
    return suite


class TestTimeoutIsADistinctOutcome:
    @pytest.mark.asyncio
    async def test_per_task_cap_is_configurable_per_suite(self):
        async def _slow(**kwargs):
            await asyncio.sleep(3)
            return _make_mock_run(output_text="alpha")

        result = await _run_suite(
            _timeout_suite(task_timeout_seconds=0.05),
            _runner(AsyncMock(side_effect=_slow)),
            _agent_cfg(["read_file"]),
        )
        task = result["task_results"][0]
        assert task.get("timed_out") is True, (
            "a suite-level task_timeout_seconds must be honoured — the 240s "
            "hardcode has no relationship to how these agents run in production"
        )

    @pytest.mark.asyncio
    async def test_task_level_cap_overrides_the_suite(self):
        async def _slow(**kwargs):
            await asyncio.sleep(3)
            return _make_mock_run(output_text="alpha")

        suite = _timeout_suite(task_timeout_seconds=600)
        suite["tasks"][0]["timeout_seconds"] = 0.05
        result = await _run_suite(
            suite, _runner(AsyncMock(side_effect=_slow)), _agent_cfg(["read_file"])
        )
        assert result["task_results"][0].get("timed_out") is True

    @pytest.mark.asyncio
    async def test_timeout_is_not_scored_as_a_wrong_answer(self):
        """A harness kill must not become partial credit on vacuous checks.

        The runner absorbs the cancellation and returns a TIMEOUT run with an
        empty ``output_text``. Every ``must_not_contain`` pattern then passes
        against the empty string, so the case was recorded at 0.5 — a grade
        that reads as "the agent half-answered" when the agent was killed
        mid-thought.
        """
        result = await _run_suite(
            _timeout_suite(),
            _runner(AsyncMock(return_value=_make_mock_run(output_text="", status="timeout"))),
            _agent_cfg(["read_file"]),
        )
        task = result["task_results"][0]
        assert task.get("timed_out") is True
        assert task["outcome"] == "timeout"
        assert task["score"] == 0.0, (
            f"a harness timeout scored {task['score']} from vacuous checks on empty output"
        )

    @pytest.mark.asyncio
    async def test_run_record_counts_timeouts_separately(self):
        result = await _run_suite(
            _timeout_suite(),
            _runner(AsyncMock(return_value=_make_mock_run(output_text="", status="timeout"))),
            _agent_cfg(["read_file"]),
        )
        assert result["timeouts"] == 1
        assert result["passed"] == 0
        assert result["total_cases"] == 1, "a timeout stays in the denominator"

    @pytest.mark.asyncio
    async def test_scored_failure_is_not_labelled_a_timeout(self):
        result = await _run_suite(
            _timeout_suite(),
            _runner(AsyncMock(return_value=_make_mock_run(output_text="gamma"))),
            _agent_cfg(["read_file"]),
        )
        task = result["task_results"][0]
        assert not task.get("timed_out")
        assert task["outcome"] == "scored"
        assert result["timeouts"] == 0
        assert task["score"] == 0.5

    def test_default_cap_reflects_how_these_agents_actually_run(self):
        """agent-architect's production runs mean 512.8s and max 728.5s, with
        zero production timeouts — ``_defaults.yaml`` sets ``timeout_seconds: 0``.
        A 240s harness cap sits inside that distribution."""
        from robothor.engine.tools.handlers.benchmark import _DEFAULT_TASK_TIMEOUT_SECONDS

        assert _DEFAULT_TASK_TIMEOUT_SECONDS >= 750
