"""A run that is cancelled from outside must stay cancelled.

`execute()` catches CancelledError so the run row gets a terminal status
instead of sitting `running` forever — the 2026-08 diagnosis found 29
immortal orphans, the oldest 171 days old. That part is right. What was
missing is the other half of the contract: after recording, the cancellation
has to continue.

Swallowing it makes every enclosing deadline a suggestion. Measured on the
box 2026-08-24: `benchmark-runner` inherits the 3600s fleet wall-clock
ceiling, its watchdog cancelled the task at exactly 3600s, the benchmark case
running inside that task absorbed the CancelledError and returned — and the
sweep carried on for another three hours, losing one more innocent case per
hour to the same kill. The harness's own `asyncio.timeout(900)` per case is
defeated the same way, which the harness documents and works around by
inspecting `run.status` instead of catching TimeoutError.

The discriminator is narrow and it matters: a run killed by its OWN watchdog
(stall, early-stall, or its own hard timeout) has done its job and returns a
timed-out run, exactly as before. Only a cancellation this run did not cause
propagates.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.stall_watchdog import _StallWatchdog


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
        id="cancel-agent",
        name="Cancel Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        timeout_seconds=0,
        stall_timeout_seconds=0,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
    )


def _response(content="done", model="test-model"):
    response = MagicMock()
    response.model = model
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = None
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage
    return response


def _patches():
    return (
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
    )


class TestAnOuterDeadlineActuallyBinds:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_wait_for_around_execute_raises_timeout(self, runner, agent_config):
        """The scenario every caller assumes works, and which did not.

        `asyncio.timeout` converts its own cancellation into TimeoutError only
        if that cancellation reaches the context manager. Absorbed inside, the
        block exits normally and the deadline silently does nothing.
        """

        async def slow_completion(**kwargs):
            await asyncio.sleep(30)
            return _response()

        p1, p2, p3 = _patches()
        with p1, p2, p3, patch("litellm.acompletion", side_effect=slow_completion):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.2):
                    await runner.execute("cancel-agent", "go", agent_config=agent_config)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_cancelling_the_task_cancels_the_run(self, runner, agent_config):
        async def slow_completion(**kwargs):
            await asyncio.sleep(30)
            return _response()

        p1, p2, p3 = _patches()
        with p1, p2, p3, patch("litellm.acompletion", side_effect=slow_completion):
            task = asyncio.create_task(
                runner.execute("cancel-agent", "go", agent_config=agent_config)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestTheRunIsStillRecorded:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_terminal_status_is_written_before_the_reraise(self, runner, agent_config):
        """Propagating must not cost us the row.

        The reason this handler exists at all is the 29 immortal `running`
        rows found in August. Re-raising without finishing the run would trade
        one silent failure for another.
        """
        finished: list = []
        original = AgentRunner._finish_run

        def spy(self, run, **kwargs):
            result = original(self, run, **kwargs)
            finished.append(result)
            return result

        async def slow_completion(**kwargs):
            await asyncio.sleep(30)
            return _response()

        p1, p2, p3 = _patches()
        with (
            p1,
            p2,
            p3,
            patch("litellm.acompletion", side_effect=slow_completion),
            patch.object(AgentRunner, "_finish_run", spy),
        ):
            task = asyncio.create_task(
                runner.execute("cancel-agent", "go", agent_config=agent_config)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert finished, "the run was cancelled without recording a terminal status"
        # 2026-08-27: an EXTERNAL cancel is no longer filed as a timeout. Both
        # outcomes used to write status='timeout', which hid real timeouts from
        # the timeout rate and left `resume` (selecting status='running')
        # nothing to recover from a graceful restart.
        assert finished[0].status == RunStatus.CANCELLED
        assert "cancelled" in (finished[0].error_message or "").lower()


class TestAnOwnWatchdogKillStillReturns:
    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_stall_kill_returns_a_run_rather_than_raising(self, runner, agent_config):
        """The watchdog killing its own run is a result, not an exception.

        Every caller of a scheduled agent expects a run object back when the
        stall watchdog fires; that path is unchanged and this pins it, so a
        future widening of the re-raise cannot quietly break the fleet.
        """
        agent_config.stall_timeout_seconds = 1

        async def slow_completion(**kwargs):
            await asyncio.sleep(30)
            return _response()

        # The watchdog polls every 30s by default, which races pytest-timeout
        # and would make this test about scheduling rather than about the
        # branch. Shrinking the tick makes the kill deterministic.
        real_init = _StallWatchdog.__init__

        def fast_init(self, *args, **kwargs):
            kwargs["tick_seconds"] = 0.05
            real_init(self, *args, **kwargs)

        p1, p2, p3 = _patches()
        with (
            p1,
            p2,
            p3,
            patch("litellm.acompletion", side_effect=slow_completion),
            patch.object(_StallWatchdog, "__init__", fast_init),
            # 2026-08-27: an in-flight provider call now opens a bounded wait
            # window, and time inside it is attributed rather than counted as
            # idle -- that is the whole point of the fix. So a hang shorter
            # than the provider's own ceiling is no longer a stall. Shrinking
            # that ceiling keeps this test about the branch it names: the call
            # is bounded and ends, the window closes WITHOUT touching, idle
            # resumes from before the call, and the stall fires as it always did.
            patch("robothor.engine.llm_client.LLM_REQUEST_TIMEOUT", 0.1),
        ):
            run = await asyncio.wait_for(
                runner.execute("cancel-agent", "go", agent_config=agent_config),
                timeout=20,
            )

        assert run.status == RunStatus.TIMEOUT
        assert "progress" in (run.error_message or "").lower()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_the_runs_own_hard_cap_returns_a_run(self, runner, agent_config):
        """The other half of the discriminator.

        A run's own `timeout_seconds` surfaces as TimeoutError, not
        CancelledError. It is this run's own deadline doing its job, so it
        returns a timed-out run exactly as before — nothing propagates.
        """
        agent_config.timeout_seconds = 1

        async def slow_completion(**kwargs):
            await asyncio.sleep(30)
            return _response()

        p1, p2, p3 = _patches()
        with p1, p2, p3, patch("litellm.acompletion", side_effect=slow_completion):
            run = await asyncio.wait_for(
                runner.execute("cancel-agent", "go", agent_config=agent_config),
                timeout=20,
            )

        assert run.status == RunStatus.TIMEOUT

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_mock_run_persistence")
    async def test_a_normal_run_is_unaffected(self, runner, agent_config):
        p1, p2, p3 = _patches()
        with p1, p2, p3, patch("litellm.acompletion", side_effect=_completion_ok):
            run = await runner.execute("cancel-agent", "go", agent_config=agent_config)
        assert run.status == RunStatus.COMPLETED
        assert run.output_text == "done"


async def _completion_ok(**kwargs):
    return _response()


def _unused_json_guard() -> None:  # pragma: no cover
    assert json is not None
