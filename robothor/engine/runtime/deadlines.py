"""Enforce a trusted runtime deadline without owning retries or finalization."""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from asyncio import Timeout
    from collections.abc import Awaitable, Callable

    from robothor.engine.runtime.contracts import ExecutionContext

#: What `execute_before_deadline` hands back is whatever the callee returns.
_Result = TypeVar("_Result")

_active_window: ContextVar[Timeout | None] = ContextVar("runtime_deadline_window", default=None)


_owned_deadline: ContextVar[tuple[datetime, float] | None] = ContextVar(
    "runtime_deadline_owner", default=None
)


class RuntimeDeadlineError(TimeoutError):
    """The host deadline expired; already dispatched effects need reconciliation."""


def remaining(context: ExecutionContext | None = None) -> float | None:
    if context is None:
        from robothor.engine.runtime.current import active_context

        context = active_context.get()
    limits = []
    if context is not None and context.deadline is not None:
        limits.append((context.deadline - datetime.now(UTC)).total_seconds())
    parent = _owned_deadline.get()
    if parent is not None:
        limits.append(parent[1] - time.monotonic())
    return min(limits) if limits else None


def constrain_context(context: ExecutionContext) -> ExecutionContext:
    parent = _owned_deadline.get()
    if parent and (context.deadline is None or parent[0] < context.deadline):
        return replace(context, deadline=parent[0])
    return context


def require_time(context: ExecutionContext | None = None) -> float | None:
    from robothor.engine.runtime.classification_window import (
        require_time as require_classification_time,
    )

    require_classification_time()
    seconds = remaining(context)
    if seconds is not None and seconds <= 0:
        raise RuntimeDeadlineError(
            "Runtime deadline expired; new dispatch denied. Reconcile already dispatched effects."
        )
    return seconds


async def execute_before_deadline(
    context: ExecutionContext, execute: Callable[[], Awaitable[_Result]]
) -> _Result:
    seconds = require_time(context)
    parent = _owned_deadline.get()
    deadline = context.deadline
    if seconds is None or (parent is not None and (deadline is None or parent[0] <= deadline)):
        return await execute()
    if deadline is None:
        # Unreachable in practice: a non-None `seconds` with no enclosing parent
        # window can only have come from `context.deadline`. Handled rather than
        # asserted, because the safe answer is to run without owning a window.
        return await execute()
    token = _owned_deadline.set((deadline, time.monotonic() + seconds))
    try:
        return await _execute_window(seconds, execute)
    finally:
        _owned_deadline.reset(token)


async def _execute_window(seconds: float, execute: Callable[[], Awaitable[_Result]]) -> _Result:
    import asyncio

    window = asyncio.timeout(seconds)
    token = _active_window.set(window)
    try:
        try:
            async with window:
                result = await execute()
        except TimeoutError as exc:
            if not window.expired():
                raise  # Preserve the cause of an unrelated provider/workflow timeout.
            raise RuntimeDeadlineError(
                "Runtime deadline expired; execution cancelled. Reconcile already dispatched effects."
            ) from exc
        if window.expired():
            # A cancellation-resistant callee cannot turn expiry into success.
            raise RuntimeDeadlineError(
                "Runtime deadline expired; cancellation was suppressed. Outcome requires reconciliation."
            )
        return result
    finally:
        _active_window.reset(token)


def enclosing_deadline_reason(exc: BaseException) -> str:
    """Preserve a host/workflow deadline instead of claiming the manifest cap fired."""
    from robothor.engine.workflow_budget import WorkflowDeadlineError

    if isinstance(exc, WorkflowDeadlineError | RuntimeDeadlineError):
        return str(exc)
    from robothor.engine.runtime.classification_window import REASON, expired

    if expired():
        return REASON
    if isinstance(exc, TimeoutError):
        seconds = remaining()
        if seconds is not None and seconds <= 0:
            return "Runtime deadline expired; execution cancelled. Reconcile already dispatched effects."
    import asyncio

    window = _active_window.get()
    if isinstance(exc, asyncio.CancelledError) and window is not None and window.expired():
        return (
            "Runtime deadline expired; execution cancelled. Reconcile already dispatched effects."
        )
    return ""


def owns_deadline(context: ExecutionContext) -> bool:
    """The adapter already bounds this deadline, so native setup need not re-arm it."""
    parent = _owned_deadline.get()
    return bool(parent and context and context.deadline and parent[0] <= context.deadline)
