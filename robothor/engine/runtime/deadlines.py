"""Enforce a trusted runtime deadline without owning retries or finalization."""

import time
from asyncio import Timeout
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime

_active_window: ContextVar[Timeout | None] = ContextVar("runtime_deadline_window", default=None)


_owned_deadline: ContextVar[tuple[datetime, float] | None] = ContextVar(
    "runtime_deadline_owner", default=None
)


class RuntimeDeadlineError(TimeoutError):
    """The host deadline expired; already dispatched effects need reconciliation."""


def remaining(context=None):
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


def constrain_context(context):
    parent = _owned_deadline.get()
    if parent and (context.deadline is None or parent[0] < context.deadline):
        return replace(context, deadline=parent[0])
    return context


def require_time(context=None):
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


async def execute_before_deadline(context, execute):
    seconds = require_time(context)
    parent = _owned_deadline.get()
    if seconds is None or (
        parent is not None and (context.deadline is None or parent[0] <= context.deadline)
    ):
        return await execute()
    token = _owned_deadline.set((context.deadline, time.monotonic() + seconds))
    try:
        return await _execute_window(seconds, execute)
    finally:
        _owned_deadline.reset(token)


async def _execute_window(seconds, execute):
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


def owns_deadline(context) -> bool:
    """The adapter already bounds this deadline, so native setup need not re-arm it."""
    parent = _owned_deadline.get()
    return bool(parent and context and context.deadline and parent[0] <= context.deadline)
