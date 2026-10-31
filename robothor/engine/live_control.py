"""Steer, stop and progress for a conversation's live run (interface only)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable, Iterator


def steer_everywhere_enabled() -> bool:
    raise NotImplementedError


@contextlib.contextmanager
def conversation_scope(key: str) -> Iterator[None]:
    raise NotImplementedError
    yield  # pragma: no cover


def find_live_run(key: str, *, tenant_id: str) -> str | None:
    raise NotImplementedError


def steer_conversation(key: str, text: str, *, tenant_id: str) -> bool:
    raise NotImplementedError


def interrupt_conversation(key: str, message: str | None = None, *, tenant_id: str) -> bool:
    raise NotImplementedError


def format_progress_line(event: dict[str, Any], *, elapsed_s: float) -> str | None:
    raise NotImplementedError


class ProgressMessage:
    def __init__(
        self,
        post: Callable[[str], Awaitable[Any]],
        edit: Callable[[Any, str], Awaitable[None]],
        *,
        min_interval: float = 1.0,
    ) -> None:
        raise NotImplementedError

    async def on_event(self, event: dict[str, Any]) -> None:
        raise NotImplementedError

    async def close(self, final: str | None = None) -> None:
        raise NotImplementedError
