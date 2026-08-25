"""The run loop bounds its own wall-clock — no dependence on anything outside it.

On 2026-08-25 a benchmark run blew through its 1200s ceiling to 3110s with
THREE enforcement layers silent at once: the outer ``asyncio.timeout``, the
stall watchdog's ``task.cancel()``, and the 80% deadline warning. The same
code, same image, same task enforced perfectly at 30s, 600s and 1200s in
instrumented probes — whatever wedged those layers was state-dependent and
did not reproduce. All three share one property: they live *beside* the loop
(an outer context manager, a sibling task, a message injection) and can all
go silent together while the loop keeps iterating.

The defense is a check that IS the loop: at the top of every iteration the
loop reads its own clock and ends its own run. It cannot be cancelled,
starved, or unhooked without also stopping the loop it is part of.

These tests kill every other layer on purpose and assert the run still ends.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.stall_watchdog import _StallWatchdog


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry"):
        mock_registry = MagicMock()
        mock_registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "exec"}},
        ]
        mock_registry.get_tool_names.return_value = ["exec"]
        r = AgentRunner(engine_config)
        r.registry = mock_registry
        yield r


def _endless_agent() -> AgentConfig:
    return AgentConfig(
        id="endless-agent",
        name="Endless Agent",
        model_primary="openrouter/test/model",
        timeout_seconds=1,
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
    )


def _make_response(content=None, tool_calls=None):
    response = MagicMock()
    response.model = "test-model"
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    response.choices = [choice]
    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    response.usage = usage
    return response


def _tool_call(i: int):
    tc = MagicMock()
    tc.id = f"call_{i}"
    tc.function.name = "exec"
    tc.function.arguments = "{}"
    return tc


class TestLoopSelfDeadline:
    @pytest.mark.asyncio
    async def test_the_loop_ends_itself_when_every_other_layer_is_dead(self, runner):
        """Watchdog inert, outer asyncio.timeout inert — the run still ends."""
        config = _endless_agent()
        calls = 0

        async def endless_llm(**kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return _make_response(tool_calls=[_tool_call(calls)])

        runner.registry.execute = AsyncMock(return_value={"content": "ok"})

        with (
            patch("asyncio.timeout", lambda *_: contextlib.nullcontext()),
            patch.object(_StallWatchdog, "start", lambda self, task: None),
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch("litellm.acompletion", side_effect=endless_llm),
        ):
            run = await asyncio.wait_for(
                runner.execute("endless-agent", "loop forever", agent_config=config),
                timeout=20,
            )

        assert run.status in (RunStatus.TIMEOUT, RunStatus.FAILED)
        assert (
            "hard timeout" in (run.error_message or "").lower()
            or "wall-clock" in (run.error_message or "").lower()
        ), f"run ended for the wrong reason: {run.error_message!r}"

    @pytest.mark.asyncio
    async def test_a_run_inside_budget_is_untouched(self, runner):
        config = _endless_agent()
        config.timeout_seconds = 60

        async def quick_llm(**kwargs):
            return _make_response(content="done immediately")

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch("litellm.acompletion", side_effect=quick_llm),
        ):
            run = await asyncio.wait_for(
                runner.execute("endless-agent", "answer", agent_config=config),
                timeout=20,
            )
        assert run.status == RunStatus.COMPLETED


class TestWatchdogTrip:
    def test_trip_flags_the_cooperative_abort(self):
        wd = _StallWatchdog(stall_timeout=0, hard_timeout=5)
        assert not wd.should_abort
        wd.trip("Circuit-breaker hard timeout (5s) — loop self-check")
        assert wd.should_abort
        assert wd.was_stall_timeout
        assert "hard timeout" in wd.abort_reason
