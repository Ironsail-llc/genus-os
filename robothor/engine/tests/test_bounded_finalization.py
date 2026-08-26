"""Run finalization is bounded. Nothing after the loop may hang forever.

The 3110s-against-a-1200s-ceiling mystery, solved by the trace shipped in
#394. On a second occurrence the evidence was unambiguous:

    wd.log      ... tick elapsed=1140 hard=1200
                    stop_called            <- t+1169s
    phases.log  run_one_start              <- t+0s
                (execute_returned NEVER stamped)
                container killed by the harness backstop at t+1500s

The agent loop ENDED at 1169s, inside its ceiling. The watchdog then stopped
ITSELF in the `finally` — and 331 further seconds passed with no protection
of any kind, because `execute()` never returned.

The wedge was never in the loop. It is in everything that runs after it:
sandbox teardown, delivery, verification, the finalizing DB writes. All of it
sits after `watchdog.stop()`, and the `except (TimeoutError, CancelledError)`
handler's own `_finish_run` sits OUTSIDE the `async with asyncio.timeout`
block entirely. Three enforcement layers could not see this because all three
had already finished by the time it started.

A run may fail to finalize. It may not hang doing so.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.engine.run_budget import (
    FINALIZATION_TIMEOUT,
    bounded_finalization,
)


class TestTheBound:
    def test_it_is_generous_but_finite(self):
        """Long enough for a slow DB write and a container teardown; short
        enough that a hang cannot outlive the run that owns it."""
        assert 30 <= FINALIZATION_TIMEOUT <= 300

    @pytest.mark.asyncio
    async def test_a_fast_step_is_untouched(self):
        async def quick():
            return "done"

        assert await bounded_finalization(quick(), "quick") == "done"

    @pytest.mark.asyncio
    async def test_a_hung_step_is_cut_off_not_awaited_forever(self):
        async def hang():
            await asyncio.sleep(3600)

        started = asyncio.get_running_loop().time()
        result = await bounded_finalization(hang(), "hang", timeout=0.2)
        elapsed = asyncio.get_running_loop().time() - started
        assert result is None
        assert elapsed < 5, f"finalization waited {elapsed:.1f}s on a hung step"

    @pytest.mark.asyncio
    async def test_a_failing_step_never_propagates(self):
        """Finalization runs on the way out. An exception here would replace
        whatever the run was actually reporting."""

        async def boom():
            raise RuntimeError("teardown exploded")

        assert await bounded_finalization(boom(), "boom") is None

    @pytest.mark.asyncio
    async def test_a_cancelled_step_does_not_escape(self):
        async def cancelled():
            raise asyncio.CancelledError

        assert await bounded_finalization(cancelled(), "cancelled") is None

    @pytest.mark.asyncio
    async def test_the_step_name_reaches_the_log(self, caplog):
        async def hang():
            await asyncio.sleep(3600)

        with caplog.at_level("WARNING"):
            await bounded_finalization(hang(), "sandbox_stop", timeout=0.1)
        assert "sandbox_stop" in caplog.text, "a hang that does not name itself is unfixable"


class TestTheRunnerUsesIt:
    """A bound the runner never applies is decoration."""

    @staticmethod
    def _source() -> str:
        from pathlib import Path

        import robothor.engine.runner as m

        return Path(m.__file__).read_text(encoding="utf-8")

    def test_sandbox_teardown_is_bounded(self):
        body = self._source()
        assert "bounded_finalization" in body, "the runner never bounds anything"
        idx = body.index("await sandbox.stop()") if "await sandbox.stop()" in body else -1
        assert idx == -1, (
            "sandbox.stop() is still awaited unbounded in the finally block — "
            "a hung container teardown runs after the watchdog has stopped"
        )

    def test_the_cancellation_handler_bounds_its_finish_run(self):
        """That `_finish_run` sits outside the outer asyncio.timeout, so it is
        the one call in the engine with no wall-clock protection at all."""
        body = self._source()
        handler = body[body.index("except (TimeoutError, asyncio.CancelledError)") :]
        handler = (
            handler[: handler.index("\n    async def ")]
            if "\n    async def " in handler
            else handler
        )
        assert "bounded_finalization" in handler, (
            "the cancellation path still finalizes without a bound"
        )
