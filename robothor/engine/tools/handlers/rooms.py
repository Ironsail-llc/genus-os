"""Workspace tools (``room_*``): the main agent creates, edits, shares and reads
workspaces for the person it is talking to.

Thin wrappers over ``robothor.rooms.definition``, where every rule lives; the
bridge's ``/api/rooms`` routes call the same functions. What this module owns
is the tool surface:

* **Who is asking.** Always the human on the run (``_actor``), never the agent.
  An unverified identity is refused outright. A run with no person behind it
  (cron, hook, workflow) may only ``room_read``, only the room the run-context
  carrier (``robothor.rooms.run_context``) binds it to, and only with the
  access that room's run-as user holds right now (``definition.run_as_actor``).
  No carrier, or no bound room, means it reads nothing.
* **What a refusal says.** Not found and no access read exactly the same, and
  name neither the room nor its id. A shared caller who tries to change a room
  is offered ``room_duplicate``.
* **The link.** Every result that names a room carries ``link``.

One function per tool; a later lane adds its tool the same way.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from robothor.rooms import definition
from robothor.rooms.access import Actor
from robothor.rooms.errors import RevisionConflict, RoomForbidden, RoomNotFound
from robothor.rooms.keys import canonical_uuid
from robothor.rooms.models import room_link

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext
    from robothor.rooms.models import Edition, Room, RoomSummary

HANDLERS: dict[str, Any] = {}

#: Tenant roles that belong to a person. ``service`` and anything else do not.
_HUMAN_ROLES = frozenset({"owner", "admin", "member", "user", "viewer"})
_MAX_MESSAGE_CHARS = 2000

_NOT_FOUND = {
    "error": (
        "No workspace with that id is visible to you. Use room_list to find the "
        "workspaces you can open; never guess an id or pass a title."
    ),
    "not_found": True,
}
_NO_PERSON = {
    "error": (
        "Workspace tools act for a person, and this run has no verified person behind "
        "it. Only room_read works here, for the one workspace this run belongs to, with "
        "the access of that workspace's run-as user."
    )
}
_UNVERIFIED = {
    "error": (
        "The person on this run is not verified, so workspace tools are refused. Ask them "
        "to sign in on a verified channel."
    )
}


def _unverified(ctx: ToolContext) -> bool:
    identity = getattr(ctx, "identity", None)
    return identity is not None and not getattr(identity, "verified", False)


def _bound_room_id(tenant_id: str) -> str | None:
    """The room this run belongs to, from the run-context carrier.

    None when the carrier module is absent (nothing binds a run to a room yet),
    when the run is bound to no room, or to a room of another tenant: a run
    with no person behind it then reads nothing. Never taken from arguments.
    """
    try:
        from robothor.rooms.run_context import (  # type: ignore[import-not-found,unused-ignore]
            current_room_run,
        )
    except ImportError:
        return None
    run = current_room_run()
    if run is None or getattr(run, "tenant_id", None) != tenant_id:
        return None
    return canonical_uuid(getattr(run, "room_id", None))


def _actor(ctx: ToolContext) -> Actor | None:
    """The human caller, or None when the run has no verified person behind it."""
    identity = getattr(ctx, "identity", None)
    if identity is not None and not getattr(identity, "verified", False):
        return None
    user_id = canonical_uuid(ctx.user_id)
    if user_id is None or ctx.user_role not in _HUMAN_ROLES:
        return None
    return Actor(user_id=user_id, role=ctx.user_role, tenant_id=ctx.tenant_id)


def _link(room_id: str) -> str:
    return room_link(room_id)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime | date) else value


def _room(room: Room) -> dict[str, Any]:
    """A room as the agent sees it. Never the session key."""
    return {
        "id": room.id,
        "kind": room.kind,
        "title": room.title,
        "agent_id": room.agent_id,
        "instructions": room.instructions,
        "settings": dict(room.settings),
        "rev": room.rev,
        "paused": room.paused,
        "archived": room.archived,
        "created_by": room.created_by,
        "run_as_user_id": room.run_as_user_id,
        "last_activity_at": _iso(room.last_activity_at),
        "link": _link(room.id),
    }


def _summary(summary: RoomSummary) -> dict[str, Any]:
    latest = summary.latest_edition
    return {
        "id": summary.id,
        "kind": summary.kind,
        "title": summary.title,
        "agent_id": summary.agent_id,
        "paused": summary.paused,
        "archived": summary.archived,
        "unread": summary.unread,
        "last_activity_at": _iso(summary.last_activity_at),
        "latest_edition": {k: _iso(v) for k, v in latest.items()} if latest else None,
        "link": _link(summary.id),
    }


def _edition(edition: Edition) -> dict[str, Any]:
    return {
        "id": edition.id,
        "seq": edition.seq,
        "title": edition.title,
        "summary": edition.summary,
        "period_start": _iso(edition.period_start),
        "period_end": _iso(edition.period_end),
        "grain": edition.grain,
        "preview": edition.preview,
        "created_at": _iso(edition.created_at),
    }


def _refusal(exc: Exception, *, editing: bool = False) -> dict[str, Any]:
    if isinstance(exc, RoomNotFound):
        return dict(_NOT_FOUND)
    out: dict[str, Any]
    if isinstance(exc, RevisionConflict):
        out = {
            "error": (
                f"The workspace changed since you read it (now revision {exc.current_rev}). "
                "Call room_read, show the person the current definition, and confirm again."
            ),
            "current_rev": exc.current_rev,
        }
    elif isinstance(exc, RoomForbidden) and editing:
        out = {
            "error": (
                "You are not assigned to this workspace, so you cannot change it. Offer "
                "room_duplicate: a copy of their own that they can change."
            )
        }
    else:
        out = {"error": str(exc)}
    return out


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


def _room_id(args: dict[str, Any]) -> str:
    return str(args.get("room_id") or "")


# ─── tools ───────────────────────────────────────────────────────────────


async def _room_create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    share_with = args.get("share_with") or []
    if not isinstance(share_with, list):
        return {"error": "share_with must be a list of user ids or emails"}
    try:
        room = await _call(
            definition.create_room_with_schedule,
            actor,
            kind=str(args.get("kind") or ""),
            title=str(args.get("title") or ""),
            instructions=str(args.get("instructions") or ""),
            agent_id=args.get("agent_id") or None,
            schedule_text=args.get("schedule") or None,
            tz=args.get("tz") or None,
            default_grain=args.get("default_grain") or None,
            share_with=[str(u) for u in share_with],
        )
        schedule = await _call(definition.room_schedule, room.tenant_id, room.id)
    except (RoomForbidden, RoomNotFound, ValueError) as exc:
        return _refusal(exc)
    note = (
        "Scheduled."
        if schedule
        else "Not scheduled. To schedule it, call room_update with changes.schedule_text."
    )
    return {"room": _room(room), "link": _link(room.id), "schedule": schedule, "note": note}


async def _room_update(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    room_id, changes, rev = _room_id(args), args.get("changes"), args.get("expected_rev")
    if not isinstance(changes, dict):
        return {"error": "changes must be an object of the fields to change"}
    if isinstance(rev, bool) or not isinstance(rev, int):
        return {"error": "expected_rev must be the revision number room_read returned"}
    try:
        room = await _call(definition.apply_definition_change, actor, room_id, rev, changes)
    except (RoomForbidden, RoomNotFound, RevisionConflict, ValueError) as exc:
        out = _refusal(exc, editing=True)
        canonical = canonical_uuid(room_id)
        if isinstance(exc, RoomForbidden | RevisionConflict) and canonical:
            out["link"] = _link(canonical)
        return out
    return {"room": _room(room), "link": _link(room.id)}


async def _room_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    scope = str(args.get("scope") or "mine")
    try:
        rooms = await _call(definition.list_rooms, actor, scope=scope)
    except (RoomForbidden, ValueError) as exc:
        return {"error": str(exc)}
    return {"scope": scope, "rooms": [_summary(r) for r in rooms]}


def _bounded(value: Any, default: int, upper: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("editions and messages must be whole numbers")
    return max(1, min(value, upper))


async def _room_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    room_id = _room_id(args)
    try:
        editions = _bounded(args.get("editions"), 3, 20)
        messages = _bounded(args.get("messages"), 10, 50)
        if _unverified(ctx):
            return dict(_UNVERIFIED)
        actor = _actor(ctx)
        if actor is None:
            # No person: only the room this run belongs to, as its run-as user.
            bound = _bound_room_id(ctx.tenant_id)
            if bound is None or canonical_uuid(room_id) != bound:
                return dict(_NOT_FOUND)
            actor = await _call(definition.run_as_actor, ctx.tenant_id, bound)
            if actor is None:
                return dict(_NOT_FOUND)
        view = await _call(
            definition.read_room, actor, room_id, editions=editions, messages=messages
        )
    except (RoomForbidden, RoomNotFound, ValueError) as exc:
        return _refusal(exc)
    for message in view["messages"]:
        message["content"] = str(message["content"] or "")[:_MAX_MESSAGE_CHARS]
        message["created_at"] = _iso(message["created_at"])
    return {
        "room": _room(view["room"]),
        "access": view["access"],
        "link": view["link"],
        "runs_as": view["runs_as"],
        "schedule": view["schedule"],
        "members": view["members"],
        "editions": [_edition(e) for e in view["editions"]],
        "messages": view["messages"],
        "tasks": [
            {"task_id": t.task_id, "origin": t.origin, "pending": t.pending} for t in view["tasks"]
        ],
        "revisions": [{**r, "changed_at": _iso(r["changed_at"])} for r in view["revisions"]],
    }


async def _room_share(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    room_id, assigned = _room_id(args), args.get("assigned")
    if assigned is not None and not isinstance(assigned, bool):
        return {"error": "assigned must be true or false, or left out to keep their role"}
    try:
        outcome = await _call(
            definition.share_room, actor, room_id, str(args.get("user") or ""), assigned=assigned
        )
    except (RoomForbidden, RoomNotFound, ValueError) as exc:
        return _refusal(exc)
    result: dict[str, Any] = {
        "member": {"user_id": outcome.user_id, "assigned": outcome.assigned},
        "already_member": not outcome.added,
        "link": _link(outcome.room_id),
    }
    if not outcome.added:
        result["note"] = "They were already a member; no new notice is sent."
    return result


async def _room_unshare(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    room_id = _room_id(args)
    try:
        removed = await _call(definition.unshare_room, actor, room_id, str(args.get("user") or ""))
    except (RoomForbidden, RoomNotFound, ValueError) as exc:
        return _refusal(exc)
    return {"removed": removed, "link": _link(str(canonical_uuid(room_id)))}


async def _room_archive(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    try:
        room = await _call(definition.archive_room, actor, _room_id(args))
    except (RoomForbidden, RoomNotFound, ValueError) as exc:
        return _refusal(exc)
    return {"room": _room(room), "link": _link(room.id)}


async def _room_duplicate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    try:
        room = await _call(definition.duplicate_room, actor, _room_id(args))
    except (RoomForbidden, RoomNotFound, ValueError) as exc:
        return _refusal(exc)
    note = (
        "The copy is paused: its schedule runs only after its owner resumes it "
        "(room_update with changes.paused=false)."
        if room.paused
        else "The copy is not scheduled."
    )
    return {"room": _room(room), "link": _link(room.id), "note": note}


async def _room_restore_revision(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    rev = args.get("rev")
    if isinstance(rev, bool) or not isinstance(rev, int):
        return {"error": "rev must be a revision number from room_read"}
    try:
        room = await _call(definition.restore_room_revision, actor, _room_id(args), rev)
    except (RoomForbidden, RoomNotFound, ValueError) as exc:
        return _refusal(exc)
    return {"room": _room(room), "link": _link(room.id)}


async def _room_admin_reassign(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    try:
        room = await _call(
            definition.reassign_room, actor, _room_id(args), str(args.get("user") or "")
        )
    except (RoomForbidden, RoomNotFound, ValueError) as exc:
        return _refusal(exc)
    return {"room": _room(room), "link": _link(room.id)}


HANDLERS["room_create"] = _room_create
HANDLERS["room_update"] = _room_update
HANDLERS["room_list"] = _room_list
HANDLERS["room_read"] = _room_read
HANDLERS["room_share"] = _room_share
HANDLERS["room_unshare"] = _room_unshare
HANDLERS["room_archive"] = _room_archive
HANDLERS["room_duplicate"] = _room_duplicate
HANDLERS["room_restore_revision"] = _room_restore_revision
HANDLERS["room_admin_reassign"] = _room_admin_reassign
