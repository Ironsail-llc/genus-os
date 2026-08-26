"""Finalization has a total budget, not just a per-step one.

#405 bounded each finalization step at 60s, which stopped the unbounded
hang: a run that had previously vanished for 331 unaccounted seconds now
completed. The very next occurrence showed the remaining hole.

    wd.log      stop_called       <- loop ended at its 1200s ceiling
    phases.log  execute_returned  <- 300 seconds later
                container killed by the harness backstop at 1500s

Three hundred seconds is exactly five steps timing out at 60s each. Every
individual bound held; nothing capped their SUM. The run finished — and
finished 3 seconds past the outer backstop, so it was killed anyway and its
stdout never flushed.

A per-step bound stops one hang. A total budget stops N of them compounding,
and it is the only kind of bound the caller can reason about: `execute()`
returns within the loop ceiling plus this, full stop.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.engine.run_budget import (
    FINALIZATION_TIMEOUT,
    FINALIZATION_TOTAL_BUDGET,
    FinalizationBudget,
)


class TestTheTotalBudget:
    def test_it_is_smaller_than_the_harness_margin(self):
        """The bench container allows task_timeout + 300s. Finalization must
        fit well inside that, or a completed run is killed anyway."""
        assert FINALIZATION_TOTAL_BUDGET < 300

    def test_it_admits_at_least_one_full_step(self):
        assert FINALIZATION_TOTAL_BUDGET >= FINALIZATION_TIMEOUT

    @pytest.mark.asyncio
    async def test_fast_steps_all_run(self):
        budget = FinalizationBudget()
        results = []
        for i in range(6):

            async def quick(n=i):
                return n

            results.append(await budget.run(quick(), f"step{i}"))
        assert results == [0, 1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_the_budget_caps_the_total_not_just_each_step(self):
        budget = FinalizationBudget(total=0.4, per_step=0.15)

        async def slow():
            await asyncio.sleep(5)

        started = asyncio.get_running_loop().time()
        for i in range(20):
            await budget.run(slow(), f"slow{i}")
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 2.0, f"twenty slow steps took {elapsed:.1f}s — no total cap"

    @pytest.mark.asyncio
    async def test_later_steps_are_skipped_once_exhausted(self):
        budget = FinalizationBudget(total=0.2, per_step=0.15)

        async def slow():
            await asyncio.sleep(5)

        await budget.run(slow(), "first")
        ran = False

        async def marker():
            nonlocal ran
            ran = True

        await budget.run(marker(), "second")
        assert not ran, "a step ran after the total budget was exhausted"
        assert budget.exhausted

    @pytest.mark.asyncio
    async def test_a_skipped_step_does_not_leak_its_coroutine(self):
        """An un-awaited coroutine emits a RuntimeWarning and hides a bug."""
        import warnings

        budget = FinalizationBudget(total=0.05, per_step=0.02)

        async def slow():
            await asyncio.sleep(5)

        await budget.run(slow(), "first")
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)

            async def never():
                return 1

            await budget.run(never(), "skipped")

    @pytest.mark.asyncio
    async def test_failures_never_propagate(self):
        budget = FinalizationBudget()

        async def boom():
            raise RuntimeError("teardown exploded")

        assert await budget.run(boom(), "boom") is None

    @pytest.mark.asyncio
    async def test_exhaustion_is_logged_once_with_the_step_name(self, caplog):
        # step one consumes effectively the whole allowance
        budget = FinalizationBudget(total=0.06, per_step=0.05)

        async def slow():
            await asyncio.sleep(5)

        with caplog.at_level("WARNING"):
            await budget.run(slow(), "sandbox_stop")

            async def after():
                return 1

            await budget.run(after(), "delivery")
        assert "sandbox_stop" in caplog.text
        assert "delivery" in caplog.text, "the skipped step must name itself"


class TestTheRunnerUsesTheBudget:
    @staticmethod
    def _source() -> str:
        from pathlib import Path

        import robothor.engine.runner as m

        return Path(m.__file__).read_text(encoding="utf-8")

    def test_one_budget_spans_the_whole_run(self):
        body = self._source()
        assert "FinalizationBudget(" in body, "the runner never creates a budget"
        assert body.count("FinalizationBudget(") == 1, (
            "more than one budget per run defeats the total cap"
        )
