"""Placeholder for the room definition logic (validation, revisions, schedule).

The real module is built in parallel and replaces this file at integration;
until then it names exactly the contract's signatures so the bridge routes
can import them. Nothing here is reachable in a release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from robothor.rooms.access import Actor
    from robothor.rooms.models import Room

#: The shortest interval a room's schedule may have.
ROOM_MIN_INTERVAL_SECONDS = 900


class UnsafeInstructions(ValueError):  # noqa: N818 - names fixed by the contract
    """The instructions failed the injection scan."""


class InvalidSchedule(ValueError):  # noqa: N818 - names fixed by the contract
    """The schedule does not parse, or fires more often than the minimum."""


def create_room_with_schedule(
    actor: Actor,
    *,
    kind: str,
    title: str,
    instructions: str,
    agent_id: str | None = None,
    schedule_text: str | None = None,
    tz: str | None = None,
    default_grain: str | None = None,
    share_with: Iterable[str] = (),
) -> Room:
    raise NotImplementedError("robothor.rooms.definition is provided by another lane")


def apply_definition_change(
    actor: Actor, room_id: str, expected_rev: int, changes: Mapping[str, Any]
) -> Room:
    raise NotImplementedError("robothor.rooms.definition is provided by another lane")
