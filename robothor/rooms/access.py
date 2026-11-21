"""Who may do what in a room.

Access comes from two things only: the caller's tenant role and their
membership row. There is no per-room role.

- tenant ``owner`` / ``admin``: ``admin`` on every room of the tenant.
- tenant ``member`` / ``user``: ``assigned`` or ``shared`` per their membership.
- tenant ``viewer``: a membership of either kind is capped at ``viewer``.
- a service identity, any other role, or another tenant: ``none``. A room's
  access always flows from a human; system work (a scheduled run's fan-out)
  calls the data layer directly under the run-as user it resolved.

An archived room keeps only ``read``, ``archive`` (to unarchive), ``duplicate``
and ``list_all``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from robothor.rooms import dal
from robothor.rooms.errors import RoomForbidden, RoomNotFound
from robothor.rooms.keys import canonical_uuid, parse_room_key, room_session_key

if TYPE_CHECKING:
    from robothor.rooms.models import Room, RoomMember

__all__ = [
    "ACTIONS",
    "ARCHIVED_ACTIONS",
    "GRANTS",
    "Access",
    "Action",
    "Actor",
    "can",
    "parse_room_key",
    "require",
    "resolve_room_key",
    "room_access",
    "room_session_key",
]

Access = Literal["none", "viewer", "shared", "assigned", "admin"]
Action = Literal[
    "read",
    "post",
    "run",
    "request_period",
    "accept_task",
    "edit_definition",
    "share",
    "archive",
    "restore_revision",
    "duplicate",
    "reassign",
    "list_all",
]

_VIEWER: frozenset[str] = frozenset({"read"})
_SHARED = _VIEWER | {"post", "run", "request_period", "accept_task", "duplicate"}
_ASSIGNED = _SHARED | {"edit_definition", "share", "archive", "restore_revision"}
_ADMIN = _ASSIGNED | {"reassign", "list_all"}

#: access level -> the actions it allows.
GRANTS: dict[str, frozenset[str]] = {
    "none": frozenset(),
    "viewer": _VIEWER,
    "shared": _SHARED,
    "assigned": _ASSIGNED,
    "admin": _ADMIN,
}
ACTIONS: frozenset[str] = _ADMIN
#: What an archived room still allows, to whoever holds the action anyway.
ARCHIVED_ACTIONS: frozenset[str] = frozenset({"read", "archive", "duplicate", "list_all"})

_ADMIN_ROLES = frozenset({"owner", "admin"})
_MEMBER_ROLES = frozenset({"member", "user"})


@dataclass(frozen=True)
class Actor:
    """The caller, in the tenant it is acting in."""

    user_id: str
    role: str
    tenant_id: str
    is_service: bool = False

    @classmethod
    def from_auth(cls, auth: Any) -> Actor:
        """From an ``AuthContext`` (or anything shaped like one)."""
        return cls(
            user_id=str(auth.user_id),
            role=str(getattr(auth, "role", "") or ""),
            tenant_id=str(auth.tenant_id),
            is_service=bool(getattr(auth, "is_service", False)),
        )


def room_access(actor: Actor, room: Room, member_row: RoomMember | None) -> Access:
    """The caller's access level to ``room``. Pure."""
    if actor.is_service or actor.tenant_id != room.tenant_id:
        return "none"
    if actor.role in _ADMIN_ROLES:
        return "admin"
    member = (
        member_row
        if member_row is not None
        and member_row.room_id == room.id
        and member_row.user_id == actor.user_id
        and member_row.tenant_id == room.tenant_id
        else None
    )
    if member is None:
        return "none"
    if actor.role == "viewer":
        return "viewer"
    if actor.role in _MEMBER_ROLES:
        return "assigned" if member.assigned else "shared"
    return "none"


def can(access: Access, action: Action, *, archived: bool = False) -> bool:
    """Whether ``access`` allows ``action`` (in an archived room, when ``archived``)."""
    if action not in ACTIONS:
        raise ValueError(f"unknown room action {action!r}")
    if archived and action not in ARCHIVED_ACTIONS:
        return False
    return action in GRANTS.get(access, frozenset())


def require(actor: Actor, room_id: str, action: Action) -> Room:
    """The room, when ``actor`` may take ``action`` in it.

    Raises ``RoomNotFound`` when the room is not in the actor's tenant or the
    actor has no access at all (indistinguishable on purpose), and
    ``RoomForbidden`` when they have access but not this action.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown room action {action!r}")
    canonical = canonical_uuid(room_id)
    if canonical is None:
        raise RoomNotFound(room_id)
    room = dal.get_room(actor.tenant_id, canonical)
    if room is None:
        raise RoomNotFound(canonical)
    member = None
    if not actor.is_service and actor.role not in _ADMIN_ROLES:
        member = dal.get_member(actor.tenant_id, canonical, actor.user_id)
    level = room_access(actor, room, member)
    if level == "none":
        raise RoomNotFound(canonical)
    if not can(level, action, archived=room.archived):
        raise RoomForbidden(f"{level} access does not allow {action!r} in this room")
    return room


def resolve_room_key(auth: Any, session_key: str, *, action: Action = "read") -> str | None:
    """The session key a chat request on ``session_key`` should use.

    None when ``session_key`` is not a room key (the caller applies its usual
    rules). For a room key: the room's own stored key once ``auth`` may take
    ``action`` there, so a key naming the room under a different agent never
    routes anywhere. Raises ``RoomNotFound`` / ``RoomForbidden`` as ``require``.
    """
    parsed = parse_room_key(session_key)
    if parsed is None:
        return None
    agent_id, room_id = parsed
    room = require(Actor.from_auth(auth), room_id, action)
    if room.agent_id != agent_id:
        raise RoomNotFound(room_id)
    return room.session_key
