"""Telling a room's people that something happened in it, and the per-room turn lock.

The lock
--------
One agent turn runs in a room at a time: a second sender waits on (or is told
about) the same lock. Locks are held weakly, so a room nobody is using costs
nothing; a held lock is kept alive by its holder, so every caller that arrives
while it is held gets the same one.

Fan-out
-------
:func:`fan_out_edition` runs after a non-preview edition is published by a
room run (``robothor.rooms.runs``). For every member with an active account
who has not muted the room (or every one named in ``recipients``):

- a note in their MAIN conversation and inbox, through the webchat channel:
  the edition's title, a summary cut to :data:`MAX_NOTE_CHARS`, and the
  markdown link ``[room title](/?v=chat&room=<id>)``. Never the edition's
  blocks or full text: people open the workspace for that, through the access
  check. The inbox row is type ``info``, addressed to the user id, and its
  ``metadata`` carries ``room_id`` and ``edition_id``, never the session key;
- their chosen external channel too (``notify.channel``, e.g. ``telegram``),
  when they have paired it; unpaired is a failed receipt, not an error;

then one ``room.edition`` event with ids only (unless the publish already sent
it: ``emit_event=False``). A recipient that fails gets a failed receipt and the
others are still told. A preview is never fanned out.

Who counts as muted: ``notify.muted`` true (or ``notify.enabled`` false) on
their membership; when their membership says nothing, the room's
``settings.notify_default`` false mutes everyone who has not opted in with
``notify.muted`` false. Tenant admins who are not members are not followers
and are not told (they can still open every room).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.rooms import dal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from robothor.engine.channels.base import SendReceipt
    from robothor.rooms.models import Edition, Room, RoomMember

__all__ = ["MAX_NOTE_CHARS", "fan_out_edition", "fan_out_note", "note_text", "room_lock"]

logger = logging.getLogger(__name__)

#: The longest summary a note carries. A note says "there is something new",
#: the workspace holds the content.
MAX_NOTE_CHARS = 280

_ADMIN_ROLES = frozenset({"owner", "admin"})

_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_guard = threading.Lock()


def room_lock(room_id: str) -> asyncio.Lock:
    """The lock one agent turn in ``room_id`` holds."""
    with _guard:
        lock = _locks.get(room_id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[room_id] = lock
        return lock


# ─── who ─────────────────────────────────────────────────────────────────


def _muted(room: Room, member: RoomMember) -> bool:
    notify: Mapping[str, Any] = member.notify or {}
    if "muted" in notify:
        return bool(notify["muted"])
    if notify.get("enabled") is False:
        return True
    return room.settings.get("notify_default") is False


def _active_roles(tenant_id: str, user_ids: Iterable[str]) -> dict[str, str]:
    """``user id -> role`` of the active accounts of the tenant among ``user_ids``."""
    ids = sorted(set(user_ids))
    if not ids:
        return {}
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id::text AS id, role FROM user_accounts "
            "WHERE tenant_id = %s AND status = 'active' AND id = ANY(%s::uuid[])",
            (tenant_id, ids),
        )
        return {r["id"]: str(r["role"]) for r in cur.fetchall()}


def _audience(room: Room, recipients: Iterable[str] | None) -> list[tuple[str, dict[str, Any]]]:
    """``(user_id, notify)`` of everyone to tell. Blocking."""
    from robothor.rooms.keys import canonical_uuid

    members = {m.user_id: m for m in dal.list_members(room.tenant_id, room.id)}
    if recipients is None:
        wanted = [uid for uid, m in members.items() if not _muted(room, m)]
    else:
        wanted = [c for c in (canonical_uuid(r) for r in recipients) if c]
    roles = _active_roles(room.tenant_id, wanted)
    out: list[tuple[str, dict[str, Any]]] = []
    for user_id in dict.fromkeys(wanted):
        role = roles.get(user_id)
        member = members.get(user_id)
        if role is None or (member is None and role not in _ADMIN_ROLES):
            continue  # no active account, or no access to this room
        out.append((user_id, dict(member.notify or {}) if member else {}))
    return out


# ─── what ────────────────────────────────────────────────────────────────


def _short(text: str, limit: int = MAX_NOTE_CHARS) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def note_text(room: Room, headline: str, summary: str = "") -> str:
    """A short note: headline, a cut summary, and the workspace link."""
    head = _short(headline, 200) or room.title
    body = _short(summary)
    line = f"{head}: {body}" if body else head
    title = _short(room.title, 120).replace("[", "(").replace("]", ")")
    return f"{line}\n\n[{title}]({room.link})"


def _main_agent() -> str:
    """The agent of everyone's main conversation."""
    from robothor.engine import chat

    config = getattr(chat, "_config", None)
    return str(getattr(config, "default_chat_agent", "") or "main")


