"""Retain a deep thread's result when its delivery task is cancelled."""

import asyncio
import contextlib
import logging
from uuid import uuid4

from robothor.engine.models import RunStep, StepType
from robothor.engine.task_registry import get_task_registry


def _record_late(worker, session, finish):
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


async def _wait(worker):
    with contextlib.suppress(Exception):
        await asyncio.shield(worker)


async def owned_deep_call(session, finish, progress_stop, progress_task, function, **kwargs):
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
