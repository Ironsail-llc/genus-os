"""Release a chat answer before run finalization (``ROBOTHOR_EARLY_DONE``).

A chat turn's final text is settled long before the run is: telemetry, post-run
guardrail bookkeeping, claim verification, the recent-actions file, the
timeline insert and the run-row persist all follow it, and none of them change
what the person is shown. With the flag on, ``AgentRunner.execute(on_complete=)``
hands the completed run to the caller first and finishes the rest as a task
under the engine task registry, so ``genus run``'s drain and daemon shutdown
still wait for it.

The split, made explicit here:

* **Before the answer** — :func:`apply_output_policies`: the post-run policies
  that can change the shown result. Today that is the deliverable contract in
  enforce mode, which turns a ``completed`` run into a ``failed`` one.
* **After the answer** — everything else in ``_publish_run_telemetry`` and
  ``_finish_run``: ``check_post_run`` (CRM task bookkeeping only), the
  completion contract, claim and metric-trend verification, hooks, persistence.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from robothor.engine.feature_flags import early_done_enabled
from robothor.engine.sanitize import sanitize_log as _sanitize
from robothor.engine.task_registry import get_task_registry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from robothor.engine.models import AgentRun

logger = logging.getLogger(__name__)

T = TypeVar("T")

_APPLIED = "_output_policies_applied"
#: How long background finalization waits for a released answer to be sent.
RELEASE_GRACE_S = 0.05


def apply_output_policies(run: AgentRun, session: Any, workspace: Any) -> None:
    """Run the post-run policies that can change the shown result, once per run."""
    if session is not None and getattr(session, _APPLIED, False):
        return
    from robothor.engine.deliverable_verdict import record_deliverable_verdicts

    record_deliverable_verdicts(run, session, workspace)
    if session is not None:
        setattr(session, _APPLIED, True)


async def release_then_finalize(
    runner: Any,
    completed: AgentRun,
    session: Any,
    on_complete: Callable[[AgentRun], Awaitable[None]],
    **finish_kwargs: Any,
) -> AgentRun:
    """Apply the output-altering policies, release the run, finalize in the background."""
    try:
        apply_output_policies(completed, session, runner.config.workspace)
    except Exception as exc:  # degrade as the flag-off path does: lose the verdict, not the run
        logger.error(
            "run %s: output policy failed: %s", completed.id, _sanitize(exc), exc_info=True
        )
    try:
        await on_complete(completed)
    finally:  # a failed release must not leave the run unfinalized
        with _background_lane():
            get_task_registry().spawn(
                _finalize(runner, completed, session=session, **finish_kwargs),
                name=f"finalize-run:{completed.id}",
            )
    return completed


def _background_lane() -> AbstractContextManager[None]:
    """The background execution lane, when the engine has lanes; else a no-op.

    A spawned task copies its creator's context, so without this the
    finalization would inherit the interactive run's lane and its workers.
    """
    try:
        from robothor.engine.lanes import Lane, in_lane
    except ImportError:
        return nullcontext()
    lane: AbstractContextManager[None] = in_lane(Lane.BACKGROUND)
    return lane


async def _finalize(runner: Any, run: AgentRun, **finish_kwargs: Any) -> None:
    """Telemetry and ``_finish_run`` after the answer went out.

    The caller already has the answer, so a failure here is logged and recorded
    on the run row rather than raised.
    """
    # Let the released answer reach its socket first. The loop is idle here, so
    # a short real wait covers every task hop on the way (HTTP middleware, the
    # SSE generator); _finish_run still blocks the loop until it goes off-loop.
    await asyncio.sleep(RELEASE_GRACE_S)
    try:
        runner._publish_run_telemetry(finish_kwargs.get("trace"), run)
        runner._finish_run(run, **finish_kwargs)
    except Exception as exc:  # noqa: BLE001 — the answer is already delivered
        note = f"Finalization failed after the answer was delivered: {exc}"
        logger.error("run %s: %s", run.id, _sanitize(note), exc_info=True)
        run.error_message = "\n".join(x for x in (run.error_message, note) if x)[:4000]
        await runner._persist_run(run)


class EarlyRelease(Generic[T]):
    """Caller side: deliver an answer from ``on_complete``, or after ``execute()``.

    ``deliver`` is the caller's own send (an SSE ``done``, a Telegram message).
    Pass :meth:`callback` as ``on_complete``; afterwards :meth:`result_or` returns
    what the early send produced, or performs the send now when it did not run.
    """

    def __init__(self, deliver: Callable[[AgentRun], Awaitable[T]]) -> None:
        self._deliver = deliver
        self.released = False
        self.result: T | None = None

    async def _release(self, run: AgentRun) -> None:
        self.result = await self._deliver(run)
        self.released = True

    def callback(self) -> Callable[[AgentRun], Awaitable[None]] | None:
        return self._release if early_done_enabled() else None

    async def result_or(self, run: AgentRun) -> T:
        if self.released:
            return self.result  # type: ignore[return-value]
        return await self._deliver(run)

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        """Fire-and-forget follow-up work; tracked by the registry after an early release."""
        if self.released:
            get_task_registry().spawn(coro, name=name)
        else:
            asyncio.create_task(coro, name=name)


def chat_done_event(run: AgentRun, delivered: dict[str, Any]) -> dict[str, Any]:
    """The webchat SSE ``done`` event for a finished turn, with the run's metadata."""
    return {
        "event": "done",
        "data": {
            **delivered,
            "status": run.status.value,
            "model": run.model_used,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "duration_ms": run.duration_ms,
            "total_cost_usd": round(run.total_cost_usd, 4),
            "run_id": run.id,
        },
    }
