"""The workflow deadline's identity survives every handler between the LLM call and the step.

Round 0's C1 was that `WorkflowDeadlineError` subclasses `TimeoutError`, so the
first broad `except TimeoutError` above the raise rewrote it into a fabricated
cause. That was fixed at `runner.execute`. Two things were still missing, and
both are the same lesson this repo keeps relearning — a correct function with
an unguarded caller.

**N2.** `propagates_to_caller` was tested; the `if` that calls it was not.
Reverting `runner.py`'s call site to the pre-fix
`isinstance(_cancel_exc, asyncio.CancelledError)` — reintroducing C1 verbatim —
left the entire 6,689-test engine suite green. `TestTheRunnerCancelArmReRaises`
drives the real `AgentRunner.execute` through that arm and reds under exactly
that revert.

**N1.** A workflow agent step may call `spawn_agent`, whose child
`runner.execute` runs INLINE in the parent's task and therefore inside the same
deadline scope. `spawn.py` re-raises correctly — straight into
`ToolRegistry.execute`'s broad `except TimeoutError`, which returned
`{'error': "Tool 'spawn_agent' timed out after 120s…"}`: a fabricated cause at
a duration that never elapsed, C1's exact shape one frame lower.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import workflow_budget
from robothor.engine.analytics import EXTERNAL_CANCEL_PREFIX, is_workflow_budget_cancellation
from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.workflow_budget import WorkflowDeadlineError


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "read_file"}}
        ]
        registry.get_tool_names.return_value = ["read_file"]
        registry.execute = AsyncMock(return_value={"ok": True})
        mock_reg.return_value = registry
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture
def agent_config() -> AgentConfig:
    return AgentConfig(
        id="budget-agent",
        name="Budget Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=0,
        stall_timeout_seconds=0,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
    )


def _patches():
    return (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
    )


class TestTheRunnerCancelArmReRaises:
    """N2: the guard on the `if`, not on the predicate.

    These drive the REAL cancel arm. The deadline is already spent when
    `execute` starts, so `bound_call_timeout` raises inside `_call_llm` before
    litellm is ever dialled — the production path, not a synthetic exception
    injected at the handler.
    """

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_spent_workflow_budget_escapes_execute(self, runner, agent_config):
        """Returning a run object here is C1. Only the workflow engine knows
        which step this was, so the exception has to continue."""
        p1, p2, p3 = _patches()
        with (
            p1,
            p2,
            p3,
            patch("litellm.acompletion", new=AsyncMock(side_effect=AssertionError("dialled"))),
            workflow_budget.workflow_deadline("email-pipeline", 0.0),
            workflow_budget.step_scope("classify"),
        ):
            with pytest.raises(WorkflowDeadlineError) as exc:
                await runner.execute("budget-agent", "go", agent_config=agent_config)

        assert exc.value.step_id == "classify"
        assert exc.value.workflow_id == "email-pipeline"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_the_row_is_a_cancellation_carrying_the_budget_reason(self, runner, agent_config):
        """Propagating must not cost us the row — and the row must not say
        'Circuit-breaker hard timeout', a ceiling this run never reached."""
        finished: list = []
        original = AgentRunner._finish_run

        def spy(self, run, **kwargs):
            result = original(self, run, **kwargs)
            finished.append(result)
            return result

        p1, p2, p3 = _patches()
        with (
            p1,
            p2,
            p3,
            patch("litellm.acompletion", new=AsyncMock(side_effect=AssertionError("dialled"))),
            patch.object(AgentRunner, "_finish_run", spy),
            workflow_budget.workflow_deadline("email-pipeline", 0.0),
            workflow_budget.step_scope("classify"),
        ):
            with pytest.raises(WorkflowDeadlineError):
                await runner.execute("budget-agent", "go", agent_config=agent_config)

        assert finished, "the run was abandoned without recording a terminal status"
        row = finished[0]
        assert row.status == RunStatus.CANCELLED, (
            f"a healthy agent cut short by its workflow is not a timeout it earned: {row.status}"
        )
        reason = row.error_message or ""
        assert "Circuit-breaker" not in reason, reason
        for fragment in ("email-pipeline", "classify"):
            assert fragment in reason, reason

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_the_timeout_rate_does_not_count_it(self, runner, agent_config):
        """`GENUINE_TIMEOUT_SQL` is `status='timeout' AND NOT LIKE 'Run cancelled
        externally%' AND status <> 'cancelled'`. Evaluated against the row this
        path writes, every clause must exclude it."""
        finished: list = []
        original = AgentRunner._finish_run

        def spy(self, run, **kwargs):
            result = original(self, run, **kwargs)
            finished.append(result)
            return result

        p1, p2, p3 = _patches()
        with (
            p1,
            p2,
            p3,
            patch("litellm.acompletion", new=AsyncMock(side_effect=AssertionError("dialled"))),
            patch.object(AgentRunner, "_finish_run", spy),
            workflow_budget.workflow_deadline("email-pipeline", 0.0),
            workflow_budget.step_scope("classify"),
        ):
            with pytest.raises(WorkflowDeadlineError):
                await runner.execute("budget-agent", "go", agent_config=agent_config)

        row = finished[0]
        counted_as_genuine_timeout = row.status == RunStatus.TIMEOUT and not (
            row.error_message or ""
        ).startswith(EXTERNAL_CANCEL_PREFIX)
        assert not counted_as_genuine_timeout, (
            "a workflow-budget kill is being counted in the timeout rate — the exact "
            "corruption GENUINE_TIMEOUT_SQL exists to prevent"
        )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_run_that_blew_its_own_clock_still_returns(self, runner, agent_config):
        """The discriminator has to stay narrow: propagating EVERY TimeoutError
        would turn a run's own hard cap into a caller's problem."""

        async def slow(**kwargs):
            await asyncio.sleep(30)

        p1, p2, p3 = _patches()
        with p1, p2, p3, patch("litellm.acompletion", side_effect=slow):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await runner.execute("budget-agent", "go", agent_config=agent_config)


class TestToolsDoNotFabricateATimeout:
    """N1: the sub-agent path, one frame below the workflow step."""

    @pytest.mark.asyncio
    async def test_the_registry_re_raises_instead_of_returning_a_tool_error(self):
        """`spawn_agent` runs a second `runner.execute` inline, in the parent's
        task, inside the same deadline scope. Its re-raise landed in the
        registry's broad `except TimeoutError`."""
        from robothor.engine.tools.registry import ToolRegistry

        registry = ToolRegistry()
        deadline = WorkflowDeadlineError("email-pipeline", "classify", "openrouter/x", 0.0)

        with patch(
            "robothor.engine.tools.registry._execute_tool",
            new=AsyncMock(side_effect=deadline),
        ):
            with pytest.raises(WorkflowDeadlineError) as exc:
                await registry.execute("spawn_agent", {}, agent_id="parent")

        assert exc.value.step_id == "classify"

    @pytest.mark.asyncio
    async def test_a_real_tool_timeout_is_still_a_tool_error(self):
        """Guards against the fix swallowing the case the handler is FOR."""
        from robothor.engine.tools.registry import ToolRegistry

        registry = ToolRegistry()
        with patch(
            "robothor.engine.tools.registry._execute_tool",
            new=AsyncMock(side_effect=TimeoutError("the tool really hung")),
        ):
            result = await registry.execute("read_file", {}, agent_id="parent")

        assert "error" in result
        assert "timed out" in result["error"]


