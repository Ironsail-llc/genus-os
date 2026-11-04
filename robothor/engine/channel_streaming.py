"""Channel streaming: the scope a streaming channel runs in.

Telegram and plugin channels that hand the inbound pipeline a ``stream``
callback run inside this scope. Two LLM-client behaviours only make sense for
such a channel, so they key off it:

* a transient stream failure is retried once on the same model, discarding
  the partial text, and
* text the model writes in a tool turn keeps reaching ``on_content``.

Both rely on the consumer treating ``on_content`` as the cumulative text of
the current call, rendered by REPLACING what it showed. Telegram's progress
message and a plugin's ``stream`` callback do; the dashboard chat appends
deltas and would show a retried prefix twice, so it never enters the scope.
The scope is a ContextVar, so it follows the run through every ``await`` and
into tasks it starts, and ends with the ``with`` block.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

__all__ = ["channel_stream_scope", "in_channel_stream_scope"]

_in_scope: ContextVar[bool] = ContextVar("channel_stream_scope", default=False)


@contextlib.contextmanager
def channel_stream_scope() -> Iterator[None]:
    """Mark the run started inside this block as streaming to a channel."""
    token = _in_scope.set(True)
    try:
        yield
    finally:
        _in_scope.reset(token)


def in_channel_stream_scope() -> bool:
    """True inside :func:`channel_stream_scope`."""
    return _in_scope.get()
