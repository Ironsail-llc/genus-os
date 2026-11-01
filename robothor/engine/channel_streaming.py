"""Channel streaming: the flag, and the scope a streaming channel runs in.

``ROBOTHOR_CHANNEL_STREAMING`` turns streaming on for Telegram and for plugin
channels that hand the inbound pipeline a ``stream`` callback. Two LLM-client
behaviours only make sense for such a channel, so they key off the scope as
well as the flag:

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
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

__all__ = ["channel_stream_scope", "channel_streaming_enabled", "in_channel_stream_scope"]

_in_scope: ContextVar[bool] = ContextVar("channel_stream_scope", default=False)


def channel_streaming_enabled() -> bool:
    """``ROBOTHOR_CHANNEL_STREAMING``, read through settings. False on any error."""
    try:
        from robothor.settings import get_settings

        return bool(get_settings().channels.channel_streaming)
    except Exception:  # noqa: BLE001 - a settings error must not break a reply
        logger.debug("channel_streaming setting unreadable; treating as off", exc_info=True)
        return False


@contextlib.contextmanager
def channel_stream_scope() -> Iterator[None]:
    """Mark the run started inside this block as streaming to a channel."""
    token = _in_scope.set(True)
    try:
        yield
    finally:
        _in_scope.reset(token)


def in_channel_stream_scope() -> bool:
    """True inside :func:`channel_stream_scope` with the flag on."""
    return _in_scope.get() and channel_streaming_enabled()
