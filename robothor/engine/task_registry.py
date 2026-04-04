"""Background task registry — track all fire-and-forget asyncio tasks.

Replaces bare ``asyncio.create_task()`` calls throughout the engine with a
tracked ``spawn()`` that stores task references, logs exceptions on completion,
and supports graceful drain on shutdown.

Usage::

    from robothor.engine.task_registry import get_task_registry

    registry = get_task_registry()
    registry.spawn(some_coroutine(), name="hook:agent_end")

    # During shutdown:
    await registry.drain(timeout=10.0)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine  # noqa: TC003
from typing import Any

logger = logging.getLogger(__name__)

_instance: TaskRegistry | None = None


class TaskRegistry:
    """Singleton registry for background asyncio tasks."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        """Create a tracked asyncio task.

        The task reference is stored until completion. On failure, the
        exception is logged at ERROR level.
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(
                "Background task '%s' failed: %s: %s",
                task.get_name(),
                type(exc).__name__,
                exc,
            )

    @property
    def pending_count(self) -> int:
        """Number of tasks still running."""
        return len(self._tasks)

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait for all pending tasks to complete (with timeout).

        Called during graceful shutdown to ensure background work finishes.
        """
        if not self._tasks:
            return
        logger.info(
            "TaskRegistry: draining %d pending tasks (timeout=%.1fs)", len(self._tasks), timeout
        )
        done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            logger.warning("TaskRegistry: cancelled %d tasks after drain timeout", len(pending))
            await asyncio.gather(*pending, return_exceptions=True)

    def cancel_all(self) -> int:
        """Cancel all pending tasks. Returns number cancelled."""
        count = 0
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                count += 1
        return count


def get_task_registry() -> TaskRegistry:
    """Return the global TaskRegistry singleton (created on first call)."""
    global _instance
    if _instance is None:
        _instance = TaskRegistry()
    return _instance


def reset_task_registry() -> None:
    """Reset the singleton. Only for testing."""
    global _instance
    _instance = None
