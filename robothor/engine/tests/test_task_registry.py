"""Tests for the background task registry."""

from __future__ import annotations

import asyncio

import pytest

from robothor.engine.task_registry import TaskRegistry, get_task_registry, reset_task_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the global singleton between tests."""
    reset_task_registry()
    yield
    reset_task_registry()


class TestTaskRegistry:
    @pytest.mark.asyncio
    async def test_spawn_runs_coroutine(self) -> None:
        registry = TaskRegistry()
        result: list[int] = []

        async def work():
            result.append(42)

        task = registry.spawn(work(), name="test")
        await task
        assert result == [42]

    @pytest.mark.asyncio
    async def test_spawn_tracks_pending(self) -> None:
        registry = TaskRegistry()
        event = asyncio.Event()

        async def block():
            await event.wait()

        task = registry.spawn(block(), name="blocker")
        assert registry.pending_count == 1
        event.set()
        await task
        # Give done callback time to fire
        await asyncio.sleep(0)
        assert registry.pending_count == 0

    @pytest.mark.asyncio
    async def test_failed_task_logged_and_removed(self) -> None:
        registry = TaskRegistry()

        async def fail():
            raise RuntimeError("boom")

        task = registry.spawn(fail(), name="fail-task")
        # Wait for task to complete
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)
        assert registry.pending_count == 0

    @pytest.mark.asyncio
    async def test_cancelled_task_removed(self) -> None:
        registry = TaskRegistry()

        async def block():
            await asyncio.sleep(999)

        task = registry.spawn(block(), name="cancel-me")
        assert registry.pending_count == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert registry.pending_count == 0

    @pytest.mark.asyncio
    async def test_drain_waits_for_completion(self) -> None:
        registry = TaskRegistry()
        done = []

        async def quick():
            await asyncio.sleep(0.01)
            done.append(True)

        registry.spawn(quick(), name="quick-1")
        registry.spawn(quick(), name="quick-2")
        assert registry.pending_count == 2
        await registry.drain(timeout=5.0)
        assert len(done) == 2
        assert registry.pending_count == 0

    @pytest.mark.asyncio
    async def test_drain_cancels_on_timeout(self) -> None:
        registry = TaskRegistry()

        async def forever():
            await asyncio.sleep(999)

        registry.spawn(forever(), name="forever")
        await registry.drain(timeout=0.05)
        # Task should be cancelled and removed
        await asyncio.sleep(0.01)
        assert registry.pending_count == 0

    @pytest.mark.asyncio
    async def test_cancel_all(self) -> None:
        registry = TaskRegistry()

        async def block():
            await asyncio.sleep(999)

        registry.spawn(block(), name="a")
        registry.spawn(block(), name="b")
        count = registry.cancel_all()
        assert count == 2

    def test_get_task_registry_singleton(self) -> None:
        r1 = get_task_registry()
        r2 = get_task_registry()
        assert r1 is r2

    def test_reset_creates_new_instance(self) -> None:
        r1 = get_task_registry()
        reset_task_registry()
        r2 = get_task_registry()
        assert r1 is not r2


def test_rejected_spawn_closes_unowned_coroutine_without_a_running_loop():
    import inspect

    async def work():
        raise AssertionError("rejected work must not execute")

    pending = work()
    try:
        with pytest.raises(RuntimeError, match="no running event loop"):
            TaskRegistry().spawn(pending)
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED
    finally:
        pending.close()


async def test_task_factory_rejection_closes_unowned_coroutine(monkeypatch):
    import inspect

    async def work():
        raise AssertionError("rejected work must not execute")

    def refuse(*args, **kwargs):
        raise RuntimeError("task factory rejected admission")

    pending = work()
    monkeypatch.setattr(asyncio, "create_task", refuse)
    try:
        with pytest.raises(RuntimeError, match="rejected admission"):
            TaskRegistry().spawn(pending)
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED
    finally:
        pending.close()


@pytest.mark.parametrize("timeout", [False, True])
async def test_drain_includes_persistence_spawned_by_finishing_work(timeout):
    registry = TaskRegistry()
    completed, children = [], []

    async def persist():
        await asyncio.sleep(999 if timeout else 0.03)
        completed.append(True)

    async def finish():
        await asyncio.sleep(0)
        children.append(registry.spawn(persist(), name="late-persistence"))

    registry.spawn(finish(), name="finishing-worker")
    try:
        await registry.drain(timeout=0.04 if timeout else 1)
        if timeout:
            assert children[0].cancelled()
        else:
            assert completed == [True]
        assert registry.pending_count == 0
    finally:
        registry.cancel_all()
        await asyncio.gather(*children, return_exceptions=True)
