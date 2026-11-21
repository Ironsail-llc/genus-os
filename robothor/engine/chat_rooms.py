"""Chat on a room's session: which session a request reaches, and the room rules.

A room (a "Workspace" in the UI) is a company-owned conversation several people
share. Its conversation is an ordinary chat session keyed
``agent:{agent_id}:room:{room_id}``, so the chat routes serve it; this module is
what they ask first.

- :func:`resolve` turns a request's ``room_id`` (or a raw room-shaped key) into
  the room's own stored key after checking access with ``robothor.rooms.access``,
  in every per-user-sessions mode and before the owner and mode rules in
  ``chat._effective_session_key``. Only a service token skips the membership
  check, and only for a room of its own tenant. No access is 404, access
  without the action 403, a request naming a room and a session 400.
- Attribution: a user turn in a room records ``author_user_id`` and
  ``author_display_name``; the model reads it as a ``[Name]`` prefix. An
  ordinary session gets none of this.
- One turn per room (``fanout.room_lock``): a second sender gets 409
  ``room_busy`` naming who is being answered. The lock is released when the
  turn's task ends, however it ends.
- The room's history is reloaded from the store at the start of each turn under
  the ROOM's tenant, so the agent sees turns written by other paths (a published
  edition) and other replicas, bounded to ``MAX_HISTORY``.
- After a saved exchange the room's activity mark moves and a ``room.message``
  event carrying ids only is published.
- A browser never sees a room's session key: replies echo ``roomId`` instead.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse

from robothor.engine import chat_store
from robothor.engine.chat_history import MAX_HISTORY, as_history
from robothor.engine.sanitize import sanitize_log
from robothor.rooms import access, dal, fanout
from robothor.rooms.errors import RoomForbidden, RoomNotFound

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from robothor.rooms.access import Action
    from robothor.rooms.models import Room

logger = logging.getLogger(__name__)

#: Room id -> display name of the person whose turn holds the room's lock.
_answering: dict[str, str] = {}


@dataclass(frozen=True)
class ChatTarget:
    """The session a chat request operates on, and the room behind it if any."""

    key: str
    room: Room | None = None

    @property
    def label(self) -> str:
        """What a reply may show: the session key, or for a room ``room:{id}``."""
        return self.key if self.room is None else f"room:{self.room.id}"

    def echo(self, body: dict[str, Any]) -> dict[str, Any]:
        """``body`` with ``roomId`` added when this is a room. Ordinary bodies unchanged."""
        return body if self.room is None else {**body, "roomId": self.room.id}

    def speaker(self, name: str, message: str) -> str:
        """The current turn as the model reads it: ``[Name] text`` in a room."""
        return f"[{name}] {message}" if self.room is not None and name else message

    def author(self, auth: Any, name: str, extras: dict[str, Any] | None) -> dict[str, Any] | None:
        """The stored user turn's extras, with its author in a room."""
        if self.room is None:
            return extras
        return {**(extras or {}), "author_user_id": str(auth.user_id), "author_display_name": name}

    def for_model(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The history the model gets: each room user turn prefixed with its author."""
        if self.room is None:
            return list(history)
        return [_attributed(row) for row in history]


def _row_author(row: Mapping[str, Any]) -> tuple[str, str]:
    """``(user_id, name)`` of a stored row (top level) or a RAM row (``extras``)."""
    raw = row.get("extras")
    extras: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    user = row.get("author_user_id") or extras.get("author_user_id") or ""
    name = row.get("author_display_name") or extras.get("author_display_name") or ""
    return str(user), str(name)


def _attributed(row: dict[str, Any]) -> dict[str, Any]:
    user, name = _row_author(row)
    if row.get("role") != "user" or not user or not name:
        return row
    return {**row, "content": f"[{name}] {row.get('content', '')}"}


def display_name(identity: Any) -> str:
    return str(getattr(identity, "display_name", "") or "")


def _refusal(status: int, error: str) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status)


def _service_room(room: Room, action: Action) -> Room:
    """A service token skips the membership check, never the archived-room rule."""
    if room.archived and action not in access.ARCHIVED_ACTIONS:
        raise RoomForbidden(f"an archived room does not allow {action!r}")
    return room


def _by_id(auth: Any, room_id: str, action: Action) -> Room:
    if getattr(auth, "is_service", False):
        room = dal.get_room(str(auth.tenant_id), room_id)
        if room is None:
            raise RoomNotFound(room_id)
        return _service_room(room, action)
    return access.require(access.Actor.from_auth(auth), room_id, action)


def _by_key(auth: Any, key: str, action: Action) -> Room | None:
    parsed = access.parse_room_key(key)
    if parsed is None:
        raise RoomNotFound(key)
    agent_id, room_id = parsed
    if getattr(auth, "is_service", False):  # verbatim key; the room, when there is one
        room = dal.get_room(str(auth.tenant_id), room_id)
        if room is None or room.agent_id != agent_id:
            return None
        return _service_room(room, action)
    room = access.require(access.Actor.from_auth(auth), room_id, action)
    if room.agent_id != agent_id:
        raise RoomNotFound(room_id)
    return room


async def resolve(
    auth: Any, source: Mapping[str, Any], action: Action = "read"
) -> ChatTarget | JSONResponse:
    """The target of a request whose body or query string is ``source``."""
    room_id = str(source.get("room_id") or "")
    key = str(source.get("session_key") or "")
    if room_id and (key or source.get("agent_id")):
        return _refusal(400, "name a room or a session, not both")
    try:
        if room_id:
            room = await asyncio.to_thread(_by_id, auth, room_id, action)
            return ChatTarget(room.session_key, room)
        if access.parse_room_key(key) is not None:
            found = await asyncio.to_thread(_by_key, auth, key, action)
            return ChatTarget(found.session_key, found) if found else ChatTarget(key)
    except RoomNotFound:
        return _refusal(404, "room_not_found")
    except RoomForbidden:
        return _refusal(403, "room_forbidden")
    from robothor.engine import chat

    return ChatTarget(chat._effective_session_key(auth, key))


def busy(target: ChatTarget) -> JSONResponse | None:
    """409 ``room_busy`` naming who is being answered, when a turn holds the room."""
    room = target.room
    if room is None or not fanout.room_lock(room.id).locked():
        return None
    body = {"error": "room_busy", "answering": _answering.get(room.id, "")}
    return JSONResponse(body, status_code=409)


async def start_turn(
    target: ChatTarget, name: str, launch: Callable[[], asyncio.Task[Any]]
) -> asyncio.Task[Any] | JSONResponse:
    """Start the turn ``launch`` starts, holding the room's lock until it ends.

    A busy room is 409 ``room_busy`` with the name of who is being answered.
    """
    room = target.room
    if room is None:
        return launch()
    if refusal := busy(target):
        return refusal
    lock = fanout.room_lock(room.id)
    await lock.acquire()  # free, so this does not yield: no sender slips in between
    _answering[room.id] = name

    def release(_task: object = None) -> None:
        if _answering.get(room.id) == name:
            _answering.pop(room.id, None)
        lock.release()

    try:
        task = launch()
    except BaseException:
        release()
        raise
    task.add_done_callback(release)
    return task


async def refresh(target: ChatTarget, session: Any) -> None:
    """Reload a room's history from the store, under the room's tenant."""
    room = target.room
    if room is None:
        return
    try:
        data = await asyncio.to_thread(
            chat_store.load_session, target.key, limit=MAX_HISTORY, tenant_id=room.tenant_id
        )
    except Exception as exc:  # noqa: BLE001 - the store being down costs freshness, not the turn
        logger.warning("room history reload failed: %s", sanitize_log(str(exc)))
        return
    if data and data.get("history"):
        session.history = as_history(data["history"])


def _publish(room: Room, message_id: int) -> None:
    from robothor.events import bus

    payload = {"room_id": room.id, "tenant_id": room.tenant_id, "message_id": message_id}
    bus.publish("agent", "room.message", payload, source="engine.chat", tenant_id=room.tenant_id)


async def saved(target: ChatTarget, saving: Awaitable[list[int] | None]) -> None:
    """Await a room exchange's save, then move the room's mark and announce it.

    Not cancellable: ``done`` may already have gone out (early release), and a
    browser leaving then cancels the turn. The save, the bump and the event
    still finish, and the turn (which holds the room's lock) waits for them, so
    the next sender's history reload sees the exchange. A cancel that arrives
    meanwhile is absorbed: the turn it would stop has already been answered.
    """
    job = asyncio.ensure_future(_record(target, saving))
    while True:
        try:
            await asyncio.shield(job)
            return
        except asyncio.CancelledError:
            if job.done():
                raise
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()


async def _record(target: ChatTarget, saving: Awaitable[list[int] | None]) -> None:
    ids = await saving
    room = target.room
    if room is None or not ids:
        return
    try:
        await asyncio.to_thread(dal.record_activity, room.tenant_id, room.id, ids[-1])
        await asyncio.to_thread(_publish, room, ids[-1])
    except Exception as exc:  # noqa: BLE001 - the exchange is saved; the mark can lag
        logger.warning("room activity not recorded: %s", sanitize_log(str(exc)))


def history_body(target: ChatTarget, body: dict[str, Any]) -> dict[str, Any]:
    """A ``/chat/history`` body for a room: authors in camelCase, no session key."""
    if target.room is None:
        return body
    messages = [_history_item(m) for m in body.get("messages", [])]
    rest = {k: v for k, v in body.items() if k != "sessionKey"}
    return target.echo({**rest, "messages": messages})


def _history_item(message: dict[str, Any]) -> dict[str, Any]:
    user, name = _row_author(message)
    item = dict(message)
    if user:
        item["authorUserId"] = user
        item["authorName"] = name
    if message.get("edition_id"):
        item["editionId"] = str(message["edition_id"])
    return item
