"""The bound on what a run may spend after its loop ends, untested until now.

`finalization_budget.py` was written after two incidents three days apart: a
run with a 1200s ceiling that reached 3110s because the loop ended at 1169s
and the next 331 seconds were spent in teardown with every enforcement layer
already stopped, and then a run that returned exactly 300 seconds late —
five steps timing out at 60s each, each bound holding, nothing capping their
sum.

It is 153 lines of pure control logic guarding the one stretch of a run
where no watchdog is running, and nothing referenced it from a test. The
module's whole promise is that it never propagates and never overruns; both
are properties, and properties are what tests are for.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.engine.finalization_budget import (
    FINALIZATION_TIMEOUT,
    FINALIZATION_TOTAL_BUDGET,
    FinalizationBudget,
    bounded_finalization,
)


async def _ok(value: str = "done"):
    return value


async def _hangs():
    await asyncio.sleep(30)
    return "never"


async def _raises():
    raise RuntimeError("teardown blew up")


class TestBoundedFinalization:
    @pytest.mark.asyncio
    async def test_a_normal_step_returns_its_value(self):
        assert await bounded_finalization(_ok("delivered"), "delivery") == "delivered"

    @pytest.mark.asyncio
    async def test_a_hanging_step_is_abandoned_not_awaited_forever(self):
        result = await asyncio.wait_for(
            bounded_finalization(_hangs(), "sandbox-teardown", timeout=0.05), timeout=5
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_a_raising_step_never_propagates(self):
        """An exception here would replace whatever the run was reporting."""
        assert await bounded_finalization(_raises(), "verification") is None

    @pytest.mark.asyncio
    async def test_the_default_bound_is_the_module_constant(self):
        assert FINALIZATION_TIMEOUT == 60


class TestTheSharedTotal:
    """The second incident: every per-step bound held, nothing capped the sum."""

    @pytest.mark.asyncio
    async def test_steps_draw_down_the_shared_total(self):
        budget = FinalizationBudget(total=0.30, per_step=0.05)
        assert not budget.exhausted
        for _ in range(6):
            await budget.run(_hangs(), "slow-step")
        assert budget.exhausted, "six timing-out steps did not exhaust a 0.3s total"

    @pytest.mark.asyncio
    async def test_an_exhausted_budget_skips_rather_than_attempts(self):
        budget = FinalizationBudget(total=0.05, per_step=0.05)
        await budget.run(_hangs(), "first")
        ran = {"second": False}

        async def _second():
            ran["second"] = True
            return "x"

        assert await budget.run(_second(), "second") is None
        assert not ran["second"], "a step ran after the budget was spent"

    @pytest.mark.asyncio
    async def test_a_skipped_coroutine_is_closed_not_leaked(self):
        """An abandoned coroutine warns and hides what it would have done."""
        budget = FinalizationBudget(total=0.05, per_step=0.05)
        await budget.run(_hangs(), "first")

        coro = _ok()
        await budget.run(coro, "skipped")
        assert coro.cr_frame is None, "the skipped coroutine was never closed"

    @pytest.mark.asyncio
    async def test_a_fast_step_leaves_budget_for_later_ones(self):
        budget = FinalizationBudget(total=1.0, per_step=0.5)
        assert await budget.run(_ok("a"), "first") == "a"
        assert await budget.run(_ok("b"), "second") == "b"
        assert not budget.exhausted

    @pytest.mark.asyncio
    async def test_a_raising_step_still_consumes_budget_and_does_not_propagate(self):
        budget = FinalizationBudget(total=1.0, per_step=0.5)
        assert await budget.run(_raises(), "boom") is None
        assert await budget.run(_ok("after"), "after") == "after"

    def test_the_total_is_the_module_constant(self):
        assert FINALIZATION_TOTAL_BUDGET == 150

    def test_a_tiny_total_is_immediately_exhausted(self):
        """Below the useful floor there is no point starting a step."""
        assert FinalizationBudget(total=0.0).exhausted
