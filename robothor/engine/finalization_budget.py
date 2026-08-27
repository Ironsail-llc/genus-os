"""What a run may spend AFTER its loop ends.

A separate concern from `run_budget`, which bounds the loop itself. This
bounds everything that happens on the way out — sandbox teardown, delivery,
verification, the closing database writes — and it exists because that
stretch had no bound at all.

Two incidents, three days apart in the same investigation:

* A run with a 1200s ceiling reached 3110s. The loop had ENDED at 1169s and
  the watchdog had stopped itself; the next 331 seconds were spent somewhere
  in teardown with nothing watching. Every enforcement layer — the outer
  `asyncio.timeout`, the stall watchdog, the loop's own self-check — had
  already finished by the time the hang began.
* With a per-step bound in place, the next occurrence returned 300 seconds
  after the loop ended: exactly five steps timing out at 60s each. Each bound
  held; nothing capped their sum, and the run was killed by the outer
  backstop three seconds past its margin, losing its stdout.

Hence both: a per-step bound so one hang cannot run forever, and a shared
total so N of them cannot compound. A run may fail to finalize. It may not
hang doing so, and it may not take so long finishing that the caller gives
up on it first.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


#: Wall-clock allowed for any single finalization step — sandbox teardown,
#: delivery, verification, the closing DB writes.
#:
#: Everything after the agent loop runs AFTER `watchdog.stop()`, and the
#: cancellation handler's own `_finish_run` sits outside the outer
#: `asyncio.timeout` entirely. So this stretch had no protection of any kind,
#: which is how a run with a 1200s ceiling reached 3110s: the loop ended at
#: 1169s, the watchdog stopped itself, and the next 331 seconds were spent
#: somewhere in teardown with nothing left watching.
#:
#: Generous enough for a slow write or a container stop; finite, because a
#: run may fail to finalize but may not hang doing so.
FINALIZATION_TIMEOUT = 60


async def bounded_finalization(
    awaitable: Any,
    step: str,
    timeout: float = FINALIZATION_TIMEOUT,
) -> Any:
    """Await one finalization step under a bound, swallowing every failure.

    Returns the step's result, or None if it timed out or raised.

    Nothing here may propagate. Finalization runs on the way out of a run, so
    an exception raised at this point replaces whatever the run was actually
    reporting — the operator would see a teardown error instead of the real
    outcome. A timeout names the step, because a hang that does not say where
    it happened is the same mystery this bound exists to end.
    """
    import asyncio as _asyncio

    try:
        return await _asyncio.wait_for(awaitable, timeout=timeout)
    except TimeoutError:
        logger.warning(
            "Finalization step %r exceeded %.0fs and was abandoned — the run "
            "still reports its outcome, but this step did not complete",
            step,
            timeout,
        )
    except _asyncio.CancelledError:
        logger.warning("Finalization step %r was cancelled", step)
    except Exception as e:
        logger.warning("Finalization step %r failed: %s", step, e)
    return None


#: Total wall-clock for ALL finalization steps in one run.
#:
#: The per-step bound above stops one hang; nothing stopped N of them
#: compounding. Measured immediately after that bound shipped: the loop ended
#: at its 1200s ceiling, `execute()` returned 300 seconds later — exactly five
#: steps timing out at 60s each — and the run was killed by the outer backstop
#: three seconds past its margin, losing its stdout.
#:
#: 150s sits well inside the bench container's task_timeout+300s allowance and
#: is far more than healthy finalization has ever needed. It is also the only
#: bound a caller can reason about: `execute()` returns within the loop
#: ceiling plus this.
FINALIZATION_TOTAL_BUDGET = 150


class FinalizationBudget:
    """One wall-clock allowance shared by every finalization step in a run.

    Each step is bounded individually AND draws from a shared total. Once the
    total is spent, later steps are skipped rather than attempted: at that
    point the run is already past the point where anything it does will be
    seen, and attempting more only pushes it past the caller's own deadline.

    Nothing here propagates. Finalization runs on the way out, so an exception
    raised now would replace whatever the run was actually reporting.
    """

    def __init__(
        self,
        total: float = FINALIZATION_TOTAL_BUDGET,
        per_step: float = FINALIZATION_TIMEOUT,
    ) -> None:
        self._remaining = float(total)
        self._per_step = float(per_step)
        # A step handed a fraction of a second cannot finish anything; it only
        # adds latency before failing. Below this floor the budget is spent.
        self._min_useful = min(1.0, float(total) * 0.5)

    @property
    def exhausted(self) -> bool:
        # `_min_useful` is derived from the total, so a caller passing 0 gets a
        # floor of 0 and `0 < 0` is false — a zero budget would report itself
        # spendable and attempt steps. Not reachable today (the one production
        # caller takes the defaults) but the shape invites it.
        if self._remaining <= 0:
            return True
        return self._remaining < self._min_useful

    async def run(self, awaitable: Any, step: str) -> Any:
        """Await one step within both its own bound and the shared total."""
        import asyncio as _asyncio

        if self.exhausted:
            # Close the coroutine we are not going to await: an abandoned one
            # emits a RuntimeWarning and hides whatever it was going to do.
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            logger.warning(
                "Finalization budget exhausted — skipping step %r; the run "
                "still reports its outcome",
                step,
            )
            return None

        allowance = min(self._per_step, self._remaining)
        started = _asyncio.get_running_loop().time()
        try:
            return await _asyncio.wait_for(awaitable, timeout=allowance)
        except TimeoutError:
            logger.warning("Finalization step %r exceeded %.0fs and was abandoned", step, allowance)
        except _asyncio.CancelledError:
            logger.warning("Finalization step %r was cancelled", step)
        except Exception as e:
            logger.warning("Finalization step %r failed: %s", step, e)
        finally:
            self._remaining -= _asyncio.get_running_loop().time() - started
        return None
