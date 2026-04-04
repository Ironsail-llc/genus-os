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
