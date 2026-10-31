"""Channel streaming: the flag and the scope a streaming channel runs in (stub)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator


def channel_streaming_enabled() -> bool:
    return False


@contextlib.contextmanager
def channel_stream_scope() -> Iterator[None]:
    yield


def in_channel_stream_scope() -> bool:
    return False
