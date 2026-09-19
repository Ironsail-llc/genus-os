"""Native internal progress hooks capture one child before siblings finish."""

import asyncio

import pytest

from robothor.engine.tools.handlers import spawn


@pytest.mark.asyncio
async def test_progress_survives_batch_cancellation_and_is_not_a_tool_argument(monkeypatch):
    recorded = asyncio.Event()
    stopped = []
    results = []

    async def child(args, **kwargs):
        if args["message"] == "fast":
            return {"run_id": "fast", "status": "completed"}
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append("slow")

    async def capture(index, result):
        results.append((index, result))
        recorded.set()

    monkeypatch.setattr(spawn, "_handle_spawn_agent", child)
    task = asyncio.create_task(
        spawn._handle_spawn_agents(
            {"agents": [{"agent_id": "worker", "message": m} for m in ("fast", "slow")]},
            _on_result=capture,
        )
    )
    await asyncio.wait_for(recorded.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert results == [(0, {"run_id": "fast", "status": "completed"})]
    assert stopped == ["slow"]

    result = await spawn._handle_spawn_agents(
        {"agents": [{"agent_id": "worker", "message": "fast"}], "_on_result": capture}
    )
    assert result["completed"] == 1
    assert len(results) == 1
