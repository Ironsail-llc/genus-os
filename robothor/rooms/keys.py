"""The room session key: ``agent:{agent_id}:room:{room_id}``.

Segment 1 stays the agent because chat routing parses it. Re-exported by
``robothor.rooms.access``, which is where callers import them from.
"""

from __future__ import annotations

import uuid
from typing import Any

_PREFIX, _ROOM = "agent", "room"


def canonical_uuid(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def room_session_key(agent_id: str, room_id: str) -> str:
    """The session key of a room's conversation. The only writer of the shape."""
    room = canonical_uuid(room_id)
    if not agent_id or ":" in agent_id or room is None:
        raise ValueError(f"cannot build a room session key from {agent_id!r}, {room_id!r}")
    return f"{_PREFIX}:{agent_id}:{_ROOM}:{room}"


def parse_room_key(key: Any) -> tuple[str, str] | None:
    """``(agent_id, room_id)`` of a room session key, else None. The only parser."""
    if not isinstance(key, str):
        return None
    parts = key.split(":")
    if len(parts) != 4 or parts[0] != _PREFIX or parts[2] != _ROOM or not parts[1]:
        return None
    room = canonical_uuid(parts[3])
    return (parts[1], room) if room else None
