"""Bound unclassified interactive work; identified long work keeps its own limits."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from robothor.engine.runtime.deadlines import RuntimeDeadlineError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from robothor.engine.runtime.contracts import RunRequest

_window: ContextVar[asyncio.Timeout | None] = ContextVar(
    "interactive_classification_window", default=None
)
_expires: ContextVar[datetime | None] = ContextVar(
    "interactive_classification_expiry", default=None
)
_release_allowed: ContextVar[bool] = ContextVar("interactive_classification_owner", default=False)
REASON = "Runtime deadline expired before request complexity was established. Reconcile already dispatched effects."


class ClassificationDeadlineError(RuntimeDeadlineError):
    def __init__(self, deadline: datetime | None) -> None:
        super().__init__(REASON)
        self.deadline = deadline


def expired() -> bool:
    window = _window.get()
    if window is None:
        return False
    # Bound once: `when()` was called twice, so neither call narrowed the other
    # and the comparison could in principle run against a None.
    when = window.when()
    return bool(
        window.expired() or (when is not None and asyncio.get_running_loop().time() >= when)
    )


def require_time() -> None:
    if expired():
        raise ClassificationDeadlineError(_expires.get())


def owned_deadline() -> datetime | None:
    """Transfer only this admission's still-active classification bound."""
    require_time()
    window = _window.get()
    if _release_allowed.get() and window is not None and window.when() is not None:
        return _expires.get()
    return None


def release() -> None:
    require_time()  # A late classification cannot revive an expired request.
    window = _window.get()
    if window is not None and _release_allowed.get():
        window.reschedule(None)


@asynccontextmanager
async def guard(request: RunRequest, admitted_at: datetime | None = None) -> AsyncIterator[None]:
    from robothor.engine.runtime.action_policy import SIMPLE_ACTION_SECONDS
    from robothor.engine.runtime.classified_deadline import eligible

    config = request.options.get("agent_config")
    deadline = (admitted_at or datetime.now(UTC)) + timedelta(seconds=SIMPLE_ACTION_SECONDS)
    enabled = eligible(request, config) and getattr(config, "difficulty_class", "") not in {
        "moderate",
        "complex",
    }
    if request.context.deadline is not None and request.context.deadline <= deadline:
        enabled = False  # The existing runtime owner already enforces this bound.
    window = (
        asyncio.timeout(max(0, (deadline - datetime.now(UTC)).total_seconds())) if enabled else None
    )
    token = _window.set(window if enabled else _window.get())
    expiry_token = _expires.set(deadline if enabled else _expires.get())
    owner_token = _release_allowed.set(enabled)
    try:
        if window is None:
            yield
        else:
            try:
                async with window:
                    require_time()
                    yield
                    require_time()
            except TimeoutError as exc:
                if window.expired():
                    raise ClassificationDeadlineError(deadline) from exc
                raise
    finally:
        _window.reset(token)
        _expires.reset(expiry_token)
        _release_allowed.reset(owner_token)
