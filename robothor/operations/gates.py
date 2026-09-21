"""Tenant-scoped shared work gates and nonblocking exclusive maintenance gates."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeVar

from robothor.operations.store import Conflict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

T = TypeVar("T")


@contextmanager
def gate(operations: Any, scope: str, *, shared: bool = False) -> Iterator[Any]:
    """Hold a PostgreSQL transaction gate; a busy gate fails without waiting.

    The caller must acquire this before any settings or work-row locks. The
    yielded cursor can perform the maintenance transaction while exclusion holds.
    This complements, rather than replaces, durable job/action lease checks.
    """
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 200:
        raise ValueError("A bounded maintenance scope is required")
    key = json.dumps(["operation-gate", operations.tenant, scope], separators=(",", ":"))
    function = "pg_try_advisory_xact_lock_shared" if shared else "pg_try_advisory_xact_lock"
    with operations.transaction() as cur:
        cur.execute(f"SELECT {function}(hashtextextended(%s,0)) AS acquired", (key,))
        if not cur.fetchone()["acquired"]:
            raise Conflict("Maintenance gate is busy")
        yield cur


async def run_shared(operations: Any, scope: str, work: Callable[[], Awaitable[T]]) -> T:
    """Run async work under a shared gate; cancellation waits for actual cleanup.

    Cancelling the caller cannot free the maintenance gate while provider work
    is still running. Workers must retain their own bounded request timeouts.
    All connection acquisition and cleanup run outside the event loop.
    """

    async def execute() -> T:
        context = gate(operations, scope, shared=True)
        await asyncio.to_thread(context.__enter__)
        try:
            result = await work()
        except BaseException:
            await asyncio.to_thread(context.__exit__, *sys.exc_info())
            raise
        else:
            await asyncio.to_thread(context.__exit__, None, None, None)
            return result

    task = asyncio.create_task(execute())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Repeated cancellation must not cancel the independent cleanup task.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not task.cancelled():
            task.exception()  # Consume any failure; the caller still sees its cancellation.
        raise