# ─── send ────────────────────────────────────────────────────────────────


def _failed(target: str, status: str, expected: int = 2) -> SendReceipt:
    from robothor.engine.channels.base import SendReceipt

    return SendReceipt(acknowledged=0, expected=expected, status=status, target=target)


async def _external(room: Room, user_id: str, channel: str, text: str) -> SendReceipt:
    from robothor.engine.channels import identities, registry

    native = await asyncio.to_thread(
        identities.native_id_for_user, channel, user_id, tenant_id=room.tenant_id
    )
    if native is None:
        return _failed(user_id, "failed:room_channel_not_linked", 1)
    sender = registry.get_channel(channel)
    if sender is None:
        return _failed(user_id, f"failed:no_channel:{channel}", 1)
    receipt: SendReceipt = await sender.send(native, text)
    return receipt


async def fan_out_note(
    room: Room,
    text: str,
    *,
    metadata: Mapping[str, Any],
    recipients: Iterable[str] | None = None,
) -> list[SendReceipt]:
    """Send ``text`` to the room's audience (see the module docstring). Never raises."""
    from robothor.engine.channels.webchat import WebchatChannel

    audience = await asyncio.to_thread(_audience, room, recipients)
    config: Any = SimpleNamespace(id=_main_agent(), name="")
    meta = {k: v for k, v in metadata.items() if k != "session_key"}
    receipts: list[SendReceipt] = []
    webchat = WebchatChannel()
    for user_id, notify in audience:
        try:
            receipts.append(
                await webchat.send(
                    user_id, text, config=config, tenant_id=room.tenant_id, metadata=meta
                )
            )
        except Exception as exc:  # noqa: BLE001 - one recipient must not stop the rest
            logger.warning("workspace note not delivered: %s", type(exc).__name__)
            receipts.append(_failed(user_id, "failed:room_note_error"))
        channel = notify.get("channel")
        if isinstance(channel, str) and channel and channel != "webchat":
            try:
                receipts.append(await _external(room, user_id, channel, text))
            except Exception as exc:  # noqa: BLE001
                logger.warning("workspace note not delivered on %s: %s", channel, exc)
                receipts.append(_failed(user_id, "failed:room_note_error", 1))
    return receipts


async def fan_out_edition(
    room: Room,
    edition: Edition,
    *,
    recipients: Iterable[str] | None = None,
    emit_event: bool = True,
) -> list[SendReceipt]:
    """Tell the room's people a published edition is out. Returns the receipts.

    ``recipients`` limits it to those users (an on-demand period run tells
    its requester only); ``emit_event`` False when the publish already sent
    the ``room.edition`` event. A preview, or an edition that is not
    published, reaches nobody."""
    if edition.preview or edition.status != "published":
        return []
    text = note_text(room, edition.title or room.title, edition.summary)
    receipts = await fan_out_note(
        room,
        text,
        metadata={"room_id": room.id, "edition_id": edition.id},
        recipients=recipients,
    )
    if emit_event:
        from robothor.rooms import editions

        try:
            await asyncio.to_thread(editions.emit_edition_event, room, edition)
        except Exception:
            logger.exception("room.edition event not published")
    return receipts