class TestBudgetCancellationsAreNotResumed:
    """N3: trading `timeout` for `cancelled` put these runs into
    `RESUMABLE_STATUSES`, so `ROBOTHOR_RESUME_IN_FLIGHT` would restart an agent
    run its workflow deliberately abandoned — outside any deadline scope, while
    the workflow run itself has already ended `timeout`."""

    def test_the_reason_is_identifiable(self):
        reason = str(WorkflowDeadlineError("email-pipeline", "classify", "openrouter/x", 0.0))
        assert is_workflow_budget_cancellation(reason)
        assert not is_workflow_budget_cancellation("Run cancelled externally; last activity: x")
        assert not is_workflow_budget_cancellation(None)

    def test_resumable_drops_them_and_keeps_the_ordinary_restart_case(self):
        from robothor.engine.resume import ResumeCandidate, resumable

        budget = ResumeCandidate(
            run_id="r-budget",
            agent_id="email-classifier",
            resume_attempts=0,
            has_checkpoint=True,
            error_message=str(WorkflowDeadlineError("email-pipeline", "classify", "m", 0.0)),
        )
        restart = ResumeCandidate(
            run_id="r-restart",
            agent_id="main",
            resume_attempts=0,
            has_checkpoint=True,
            error_message=f"{EXTERNAL_CANCEL_PREFIX}; last activity: llm_inflight:x",
        )

        kept = {c.run_id for c in resumable([budget, restart])}
        assert kept == {"r-restart"}, (
            "a run its workflow abandoned was queued for resume — the workflow run "
            "has already ended, so there is nothing for the resumed agent to report to"
        )
