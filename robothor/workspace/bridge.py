"""Calling an async workspace provider from the worker thread a gws tool runs in.

The ``gws_*`` handlers are synchronous and run under ``asyncio.to_thread`` --
the gws CLI is a subprocess, and the calendar edit's cancellation check sits
in that thread right before the write. The provider protocols are async.
:func:`blocking` joins the two without ``asyncio.run`` (which would build a
second event loop per call, and an httpx client bound to one loop cannot be
used from another):

* a provider with a synchronous twin (``.blocking``, the Google adapter) is
  called directly -- the same thread, the same calls as before the seam;
* any other provider's coroutine is submitted to the ENGINE's loop, which the
  tool handler binds with :func:`bind_engine_loop` before it hands off to the
  thread (``asyncio.to_thread`` copies context variables into the thread).

A :class:`~robothor.workspace.errors.WorkspaceError` from an async provider
comes back as the ``{"error", "hint"}`` dict every handler already checks.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from robothor.workspace.errors import (
    AuthError,
    NotFound,
    PermissionDenied,
    PreconditionFailed,
    RateLimited,
    UnknownEffect,
    Unsupported,
    WorkspaceError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["bind_engine_loop", "blocking", "error_result"]

_engine_loop: ContextVar[asyncio.AbstractEventLoop | None] = ContextVar(
    "workspace_engine_loop", default=None
)

#: The hint each error class carries, matching the gws CLI's hint vocabulary.
_HINTS: tuple[tuple[type[WorkspaceError], str], ...] = (
    (UnknownEffect, "outcome_unknown"),
    (NotFound, "not_found"),
    (PreconditionFailed, "precondition_failed"),
    (RateLimited, "rate_limited"),
    (AuthError, "auth"),
    (PermissionDenied, "permission_denied"),
    (Unsupported, "unsupported"),
)


def error_result(exc: WorkspaceError) -> dict[str, Any]:
    """A provider error as the dict a gws handler returns. No body, no token."""
    hint = next((h for cls, h in _HINTS if isinstance(exc, cls)), "provider_error")
    out: dict[str, Any] = {"error": str(exc), "hint": hint}
    if exc.status is not None:
        out["status_code"] = exc.status
    if isinstance(exc, UnknownEffect):
        out["outcome_unknown"] = True
    return out


@contextmanager
def bind_engine_loop(loop: asyncio.AbstractEventLoop) -> Iterator[None]:
    """Make ``loop`` the one :func:`blocking` submits coroutines to."""
    token = _engine_loop.set(loop)
    try:
        yield
    finally:
        _engine_loop.reset(token)


def _run_on_engine_loop(fn: Any, *args: Any, **kwargs: Any) -> Any:
    loop = _engine_loop.get()
    if loop is None or loop.is_closed():
        raise Unsupported("this workspace provider needs the engine event loop; none is bound")
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        # Blocking the loop on a coroutine that needs the loop is a deadlock.
        raise Unsupported("a blocking workspace call was made on the engine event loop itself")
    try:
        return asyncio.run_coroutine_threadsafe(fn(*args, **kwargs), loop).result()
    except WorkspaceError as exc:
        return error_result(exc)


class _LoopBridge:
    """Synchronous view of an async-only provider."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._provider, name)
        if not inspect.iscoroutinefunction(attr):
            return attr
        return functools.partial(_run_on_engine_loop, attr)

    @contextmanager
    def session(self) -> Iterator[Any]:
        """Per-request calls through the loop; the provider pools connections."""
        yield self


def blocking(provider: Any) -> Any:
    """The synchronous face of ``provider`` for a worker thread."""
    twin = getattr(provider, "blocking", None)
    return twin if twin is not None else _LoopBridge(provider)
