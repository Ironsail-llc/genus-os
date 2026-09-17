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
        # Two seconds, not four. Only the three tests that deliberately use
        # REAL time read this — the rest drive `_budget_clock` — and for those
        # three the outcome is inevitable rather than raced, so the number only
        # decides how long the suite waits to watch it happen.
        timeout_seconds=kw.pop("timeout_seconds", 2),
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


#: Budget seconds each model call spends. With a 1200s budget and the rung at
#: 50 %, three calls cross the whole ladder: turns 0 and 1 below it, turn 2
#: above it, and the fourth look expires the run.
_STEP = 400


class _Clock:
    """A budget clock the test moves by hand. Nothing here waits on the real one."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@contextlib.contextmanager
def _without_step_persistence():
    """Take the per-iteration database write out of these tests.

    The subject here is the loop's budget ladder, not persistence. Leaving the
    write in made the outcome depend on whether a database happens to be
    running: `flush_new_steps_sync` goes through `tracking.create_steps_batch`,
    which is wrapped in `retry.py`'s exponential backoff — so on a machine
    with no database each iteration SLEEPS through its retries. Measured here:
    ~0.12s per iteration with PostgreSQL up, ~3.6s with it down. CI has no
    database, which is why twelve iterations sailed past pytest-timeout's 30s
    there and took a second and a half on this box.

    Patched at the session, not at the connection: refusing connections makes
    every other best-effort caller retry too, which was slower still.
    """
    from robothor.engine.session import AgentSession

    with patch.object(AgentSession, "flush_new_steps_sync", lambda self: 0):
        yield


@contextlib.contextmanager
def _budget_clock(seconds: int = 1200, fraction: float = 0.5):
    """Give the run's `BudgetStop` a clock the test owns.

    The first cut of the two wrap-up tests below used a 4-second budget and a
    50 % fraction and let real time carry the run across the rung. That is a
    two-second window a loop ITERATION has to start inside, and how long an
    iteration takes is environment-dependent — the model call, the tool turn
    and whatever the step flush does with the database. CI's 3.13 runner took
    longer than the window for a single iteration, so the run went straight
    from `normal` to `expired`, never narrowed, and both tests failed while
    the hard-stop test beside them passed. The behaviour was right; the
    measurement was a coin toss.

    So the clock advances only where this test says it does — once per model
    call — and the rung is crossed at a turn number, not at a second.

    The step is sized so the whole ladder takes THREE model calls: turns 0 and
    1 below the rung, turn 2 above it, and the fourth look expires the run.
    Twelve turns was the first cut and it was four times the work for the same
    assertion — and on a machine with no database, four times a cost that is
    not this test's subject.
    """
    from robothor.engine.run_deadline import BudgetStop, RunBudget

    clock = _Clock()

    def _for_run(_cls, _agent_config, *, mode, watchdog=None, session=None):
        return BudgetStop(
            budget=RunBudget(seconds=seconds, source="external"),
            mode=mode,
            session=session,
            fraction=fraction,
            grace=0,
            now=clock,
            started=clock(),
        )

    with patch.object(BudgetStop, "for_run", classmethod(_for_run)):
        yield clock


@contextlib.contextmanager
def _every_other_layer_dead(llm):
    with (
        patch("asyncio.timeout", lambda *_: contextlib.nullcontext()),
        patch.object(_StallWatchdog, "start", lambda self, task: None),
        patch("robothor.engine.runner.create_run"),
        patch("robothor.engine.runner.update_run"),
        patch("robothor.engine.run_finalizer.create_step"),
        patch("litellm.acompletion", side_effect=llm),
        _without_step_persistence(),
    ):
        yield


class TestTheBudgetEndsTheRun:
    @pytest.mark.asyncio
    async def test_a_run_that_never_stops_is_ended_by_its_budget(self, runner) -> None:
        calls = 0

        with _budget_clock() as clock:

            async def endless(**_kw):
                nonlocal calls
                calls += 1
                clock.advance(_STEP)
                return _response(tool_calls=[_tool_call(calls)])

            runner.registry.execute = AsyncMock(return_value={"content": "ok"})
            with _every_other_layer_dead(endless):
                run = await asyncio.wait_for(
                    runner.execute("budgeted-agent", "work forever", agent_config=_agent()),
                    timeout=120,
                )
        assert calls == 3, f"the budget was not spent where the arithmetic says: {calls}"
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
        with _budget_clock() as clock:

            async def endless(**_kw):
                clock.advance(_STEP)
                return _response(tool_calls=[_tool_call(1)])

            runner.registry.execute = AsyncMock(return_value={"content": "ok"})
            with _every_other_layer_dead(endless):
                run = await asyncio.wait_for(
                    runner.execute("budgeted-agent", "work forever", agent_config=_agent()),
                    timeout=120,
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
        """At the wrap-up rung the model keeps writing and loses `exec`.

        1200s budget, wrap-up at 50 %, 400s of budget spent per model call —
        so the rung is crossed at turn 2 and the budget at turn 3, by
        arithmetic rather than by how fast the box is.
        """
        seen: list[list[str]] = []

        with _budget_clock() as clock:

            async def watch_tools(**kw):
                seen.append(
                    [s["function"]["name"] for s in (kw.get("tools") or []) if "function" in s]
                )
                clock.advance(_STEP)
                return _response(tool_calls=[_tool_call(len(seen))])

            runner.registry.execute = AsyncMock(return_value={"content": "ok"})
            with _every_other_layer_dead(watch_tools):
                await asyncio.wait_for(
                    runner.execute("budgeted-agent", "work forever", agent_config=_agent()),
                    timeout=120,
                )
        assert len(seen) == 3, f"the ladder was not crossed where the arithmetic says: {len(seen)}"
        assert all("exec" in turn for turn in seen[:2]), "the full tool set before the rung"
        assert all("exec" not in turn for turn in seen[2:]), "wrap-up never narrowed the tools"
        assert all("write_file" in turn for turn in seen), "wrap-up took away the ability to write"

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

        with _budget_clock() as clock:

            async def always_asks_for_exec(**kw):
                names = {s["function"]["name"] for s in (kw.get("tools") or []) if "function" in s}
                offered.append("exec" in names)
                clock.advance(_STEP)
                return _response(tool_calls=[_tool_call(len(offered))])

            runner.registry.execute = AsyncMock(side_effect=_execute)
            with _every_other_layer_dead(always_asks_for_exec):
                await asyncio.wait_for(
                    runner.execute("budgeted-agent", "run things", agent_config=_agent()),
                    timeout=120,
                )
        narrowed = [i for i, had in enumerate(offered) if not had]
        assert narrowed == list(range(2, len(offered))), (
            f"the rung was not crossed at turn 2: {narrowed}"
        )
        assert executed, "no `exec` ran at all before wrap-up — the test proves nothing"
        assert not set(executed) & set(narrowed), (
            f"`exec` ran on turns where it was withdrawn: {sorted(set(executed) & set(narrowed))}"
        )

    @pytest.mark.asyncio
    async def test_one_iteration_may_span_the_whole_wrapup_window(self, runner) -> None:
        """And when it does, the run ends honestly without ever wrapping up.

        This is what CI's 3.13 runner hit: the rungs are read at the TOP of an
        iteration, so an iteration longer than the wrap-up window takes the run
        straight from `normal` to `expired`. Pinned as the behaviour it is —
        nothing is narrowed because there is no turn left to narrow for, and
        the stop still records itself — so the next reader does not "fix" it by
        widening a timing margin in a test again.
        """
        seen: list[list[str]] = []

        with _budget_clock() as clock:

            async def one_long_turn(**kw):
                seen.append(
                    [s["function"]["name"] for s in (kw.get("tools") or []) if "function" in s]
                )
                clock.advance(1200)  # the whole budget, inside one iteration
                return _response(tool_calls=[_tool_call(len(seen))])

            runner.registry.execute = AsyncMock(return_value={"content": "ok"})
            with _every_other_layer_dead(one_long_turn):
                run = await asyncio.wait_for(
                    runner.execute("budgeted-agent", "one long turn", agent_config=_agent()),
                    timeout=120,
                )
        assert len(seen) == 1
        assert "exec" in seen[0], "nothing was narrowed, because no turn was left to narrow for"
        assert run.budget_exhausted is True
        assert "budget" in (run.error_message or "").lower()

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
