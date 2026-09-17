"""The loop, driven to its budget, with every other layer dead.

The unit tests in `test_run_deadline.py` pin the resolver and the ladder. These
drive the real `_run_loop` to its deadline and assert the thing the recorded
failure is about: the run ENDS ITSELF, holding what it had, rather than being
destroyed by whatever is counting down outside it.

The outer `asyncio.timeout` and the stall watchdog are switched off on purpose,
the same way `test_loop_self_deadline.py` does it. If this control only works
while those two are healthy it is not a control — all three were silent at once
on 2026-08-25, and on 2026-09-17 the watchdog's ceiling was four hundred
seconds past the point at which the container was destroyed.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.models import AgentConfig, DeliveryMode, RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.stall_watchdog import _StallWatchdog


@pytest.fixture
def runner(engine_config):
    with patch("robothor.engine.runner.get_registry"):
        registry = MagicMock()
        registry.build_for_agent.return_value = [
            {"type": "function", "function": {"name": "exec"}},
            {"type": "function", "function": {"name": "write_file"}},
        ]
        registry.get_tool_names.return_value = ["exec", "write_file"]
        r = AgentRunner(engine_config)
        r.registry = registry
        yield r


@pytest.fixture(autouse=True)
def _enforcing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ROBOTHOR_STEP_EFFICIENCY_MODE", "enforce")
    monkeypatch.setenv("ROBOTHOR_RUN_WRAPUP_FRACTION", "0.5")
    monkeypatch.setenv("ROBOTHOR_RUN_BUDGET_GRACE_SECONDS", "1")


def _agent(**kw: Any) -> AgentConfig:
    return AgentConfig(
        id="budgeted-agent",
        name="Budgeted Agent",
        model_primary="openrouter/test/model",
        timeout_seconds=kw.pop("timeout_seconds", 4),
        delivery_mode=DeliveryMode.NONE,
        planning_enabled=False,
        scratchpad_enabled=False,
        # The fake tool results are not what any handler would return, so the
        # 10-error hard abort would end these runs before their budget did and
        # every assertion below would be about the wrong control.
        error_feedback=False,
        **kw,
    )


def _response(content=None, tool_calls=None):
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


def _tool_call(i: int, name: str = "exec"):
    call = MagicMock()
    call.id = f"call_{i}"
    call.function.name = name
    call.function.arguments = "{}"
    return call


@contextlib.contextmanager
def _every_other_layer_dead(llm):
    with (
        patch("asyncio.timeout", lambda *_: contextlib.nullcontext()),
        patch.object(_StallWatchdog, "start", lambda self, task: None),
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=llm),
    ):
        yield


class TestTheBudgetEndsTheRun:
    @pytest.mark.asyncio
    async def test_a_run_that_never_stops_is_ended_by_its_budget(self, runner) -> None:
        calls = 0

        async def endless(**_kw):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return _response(tool_calls=[_tool_call(calls)])

        runner.registry.execute = AsyncMock(return_value={"content": "ok"})
        with _every_other_layer_dead(endless):
            run = await asyncio.wait_for(
                runner.execute("budgeted-agent", "work forever", agent_config=_agent()),
                timeout=30,
            )
        assert run.budget_exhausted is True
        assert "budget" in (run.error_message or "").lower()
        # `budget_exhausted` is the FIELD, not a new status — the same shape
        # the cost hard-budget stop and the safety valve already use, and the
        # one `delivery` and `scheduler` already read to reframe a truncated
        # run. The loop returned rather than raised, so the run finalizes as
        # any other does: a new RunStatus would need every consumer of the old
        # ones to learn it, for a distinction these two fields already carry.
        assert run.status is RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_it_ends_with_an_honest_summary_rather_than_nothing(self, runner) -> None:
        async def endless(**_kw):
            await asyncio.sleep(0.05)
            return _response(tool_calls=[_tool_call(1)])

        runner.registry.execute = AsyncMock(return_value={"content": "ok"})
        with _every_other_layer_dead(endless):
            run = await asyncio.wait_for(
                runner.execute("budgeted-agent", "work forever", agent_config=_agent()),
                timeout=30,
            )
        assert "budget" in (run.output_text or "").lower()

    @pytest.mark.asyncio
    async def test_a_call_still_in_flight_at_the_budget_is_cancelled(self, runner) -> None:
        """The recorded shape: `wait=llm_inflight` at every tick past the budget.

        Nothing outside the engine gets to be the thing that stops this run.
        """
        cancelled = False

        async def never_returns(**_kw):
            nonlocal cancelled
            try:
                await asyncio.sleep(120)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return _response(content="too late")  # pragma: no cover

        runner.registry.execute = AsyncMock(return_value={"content": "ok"})
        with _every_other_layer_dead(never_returns):
            run = await asyncio.wait_for(
                runner.execute("budgeted-agent", "hang forever", agent_config=_agent()),
                timeout=40,
            )
        assert cancelled, "the in-flight model call outlived the budget"
        assert run.budget_exhausted is True

    @pytest.mark.asyncio
    async def test_a_run_that_finishes_early_is_untouched(self, runner) -> None:
        async def quick(**_kw):
            return _response(content="done, and here is the answer")

        with _every_other_layer_dead(quick):
            run = await asyncio.wait_for(
                runner.execute(
                    "budgeted-agent", "answer now", agent_config=_agent(timeout_seconds=600)
                ),
                timeout=30,
            )
        assert run.budget_exhausted is False
        assert "done, and here is the answer" in (run.output_text or "")

    @pytest.mark.asyncio
    async def test_wrapup_takes_the_exploring_tools_away(self, runner) -> None:
        """At the wrap-up rung the model keeps writing and loses `exec`."""
        seen: list[list[str]] = []

        async def watch_tools(**kw):
            seen.append([s["function"]["name"] for s in (kw.get("tools") or []) if "function" in s])
            await asyncio.sleep(0.05)
            return _response(tool_calls=[_tool_call(len(seen))])

        runner.registry.execute = AsyncMock(return_value={"content": "ok"})
        with _every_other_layer_dead(watch_tools):
            await asyncio.wait_for(
                runner.execute("budgeted-agent", "work forever", agent_config=_agent()),
                timeout=30,
            )
        assert seen, "no model call was ever made"
        assert "exec" in seen[0], "the first call should have the full tool set"
        assert "exec" not in seen[-1], "wrap-up never narrowed the tools"
        assert "write_file" in seen[-1], "wrap-up took away the ability to write"

    @pytest.mark.asyncio
    async def test_wrapup_refuses_the_exec_a_model_asks_for_anyway(self, runner) -> None:
        """Narrowing the schema is a request; admission is the rule.

        Hostile review 2026-09-17, finding 2: a model that kept asking for
        `exec` after the narrowing had it executed eight more times, so
        "nothing that starts new work survives" was false as written.
        """
        offered: list[bool] = []  # was `exec` in the schema this turn
        executed: list[int] = []  # turns on which the registry ran it

        async def _execute(name, *_a, **_kw):
            if name == "exec":
                executed.append(len(offered) - 1)
            return {"content": "ok"}

        async def always_asks_for_exec(**kw):
            names = {s["function"]["name"] for s in (kw.get("tools") or []) if "function" in s}
            offered.append("exec" in names)
            await asyncio.sleep(0.05)
            return _response(tool_calls=[_tool_call(len(offered))])

        runner.registry.execute = AsyncMock(side_effect=_execute)
        with _every_other_layer_dead(always_asks_for_exec):
            await asyncio.wait_for(
                runner.execute("budgeted-agent", "run things", agent_config=_agent()),
                timeout=30,
            )
        narrowed = [i for i, had in enumerate(offered) if not had]
        assert narrowed, "wrap-up never narrowed the schema — the test proves nothing"
        assert executed, "no `exec` ran at all before wrap-up — the test proves nothing"
        assert not set(executed) & set(narrowed), (
            f"`exec` ran on turns where it was withdrawn: {sorted(set(executed) & set(narrowed))}"
        )

    @pytest.mark.asyncio
    async def test_off_with_an_imposed_budget_is_still_the_previous_engine(
        self, runner, monkeypatch
    ) -> None:
        """Hostile review 2026-09-17, finding 1 — through the real loop.

        The bench harness exports `ROBOTHOR_RUN_BUDGET_SECONDS` on every run
        whatever the rung says, so the `off` arm of the differential was
        receiving the biggest part of this change: the run ended at the
        imposed 2s instead of at the agent's own 60s ceiling. At `off` the
        imposed number must not be the one any clock uses.
        """
        monkeypatch.setenv("ROBOTHOR_STEP_EFFICIENCY_MODE", "off")
        monkeypatch.setenv("ROBOTHOR_RUN_BUDGET_SECONDS", "2")

        async def quick(**_kw):
            await asyncio.sleep(0.05)
            return _response(content="finished in well under two seconds")

        with _every_other_layer_dead(quick):
            run = await asyncio.wait_for(
                runner.execute(
                    "budgeted-agent", "answer now", agent_config=_agent(timeout_seconds=60)
                ),
                timeout=30,
            )
        assert run.budget_exhausted is False
        assert "finished in well under two seconds" in (run.output_text or "")

    @pytest.mark.asyncio
    async def test_off_leaves_the_previous_engine(self, runner, monkeypatch) -> None:
        """`off` must be the engine that shipped: the wallclock self-check ends
        the run, as a timeout, with no budget_exhausted flag from this path."""
        monkeypatch.setenv("ROBOTHOR_STEP_EFFICIENCY_MODE", "off")

        async def endless(**_kw):
            await asyncio.sleep(0.05)
            return _response(tool_calls=[_tool_call(1)])

        runner.registry.execute = AsyncMock(return_value={"content": "ok"})
        with _every_other_layer_dead(endless):
            run = await asyncio.wait_for(
                runner.execute("budgeted-agent", "work forever", agent_config=_agent()),
                timeout=30,
            )
        assert "hard timeout" in (run.error_message or "").lower()
