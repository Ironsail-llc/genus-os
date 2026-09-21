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


@pytest.mark.asyncio
async def test_trusted_child_scopes_are_isolated_and_not_model_arguments(monkeypatch):
    from contextlib import contextmanager
    from contextvars import ContextVar

    current = ContextVar("fixture_child", default=None)
    seen, closed = [], []

    @contextmanager
    def child_scope(index):
        token = current.set(index)
        try:
            yield
        finally:
            closed.append(index)
            current.reset(token)

    async def child(args, **kwargs):
        before = current.get()
        await asyncio.sleep(0)
        seen.append((before, current.get()))
        return {"status": "completed"}

    monkeypatch.setattr(spawn, "_handle_spawn_agent", child)
    args = {"agents": [{"agent_id": "worker", "message": str(i)} for i in range(3)]}
    await spawn._handle_spawn_agents(args, _child_scope=child_scope)
    assert seen == [(0, 0), (1, 1), (2, 2)]
    assert set(closed) == {0, 1, 2}
    assert current.get() is None
    seen.clear()
    await spawn._handle_spawn_agents({**args, "_child_scope": child_scope})
    assert seen == [(None, None)] * 3
