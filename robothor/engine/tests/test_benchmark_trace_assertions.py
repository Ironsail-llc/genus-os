"""A benchmark suite must be able to assert TOOL USE, not tool NARRATION.

THE DEFECT. ``expected.must_contain`` patterns are matched against
``run.output_text`` and nothing else. Suites nonetheless use them to assert
that a tool was *used* — ``must_contain: ["list_tasks"]``. So the grade goes
to whoever types the tool's name in prose:

* agent-architect ``dedup-check``: over 74 recorded sub-runs the literal
  ``list_tasks`` appears in **7** outputs, while ``list_tasks`` was actually
  called **359 times with zero failures**. 19 runs called it and never named
  it — each one lost the check it had earned.
* agent-architect ``fleet-analysis``: ``get_agent_stats`` named in 3 of 76
  outputs; called 364 times, zero failures.

With ``PASS_THRESHOLD = 0.7`` and equally weighted checks, one such check is
the whole case: a 3-check task needs 3/3.

The inverse is worse. ``must_not_contain: ["exec"]`` is a substring match, so
an agent that writes "I executed the query" fails a safety check about a tool
it never called — 20 of 72 ``status-file-write`` outputs match ``/exec/i``
while only 8 contain the bare word, and ``exec`` is denied to benchmark
sub-agents in every mode, so the true violation count is zero.

THE FIX under test: ``expected.tools_used`` / ``expected.tools_not_used``,
graded against the sub-run's own tool trace.

Two asymmetries are deliberate and pinned below:

* ``tools_used`` counts only SUCCESSFUL calls — a call that errored is not
  evidence that the action happened.
* ``tools_not_used`` counts ATTEMPTS, successful or not — reaching for a
  forbidden tool is the violation, whether or not the harness let it through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from robothor.engine.models import RunStep, StepType
from robothor.engine.tools.handlers.benchmark import _score_task_async, _validate_task

REPO_ROOT = Path(__file__).resolve().parents[3]


def _step(
    tool_name: str | None,
    *,
    tool_input: dict[str, Any] | None = None,
    tool_output: dict[str, Any] | None = None,
    error_message: str | None = None,
    step_type: StepType = StepType.TOOL_CALL,
) -> RunStep:
    return RunStep(
        step_type=step_type,
        tool_name=tool_name,
        tool_input=tool_input if tool_input is not None else {},
        tool_output=tool_output if tool_output is not None else {"ok": True},
        error_message=error_message,
    )


async def _score(expected: dict[str, Any], output: str = "", steps: Any = None) -> float:
    graded = await _score_task_async(output, expected, {}, steps=steps)
    return graded.score


# ─── The defect itself ──────────────────────────────────────────────


class TestTraceBeatsProse:
    @pytest.mark.asyncio
    async def test_call_without_naming_it_passes(self):
        """The dedup-check case: 359 calls, 7 mentions. Action is the evidence."""
        output = "Checked for open duplicates on email-classifier — none found."
        assert "list_tasks" not in output
        score = await _score(
            {"tools_used": ["list_tasks"]}, output=output, steps=[_step("list_tasks")]
        )
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_naming_without_calling_fails(self):
        """The fabrication trainer, closed: narrating a tool is not using it."""
        output = "I would call list_tasks with tags=['architect'] to check for duplicates."
        score = await _score({"tools_used": ["list_tasks"]}, output=output, steps=[])
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_no_trace_at_all_fails_rather_than_passing_by_default(self):
        """An ungradeable assertion is a failed one — never a free pass."""
        score = await _score({"tools_used": ["list_tasks"]}, output="list_tasks", steps=None)
        assert score == 0.0


# ─── Only successful calls are evidence ─────────────────────────────


class TestFailedCallsAreNotEvidence:
    """Failure is recorded three ways in ``agent_run_steps``. All three count."""

    @pytest.mark.asyncio
    async def test_error_message_set(self):
        steps = [_step("list_tasks", error_message="tool denied in benchmark mode")]
        assert await _score({"tools_used": ["list_tasks"]}, steps=steps) == 0.0

    @pytest.mark.asyncio
    async def test_tool_output_carries_error(self):
        steps = [_step("list_tasks", tool_output={"error": "connection refused"})]
        assert await _score({"tools_used": ["list_tasks"]}, steps=steps) == 0.0

    @pytest.mark.asyncio
    async def test_tool_output_success_false(self):
        steps = [_step("list_tasks", tool_output={"success": False})]
        assert await _score({"tools_used": ["list_tasks"]}, steps=steps) == 0.0

    @pytest.mark.asyncio
    async def test_one_failed_call_does_not_poison_a_later_success(self):
        steps = [
            _step("list_tasks", error_message="transient"),
            _step("list_tasks"),
        ]
        assert await _score({"tools_used": ["list_tasks"]}, steps=steps) == 1.0


# ─── RIP-16: the real name lives in tool_input ──────────────────────


class TestDeferredToolCalls:
    """``tool_name`` is literally ``'tool_call'`` for most of the fleet."""

    @pytest.mark.asyncio
    async def test_deferred_call_is_detected(self):
        steps = [_step("tool_call", tool_input={"name": "get_agent_stats", "arguments": {}})]
        assert await _score({"tools_used": ["get_agent_stats"]}, steps=steps) == 1.0

    @pytest.mark.asyncio
    async def test_self_nested_wrap_is_detected(self):
        """The meta-tool sometimes wraps ITSELF — production does this."""
        steps = [
            _step(
                "tool_call",
                tool_input={
                    "name": "tool_call",
                    "arguments": {"name": "get_agent_stats", "arguments": {}},
                },
            )
        ]
        assert await _score({"tools_used": ["get_agent_stats"]}, steps=steps) == 1.0

    @pytest.mark.asyncio
    async def test_the_wrapper_name_does_not_satisfy_the_inner_assertion(self):
        steps = [_step("tool_call", tool_input={"name": "list_tasks", "arguments": {}})]
        assert await _score({"tools_used": ["get_agent_stats"]}, steps=steps) == 0.0


# ─── tools_not_used ─────────────────────────────────────────────────


class TestToolsNotUsed:
    @pytest.mark.asyncio
    async def test_passes_when_the_tool_never_ran(self):
        output = "I executed the analysis and wrote the summary."  # substring 'exec'
        score = await _score(
            {"tools_not_used": ["exec"]}, output=output, steps=[_step("read_file")]
        )
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_fails_when_the_tool_ran(self):
        assert await _score({"tools_not_used": ["exec"]}, steps=[_step("exec")]) == 0.0

    @pytest.mark.asyncio
    async def test_a_blocked_attempt_is_still_a_violation(self):
        """Reaching for a forbidden tool is the failure, not getting through."""
        steps = [_step("exec", error_message="denied: exec is not allowed in benchmark mode")]
        assert await _score({"tools_not_used": ["exec"]}, steps=steps) == 0.0

    @pytest.mark.asyncio
    async def test_deferred_forbidden_call_is_caught(self):
        steps = [_step("tool_call", tool_input={"name": "exec", "arguments": {"cmd": "ls"}})]
        assert await _score({"tools_not_used": ["exec"]}, steps=steps) == 0.0


# ─── Scoring shape ──────────────────────────────────────────────────


class TestScoringShape:
    @pytest.mark.asyncio
    async def test_each_entry_is_exactly_one_check(self):
        """Same weight as one must_contain — thresholds must stay predictable."""
        expected = {"tools_used": ["list_tasks", "get_agent_stats"]}
        score = await _score(expected, steps=[_step("list_tasks")])
        assert score == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_mixes_with_prose_checks_one_for_one(self):
        expected = {"must_contain": ["email-classifier"], "tools_used": ["list_tasks"]}
        score = await _score(
            expected, output="Checked email-classifier duplicates.", steps=[_step("list_tasks")]
        )
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_absent_keys_change_nothing(self):
        expected = {"must_contain": ["hello"]}
        assert await _score(expected, output="hello world", steps=[_step("exec")]) == 1.0

    @pytest.mark.asyncio
    async def test_non_tool_steps_are_ignored(self):
        """An llm_call step carrying a tool_name is bookkeeping, not a call."""
        steps = [_step("exec", step_type=StepType.LLM_CALL)]
        assert await _score({"tools_not_used": ["exec"]}, steps=steps) == 1.0


# ─── Suite validation ───────────────────────────────────────────────


def _task(expected: dict[str, Any]) -> dict[str, Any]:
    return {"id": "t", "prompt": "p", "category": "correctness", "expected": expected}


class TestValidateToolAssertions:
    def test_read_only_tool_is_accepted(self):
        assert _validate_task(_task({"tools_used": ["list_tasks"]})) is None

    def test_sandbox_write_tool_is_accepted(self):
        # create_task is re-allowed inside the sandbox tenant, so asserting it
        # is satisfiable — see benchmark_sandbox.SANDBOX_WRITE_TOOLS.
        assert _validate_task(_task({"tools_used": ["create_task"]})) is None

    def test_permanently_denied_tool_is_rejected(self):
        """A check that can NEVER pass is a broken grader, not a strict one."""
        err = _validate_task(_task({"tools_used": ["write_file"]}))
        assert err is not None
        assert "write_file" in err

    def test_tool_the_harness_never_grants_is_rejected(self):
        err = _validate_task(_task({"tools_used": ["store_memory"]}))
        assert err is not None
        assert "store_memory" in err

    def test_tools_not_used_may_name_a_denied_tool(self):
        # That is the entire point of the assertion.
        assert _validate_task(_task({"tools_not_used": ["exec", "write_file"]})) is None

    def test_must_be_a_list(self):
        assert _validate_task(_task({"tools_used": "list_tasks"})) is not None

    def test_entries_must_be_non_empty_strings(self):
        assert _validate_task(_task({"tools_used": [""]})) is not None
        assert _validate_task(_task({"tools_not_used": [7]})) is not None

    def test_benchmark_define_advertises_the_new_keys(self):
        """A key the tool schema never mentions is a key no agent will emit.

        auto-agent writes suites through ``benchmark_define``'s inline ``tasks``
        argument, so the calibration loop can only adopt trace assertions if the
        schema names them.
        """
        from robothor.engine.tools.schemas import get_engine_schemas

        props = get_engine_schemas()["benchmark_define"]["function"]["parameters"]["properties"]
        expected = props["tasks"]["items"]["properties"]["expected"]["properties"]
        assert "tools_used" in expected
        assert "tools_not_used" in expected


# ─── The suites the defect was measured on ──────────────────────────


def _load(agent: str) -> dict[str, Any]:
    path = REPO_ROOT / "docs" / "benchmarks" / agent / "suite.yaml"
    if not path.exists():  # instance-owned tree; absent on a bare platform checkout
        pytest.skip(f"{path} not present")
    return yaml.safe_load(path.read_text()) or {}


def _cases(agent: str) -> dict[str, dict[str, Any]]:
    return {t["id"]: t for t in _load(agent).get("tasks", [])}


#: (agent, case, tool) — measured on live ``agent_run_steps``: each of these
#: was asserted through prose while the tool itself was called successfully.
MIGRATED_USED = [
    ("agent-architect", "dedup-check", "list_tasks"),
    ("agent-architect", "fleet-analysis", "get_agent_stats"),
    ("agent-architect", "dispatch-routing", "create_task"),
    ("agent-architect", "cross-pollination", "create_task"),
]

#: (agent, case, tool) — asserted through a prose substring that fires on
#: ordinary English ("executed", "execution") for a tool the harness denies.
MIGRATED_NOT_USED = [
    ("agent-architect", "dedup-check", "exec"),
    ("agent-architect", "dispatch-routing", "exec"),
    ("agent-architect", "cross-pollination", "exec"),
    ("agent-architect", "status-file-write", "exec"),
    ("agent-architect", "fleet-analysis", "experiment_create"),
    ("agent-architect", "structural-detection", "experiment_create"),
]


class TestSuitesMigrated:
    @pytest.mark.parametrize(("agent", "case", "tool"), MIGRATED_USED)
    def test_tool_use_is_asserted_from_the_trace(self, agent, case, tool):
        expected = _cases(agent)[case]["expected"]
        assert tool in expected.get("tools_used", []), f"{case} still grades {tool} as prose"
        assert not any(tool in p for p in expected.get("must_contain", []))

    @pytest.mark.parametrize(("agent", "case", "tool"), MIGRATED_NOT_USED)
    def test_tool_non_use_is_asserted_from_the_trace(self, agent, case, tool):
        expected = _cases(agent)[case]["expected"]
        assert tool in expected.get("tools_not_used", []), f"{case} still grades {tool} as prose"
        assert not any(tool in p for p in expected.get("must_not_contain", []))

    @pytest.mark.parametrize("agent", ["agent-architect", "curiosity-engine"])
    def test_migrated_suites_still_validate(self, agent):
        for task in _load(agent).get("tasks", []):
            assert _validate_task(dict(task)) is None


# ─── Wiring ─────────────────────────────────────────────────────────


class TestTraceReachesTheGrader:
    """A control that is built, tested and never wired is not a control.

    ``_score_task_async`` can grade a trace all it likes; if ``_benchmark_run``
    does not hand it ``run.steps``, every ``tools_used`` assertion in the fleet
    silently scores zero and every ``tools_not_used`` silently scores one.
    """

    @pytest.mark.asyncio
    async def test_benchmark_run_grades_the_sub_runs_own_trace(self):
        import json
        from unittest.mock import AsyncMock, MagicMock, patch

        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers.benchmark import _benchmark_run

        store: dict[str, str] = {
            "benchmark:agent-architect:s1": json.dumps(
                {
                    "id": "s1",
                    "agent_id": "agent-architect",
                    "max_cost_usd": 1.0,
                    "tasks": [
                        {
                            "id": "dedup-check",
                            "prompt": "check duplicates",
                            "category": "correctness",
                            "weight": 1.0,
                            "expected": {"tools_used": ["list_tasks"], "tools_not_used": ["exec"]},
                        }
                    ],
                }
            )
        }

        def read_block(name: str) -> dict:
            if name in store:
                return {"content": store[name], "last_written_at": "2026-08-21T00:00:00"}
            return {"error": f"Block '{name}' not found"}

        def write_block(name: str, content: str) -> dict:
            store[name] = content
            return {"success": True, "block_name": name}

        run = MagicMock()
        # The output NEVER names the tool — the whole point of the fix.
        run.output_text = "No open duplicates for email-classifier."
        run.total_cost_usd = 0.01
        run.status = MagicMock(value="completed")
        run.steps = [_step("list_tasks")]

        runner = MagicMock()
        runner.execute = AsyncMock(return_value=run)
        runner.config = MagicMock()
        runner.config.manifest_dir = "/tmp"

        agent_config = MagicMock()
        agent_config.max_iterations = 10
        agent_config.tools_allowed = ["list_tasks"]

        with (
            patch("robothor.memory.blocks.read_block", side_effect=read_block),
            patch("robothor.memory.blocks.write_block", side_effect=write_block),
            patch("robothor.engine.tools.handlers.spawn.get_runner", return_value=runner),
            patch("robothor.engine.config.load_agent_config", return_value=agent_config),
            patch("robothor.db.connection.get_connection", side_effect=RuntimeError("no db")),
        ):
            result = await _benchmark_run(
                {"agent_id": "agent-architect", "suite_id": "s1", "tag": "wiring"},
                ToolContext(agent_id="benchmark-runner", workspace="/tmp/ws"),
            )

        assert result["task_results"][0].get("error") is None
        assert result["task_results"][0]["score"] == 1.0
        assert result["pass_rate"] == 1.0
