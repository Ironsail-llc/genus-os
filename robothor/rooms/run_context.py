"""Which room the current agent run belongs to, and whether it is a preview.

The one carrier room tools read to learn that "this run's session is that
room". Whoever starts a run in a room binds it around ``runner.execute``:

    with room_run_scope(RoomRun.for_session(tenant_id, session_key)):
        await runner.execute(...)

- an interactive turn (chat on a room session): ``RoomRun.for_session`` with
  the room's session key; it is None for any other key, which binds nothing;
- a scheduled or on-demand run: the same, plus ``preview``, ``requested_by``
  (the preview's requester), the ``edition_id`` of the ``building`` row it
  created, and the period that row covers.

A ``ContextVar``, so it is task-local: tool handlers inside the run see it,
and a concurrent run in another task never does. Nothing here is ever taken
from tool arguments; an agent cannot claim a room, a preview or a period.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from robothor.rooms.keys import parse_room_key

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import date

__all__ = ["RoomRun", "current_room_run", "room_run_scope"]


@dataclass(frozen=True)
class RoomRun:
    """The room a run is in. Identifiers and flags only."""

    tenant_id: str
    room_id: str
    session_key: str
    #: A preview builds an edition for ``requested_by`` only: no event, no
    #: hooks, no message stamp.
    preview: bool = False
    requested_by: str | None = None
    #: The ``building`` edition the run was started to complete, if any.
    edition_id: str | None = None
    #: The default period for an edition published in this run.
    grain: str | None = None
    period_start: date | None = None
    period_end: date | None = None

    def __post_init__(self) -> None:
        parts = (self.grain, self.period_start, self.period_end)
        if any(p is not None for p in parts) and any(p is None for p in parts):
            raise ValueError("a run's period needs a grain, a start and an end")

    @property
    def period(self) -> tuple[str, date, date] | None:
        if self.grain is None or self.period_start is None or self.period_end is None:
            return None
        return (self.grain, self.period_start, self.period_end)

    @classmethod
    def for_session(cls, tenant_id: str, session_key: str, **kwargs: Any) -> RoomRun | None:
        """The carrier for a run on ``session_key``; None when it is not a room key."""
        parsed = parse_room_key(session_key)
        if parsed is None:
            return None
        return cls(tenant_id=tenant_id, room_id=parsed[1], session_key=session_key, **kwargs)


_CURRENT: ContextVar[RoomRun | None] = ContextVar("robothor_room_run", default=None)


def current_room_run() -> RoomRun | None:
    """The room the running agent turn is in, or None."""
    return _CURRENT.get()


@contextmanager
def room_run_scope(run: RoomRun | None) -> Iterator[RoomRun | None]:
    """Bind ``run`` for the block (None binds "no room") and restore after."""
    token = _CURRENT.set(run)
    try:
        yield run
    finally:
        _CURRENT.reset(token)
