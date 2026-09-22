"""Cancellation retains ownership of a deep worker until its result is recorded."""

import asyncio
import threading
from unittest.mock import patch

import pytest

from robothor.engine.models import RunStatus
from robothor.engine.runner import AgentRunner
from robothor.engine.task_registry import get_task_registry


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_cancelled_deep_worker_records_late_result_without_false_completion(
    engine_config, cancel_waiter
):
    entered, release = threading.Event(), threading.Event()
    finished = []

    def worker(**kwargs):
        entered.set()
        assert release.wait(5)
        return {"response": "Late synthetic analysis", "cost_usd": 0.12}

    runner = AgentRunner(engine_config)
    with (
        patch("robothor.engine.runner.create_run"),
        patch.object(runner, "_finish_run", side_effect=lambda run: finished.append(run) or run),
        patch("robothor.engine.rlm_tool.execute_deep_reason", side_effect=worker),
    ):
        task = asyncio.create_task(
            runner.execute_deep("Synthetic analysis", user_id="operator", user_role="owner")
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert not finished
            if cancel_waiter:
                await get_task_registry().drain(timeout=0)
            release.set()
            async with asyncio.timeout(2):
                while not finished:
                    await asyncio.sleep(0.01)
            await get_task_registry().drain(timeout=2)
            assert len(finished) == 1
            assert finished[0].status == RunStatus.CANCELLED
            assert finished[0].total_cost_usd == 0.12
            assert any(
                step.tool_output.get("response") == "Late synthetic analysis"
                for step in finished[0].steps
                if isinstance(step.tool_output, dict)
            )
            assert not any(
                not t.done() and "execute_deep.<locals>._progress_loop" in t.get_coro().__qualname__
                for t in asyncio.all_tasks()
            )
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
