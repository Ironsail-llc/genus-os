"""Retain a deep thread's result when its delivery task is cancelled."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import asyncio
import contextlib
import logging
from uuid import uuid4

from robothor.engine.models import RunStep, StepType
from robothor.engine.task_registry import get_task_registry


def _record_late(worker: asyncio.Task[Any], session: Any, finish: Callable[[Any], Any]) -> None:
    if worker.cancelled():
        # Loop shutdown does not prove the OS thread ended. Leave the audit open.
        return
    try:
        result = worker.result()
    except Exception as error:
        result = {"error": str(error)}
    try:
        session.run.total_cost_usd += result.get("cost_usd", 0.0)
        session.run.steps.append(
            RunStep(
                id=str(uuid4()),
                run_id=session.run.id,
                step_number=len(session.run.steps) + 1,
                step_type=StepType.DEEP_REASON,
                tool_name="deep_reason",
                tool_output=result,
            )
        )
        finish(
            session.cancelled("Deep execution was interrupted. Its returned result is recorded.")
        )
    except Exception as error:
        logging.getLogger(__name__).error(
            "Late deep result persistence failed (%s)", type(error).__name__
        )


async def _wait(worker: asyncio.Task[Any]) -> None:
    with contextlib.suppress(Exception):
        await asyncio.shield(worker)


async def owned_deep_call(
    session: Any,
    finish: Callable[[Any], Any],
    progress_stop: asyncio.Event,
    progress_task: asyncio.Task[Any],
    function: Callable[..., Any],
    **kwargs: Any,
) -> Any:
    worker = asyncio.create_task(asyncio.to_thread(function, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # The thread may still be running. Keep its future and account for its
        # result exactly once, even if a registry drain cancels the waiter.
        worker.add_done_callback(lambda done: _record_late(done, session, finish))
        get_task_registry().spawn(_wait(worker), name=f"deep-worker:{session.run.id}")
        raise
    finally:
        progress_stop.set()
        progress_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await progress_task
