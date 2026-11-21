"""Workspaces ("rooms") bridge API.

Code says room; the dashboard says Workspaces. The gate is per room, not per
tenant: every handler opens with ``_guard(request, room_id, action)`` (or
``_human(request)`` where no room is named yet), which builds the caller from
the verified token and asks ``robothor.rooms.access``. No access is 404 (a room
id cannot be probed), access without the action is 403, a stale revision is
409 with the current one. Admin-only: ``?scope=all``, reassign and unarchive.

Definition changes go through ``robothor.rooms.definition``, which owns
validation (injection scan, schedule, minimum interval) and the revision
write; this router maps its errors and never validates on its own.

Two routes are engine-backed (``/run``, ``/periods``): the bridge checks access
and proxies to the engine with the requester's id, since the engine sees the
bridge's service identity. Every mutation is audited through ``_audit``
(identifiers only) and announced as a ``room.updated`` event. The session key
never leaves this module.

JSON is camelCase at the top level. Stored JSON documents (``settings``,
``blocks``, ``suggestions``, ``provenance``, a revision's ``definition``) are
passed through as stored.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from robothor.crm.dal import send_notification
from robothor.events.bus import publish
from robothor.rooms import access, dal, definition
from robothor.rooms.errors import (
    InvalidBlocks,
    RevisionConflict,
    RoomBusy,
    RoomForbidden,
    RoomNotFound,
)
from robothor.rooms.keys import canonical_uuid
from robothor.rooms.models import room_link
from routers._audit import audited
from routers._engine_client import engine_request

if TYPE_CHECKING:
    from datetime import date, datetime

    from robothor.rooms.models import Edition, Revision, Room, RoomMember, RoomSummary, RoomTask

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rooms", tags=["rooms"])

#: Tenant roles that may hold a room at all, and the ones that may do work in
#: one (create, open a task, be reassigned to). A viewer reads only.
_HUMAN_ROLES = frozenset({"owner", "admin", "member", "user", "viewer"})
_WORKING_ROLES = _HUMAN_ROLES - {"viewer"}
_ADMIN_ROLES = frozenset({"owner", "admin"})
_CREATABLE_KINDS = frozenset({"report", "agent"})

#: camelCase spellings of the definition change keys, to the contract's names.
_CHANGE_KEYS = {
    "scheduleText": "schedule_text",
    "defaultGrain": "default_grain",
    "notifyDefault": "notify_default",
    "maxCostUsd": "max_cost_usd",
}


# ─── errors ──────────────────────────────────────────────────────────────


def room_error_response(_request: Any, exc: Exception) -> JSONResponse:
    """The one mapping from a rooms error to an HTTP answer (installed on the app)."""
    if isinstance(exc, RoomNotFound):
        return JSONResponse({"error": "workspace not found"}, status_code=404)
    if isinstance(exc, RoomForbidden):
        return JSONResponse({"error": str(exc)}, status_code=403)
    if isinstance(exc, RevisionConflict):
        return JSONResponse(
            {"error": "the workspace changed since you opened it", "currentRev": exc.current_rev},
            status_code=409,
        )
    if isinstance(exc, RoomBusy):
        return JSONResponse({"error": "room_busy", "answering": exc.answering}, status_code=409)
    if isinstance(exc, InvalidBlocks):
        return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse({"error": "workspace error"}, status_code=500)


def _invalid(exc: ValueError) -> JSONResponse:
    return JSONResponse({"error": str(exc)}, status_code=422)


# ─── gates ───────────────────────────────────────────────────────────────


def _actor(request: Request) -> access.Actor:
    auth = getattr(request.state, "auth", None)
    if auth is None:
        raise HTTPException(status_code=401, detail="sign in required")
    return access.Actor.from_auth(auth)


def _human(request: Request, roles: frozenset[str] = _HUMAN_ROLES) -> access.Actor:
    """The signed-in person, when their tenant role is one of ``roles``."""
    actor = _actor(request)
    if actor.is_service or actor.role not in _HUMAN_ROLES:
        raise RoomForbidden("workspaces need a signed-in person")
    if actor.role not in roles:
        raise RoomForbidden(f"the {actor.role} role cannot do this")
    return actor


def _guard(request: Request, room_id: str, action: access.Action) -> tuple[access.Actor, Room]:
    """The caller and the room, when they may take ``action`` there."""
    actor = _actor(request)
    return actor, access.require(actor, room_id, action)


def _is_admin(actor: access.Actor) -> bool:
    return not actor.is_service and actor.role in _ADMIN_ROLES


def _audit(request: Request, event: str, room_id: str, **details: Any) -> None:
    audited(request, event, action=room_id, room_id=room_id, **details)


def _announce(actor: access.Actor, room_id: str, *, task_id: str | None = None) -> None:
    """A ``room.updated`` event. Ids only: listeners re-read what they may see."""
    payload = {"room_id": room_id, "tenant_id": actor.tenant_id}
    if task_id:
        payload["task_id"] = task_id
    publish(
        "agent",
        "room.updated",
        payload,
        source="bridge",
        tenant_id=actor.tenant_id,
    )


def _eligible(actor: access.Actor, user_id: str, roles: frozenset[str]) -> dict[str, Any]:
    """The active account of the actor's company ``user_id`` names, else ValueError."""
    canonical = canonical_uuid(user_id) or ""
    account = dal.get_accounts(actor.tenant_id, [canonical]).get(canonical) if canonical else None
    if not isinstance(account, dict) or account.get("status") != "active":
        raise ValueError("no active account in this company has that id")
    if account.get("role") not in roles:
        raise ValueError(f"a {account.get('role')} account cannot take this on")
    return dict(account)


# ─── shapes ──────────────────────────────────────────────────────────────


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _summary_json(summary: RoomSummary) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": summary.id,
        "kind": summary.kind,
        "title": summary.title,
        "agentId": summary.agent_id,
        "paused": summary.paused,
        "archived": summary.archived,
        "unread": summary.unread,
        "lastActivityAt": _iso(summary.last_activity_at),
    }
    latest = summary.latest_edition
    if latest:
        body["latestEdition"] = {
            "id": latest["id"],
            "seq": latest["seq"],
            "publishedSeq": latest.get("published_seq"),
            "title": latest["title"],
            "periodStart": _iso(latest["period_start"]),
            "periodEnd": _iso(latest["period_end"]),
        }
    return body


def _name(accounts: dict[str, dict[str, Any]], user_id: str | None) -> str:
    account = accounts.get(user_id or "")
    return str(account["display_name"]) if account else "Unknown user"


def _member_json(member: RoomMember, accounts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "userId": member.user_id,
        "displayName": _name(accounts, member.user_id),
        "assigned": member.assigned,
    }


def _room_json(actor: access.Actor, room: Room) -> dict[str, Any]:
    """``Room``: the summary plus definition, the caller's access, members, run-as."""
    summary = dal.room_summary(actor.tenant_id, room.id, actor.user_id)
    if summary is None:
        raise RoomNotFound(room.id)
    members = dal.list_members(actor.tenant_id, room.id)
    accounts = dal.get_accounts(
        actor.tenant_id, [m.user_id for m in members] + [room.run_as_user_id or ""]
    )
    own = next((m for m in members if m.user_id == actor.user_id), None)
    body = _summary_json(summary)
    body.update(
        {
            "instructions": room.instructions,
            "settings": dict(room.settings),
            "rev": room.rev,
            "access": access.room_access(actor, room, own),
            "members": [_member_json(m, accounts) for m in members],
            "nextRunAt": _iso(dal.next_run_at(actor.tenant_id, room.id)),
        }
    )
    if room.run_as_user_id:
        body["runsAs"] = {
            "userId": room.run_as_user_id,
            "displayName": _name(accounts, room.run_as_user_id),
        }
    return body


def _edition_json(edition: Edition) -> dict[str, Any]:
    return {
        "id": edition.id,
        "seq": edition.seq,
        "kind": edition.kind,
        "status": edition.status,
        "preview": edition.preview,
        "periodStart": _iso(edition.period_start),
        "periodEnd": _iso(edition.period_end),
        "grain": edition.grain,
        "revision": edition.revision,
        "publishedSeq": edition.published_seq,
        "title": edition.title,
        "summary": edition.summary,
        "blocks": edition.blocks,
        "suggestions": edition.suggestions,
        "provenance": edition.provenance,
        "createdAt": _iso(edition.created_at),
    }


def _revision_json(revision: Revision, accounts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "rev": revision.rev,
        "changedBy": revision.changed_by,
        "changedByName": _name(accounts, revision.changed_by),
        "changedAt": _iso(revision.changed_at),
        "definition": revision.definition,
    }


def _task_json(task: RoomTask) -> dict[str, Any]:
    return {
        "taskId": task.task_id,
        "editionId": task.edition_id,
        "origin": task.origin,
        "suggestionKey": task.suggestion_key,
        "acceptedBy": task.accepted_by,
        "dismissedBy": task.dismissed_by,
        "pending": task.pending,
        "createdAt": _iso(task.created_at),
    }


# ─── bodies ──────────────────────────────────────────────────────────────


class _Body(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class CreateRoomBody(_Body):
    kind: str
    title: str
    instructions: str = ""
    agent_id: str | None = Field(None, alias="agentId")
    schedule_text: str | None = Field(None, alias="scheduleText")
    tz: str | None = None
    default_grain: str | None = Field(None, alias="defaultGrain")
    share_with: list[str] = Field(default_factory=list, alias="shareWith")


class UpdateRoomBody(_Body):
    expected_rev: int = Field(alias="expectedRev")
    changes: dict[str, Any]


class RestoreBody(_Body):
    expected_rev: int | None = Field(None, alias="expectedRev")


class MemberBody(_Body):
    assigned: bool = False


class ReassignBody(_Body):
    user_id: str = Field(alias="userId")


class RunBody(_Body):
    preview: bool = False
    period: Any = None


class PeriodsBody(_Body):
    grain: str
    anchor: str


# ─── list, create ────────────────────────────────────────────────────────


@router.get("")
def api_list_rooms(request: Request, scope: str = Query("mine", pattern="^(mine|all)$")) -> Any:
    actor = _human(request)
    if scope == "mine":
        rooms = dal.list_rooms_for_user(actor.tenant_id, actor.user_id)
        return {"rooms": [_summary_json(s) for s in rooms]}
    if not _is_admin(actor):
        raise RoomForbidden("listing every workspace needs a company admin")
    rooms = dal.list_all_rooms(actor.tenant_id, include_archived=True)
    assigned = dal.assigned_members(actor.tenant_id, [s.id for s in rooms])
    accounts = dal.get_accounts(actor.tenant_id, [u for users in assigned.values() for u in users])
    listed = []
    for summary in rooms:
        body = _summary_json(summary)
        body["assigned"] = [
            {"userId": u, "displayName": _name(accounts, u)} for u in assigned.get(summary.id, [])
        ]
        listed.append(body)
    return {"rooms": listed}


@router.post("", status_code=201)
def api_create_room(request: Request, body: CreateRoomBody) -> Any:
    actor = _human(request, _WORKING_ROLES)
    try:
        if body.kind not in _CREATABLE_KINDS:
            raise ValueError("kind must be report or agent; open a task as a workspace instead")
        share_with = [_eligible(actor, u, _HUMAN_ROLES)["id"] for u in body.share_with]
        room = definition.create_room_with_schedule(
            actor,
            kind=body.kind,
            title=body.title,
            instructions=body.instructions,
            agent_id=body.agent_id,
            schedule_text=body.schedule_text,
            tz=body.tz,
            default_grain=body.default_grain,
            share_with=tuple(u for u in share_with if u != actor.user_id),
        )
    except ValueError as exc:
        return _invalid(exc)
    _audit(request, "room.create", room.id, kind=room.kind, shared_with=len(share_with))
    _announce(actor, room.id)
    return _room_json(actor, room)


@router.post("/from-task/{task_id}")
def api_open_task(task_id: str, request: Request) -> Any:
    """A CRM task's workspace, made on first use. A colleague who opens the same
    task joins its workspace as shared: the task is already theirs to see."""
    actor = _human(request, _WORKING_ROLES)
    from robothor.engine.manifest_store import main_agent_id

    room = dal.get_or_create_task_room(
        actor.tenant_id, task_id, agent_id=main_agent_id(), created_by=actor.user_id
    )
    if room is None:
        raise RoomNotFound(task_id)
    joined = False
    if not _is_admin(actor) and dal.get_member(actor.tenant_id, room.id, actor.user_id) is None:
        dal.add_member(actor.tenant_id, room.id, actor.user_id, added_by=actor.user_id)
        joined = True
    _audit(request, "room.open_task", room.id, task_id=room.task_id, joined=joined)
    return _room_json(actor, access.require(actor, room.id, "read"))


# ─── one room ────────────────────────────────────────────────────────────


@router.get("/{room_id}")
def api_get_room(room_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "read")
    return _room_json(actor, room)


@router.patch("/{room_id}")
def api_update_room(room_id: str, request: Request, body: UpdateRoomBody) -> Any:
    try:
        actor, room = _guard(request, room_id, "edit_definition")
    except RoomForbidden as exc:
        raise RoomForbidden(f"{exc}; duplicate the workspace to make your own copy") from exc
    changes = {_CHANGE_KEYS.get(k, k): v for k, v in body.changes.items()}
    try:
        saved = definition.apply_definition_change(actor, room.id, body.expected_rev, changes)
    except ValueError as exc:
        return _invalid(exc)
    _audit(request, "room.update", room.id, rev=saved.rev, fields=",".join(sorted(changes)))
    _announce(actor, room.id)
    return _room_json(actor, saved)


@router.post("/{room_id}/archive")
def api_archive_room(room_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "archive")
    archived = dal.archive_room(actor.tenant_id, room.id) or room
    _audit(request, "room.archive", room.id)
    _announce(actor, room.id)
    return _room_json(actor, archived)


@router.post("/{room_id}/unarchive")
def api_unarchive_room(room_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "archive")
    if not _is_admin(actor):
        raise RoomForbidden("restoring an archived workspace needs a company admin")
    restored = dal.unarchive_room(actor.tenant_id, room.id) or room
    _audit(request, "room.unarchive", room.id)
    _announce(actor, room.id)
    return _room_json(actor, restored)


@router.post("/{room_id}/duplicate", status_code=201)
def api_duplicate_room(room_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "duplicate")
    if actor.role not in _WORKING_ROLES:
        raise RoomForbidden(f"the {actor.role} role cannot create a workspace")
    copy = dal.duplicate_room(actor.tenant_id, room.id, actor.user_id)
    _audit(request, "room.duplicate", copy.id, source_room_id=room.id)
    _announce(actor, copy.id)
    return _room_json(actor, copy)


@router.post("/{room_id}/read")
def api_mark_read(room_id: str, request: Request) -> Any:
    """Move the caller's own read marks to now. Nobody else's change."""
    actor, room = _guard(request, room_id, "read")
    member = dal.mark_read(actor.tenant_id, room.id, actor.user_id)
    return {"read": member is not None}


# ─── revisions ───────────────────────────────────────────────────────────


@router.get("/{room_id}/revisions")
def api_list_revisions(room_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "read")
    revisions = dal.list_revisions(actor.tenant_id, room.id)
    accounts = dal.get_accounts(actor.tenant_id, [r.changed_by or "" for r in revisions])
    return {"revisions": [_revision_json(r, accounts) for r in revisions]}


@router.post("/{room_id}/revisions/{rev}/restore")
def api_restore_revision(
    room_id: str, rev: int, request: Request, body: RestoreBody | None = Body(None)
) -> Any:
    actor, room = _guard(request, room_id, "restore_revision")
    try:
        saved = dal.restore_revision(
            actor.tenant_id,
            room.id,
            rev,
            actor.user_id,
            expected_rev=body.expected_rev if body else None,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    _audit(request, "room.restore", room.id, restored_rev=rev, rev=saved.rev)
    _announce(actor, room.id)
    return _room_json(actor, saved)


# ─── members ─────────────────────────────────────────────────────────────


@router.get("/{room_id}/members")
def api_list_members(room_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "read")
    members = dal.list_members(actor.tenant_id, room.id)
    accounts = dal.get_accounts(actor.tenant_id, [m.user_id for m in members])
    return {"members": [_member_json(m, accounts) for m in members]}


@router.put("/{room_id}/members/{user_id}")
def api_share(
    room_id: str, user_id: str, request: Request, body: MemberBody | None = Body(None)
) -> Any:
    actor, room = _guard(request, room_id, "share")
    try:
        account = _eligible(actor, user_id, _HUMAN_ROLES)
    except ValueError as exc:
        return _invalid(exc)
    assigned = bool(body and body.assigned)
    existing = dal.get_member(actor.tenant_id, room.id, account["id"])
    member = dal.add_member(
        actor.tenant_id, room.id, account["id"], added_by=actor.user_id, assigned=assigned
    )
    _audit(request, "room.share", room.id, user_id=member.user_id, assigned=assigned)
    _announce(actor, room.id)
    if existing is None:
        sharer = dal.get_accounts(actor.tenant_id, [actor.user_id])
        send_notification(
            from_agent="workspaces",
            to_agent=member.user_id,
            notification_type="info",
            subject=f"{_name(sharer, actor.user_id)} shared the workspace “{room.title}” with you",
            metadata={"kind": "room_shared", "room_id": room.id, "link": room_link(room.id)},
            tenant_id=actor.tenant_id,
        )
    return _member_json(member, {account["id"]: account})


@router.delete("/{room_id}/members/{user_id}")
def api_unshare(room_id: str, user_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "share")
    if not dal.remove_member(actor.tenant_id, room.id, user_id):
        return JSONResponse({"error": "not a member of this workspace"}, status_code=404)
    removed = canonical_uuid(user_id)
    _audit(request, "room.unshare", room.id, user_id=removed)
    _announce(actor, room.id)
    return {"removed": removed}


@router.post("/{room_id}/reassign")
def api_reassign(room_id: str, request: Request, body: ReassignBody) -> Any:
    actor, room = _guard(request, room_id, "reassign")
    try:
        account = _eligible(actor, body.user_id, _WORKING_ROLES)
    except ValueError as exc:
        return _invalid(exc)
    moved = dal.reassign_room(actor.tenant_id, room.id, account["id"], reassigned_by=actor.user_id)
    _audit(request, "room.reassign", room.id, user_id=account["id"])
    _announce(actor, room.id)
    return _room_json(actor, moved)


# ─── editions ────────────────────────────────────────────────────────────


@router.get("/{room_id}/editions")
def api_list_editions(room_id: str, request: Request, limit: int = Query(50, ge=1, le=200)) -> Any:
    actor, room = _guard(request, room_id, "read")
    editions = dal.list_editions(actor.tenant_id, room.id, viewer_id=actor.user_id, limit=limit)
    return {"editions": [_edition_json(e) for e in editions]}


@router.get("/{room_id}/editions/{edition_id}")
def api_get_edition(room_id: str, edition_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "read")
    edition = dal.get_edition(actor.tenant_id, edition_id, viewer_id=actor.user_id)
    if edition is None or edition.room_id != room.id:
        return JSONResponse({"error": "edition not found"}, status_code=404)
    return _edition_json(edition)


# ─── engine-backed: run, periods ─────────────────────────────────────────


async def _engine(actor: access.Actor, room: Room, route: str, payload: dict[str, Any]) -> Any:
    """POST one engine room route on the caller's behalf; ``(status, body)``."""
    return await engine_request(
        "POST",
        f"/api/rooms/{room.id}/{route}",
        json={**payload, "requested_by": actor.user_id},
        tenant=actor.tenant_id,
    )


@router.post("/{room_id}/run")
async def api_run(room_id: str, request: Request, body: RunBody | None = Body(None)) -> Any:
    """Run now (or a preview for the requester only). The engine builds it."""
    actor, room = await asyncio.to_thread(_guard, request, room_id, "run")
    run = body or RunBody()
    payload: dict[str, Any] = {"preview": run.preview}
    if run.period is not None:
        payload["period"] = run.period
    status, answer = await _engine(actor, room, "run", payload)
    await asyncio.to_thread(_audit, request, "room.run", room.id, preview=run.preview)
    return JSONResponse(answer, status_code=status)


@router.post("/{room_id}/periods")
async def api_request_period(room_id: str, request: Request, body: PeriodsBody) -> Any:
    """The edition for another period: found, or built by the engine."""
    actor, room = await asyncio.to_thread(_guard, request, room_id, "request_period")
    payload = {"grain": body.grain, "anchor": body.anchor}
    status, answer = await _engine(actor, room, "periods", payload)
    await asyncio.to_thread(_audit, request, "room.request_period", room.id, grain=body.grain)
    return JSONResponse(answer, status_code=status)


# ─── tasks ───────────────────────────────────────────────────────────────


@router.get("/{room_id}/tasks")
def api_list_tasks(room_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "read")
    return {"tasks": [_task_json(t) for t in dal.list_room_tasks(actor.tenant_id, room.id)]}


def _decide(
    actor: access.Actor, room: Room, task_id: str, *, accept: bool
) -> tuple[RoomTask | None, JSONResponse | None]:
    """Accept or dismiss a linked task once: the decision, or the refusal."""
    decide = dal.accept_task if accept else dal.dismiss_task
    decided = decide(actor.tenant_id, room.id, task_id, actor.user_id)
    if decided is not None:
        _announce(actor, room.id, task_id=decided.task_id)
        return decided, None
    task = canonical_uuid(task_id)
    if any(t.task_id == task for t in dal.list_room_tasks(actor.tenant_id, room.id)):
        return None, JSONResponse({"error": "this task was already decided"}, status_code=409)
    return None, JSONResponse({"error": "task not linked to this workspace"}, status_code=404)


@router.post("/{room_id}/tasks/{task_id}/accept")
def api_accept_task(room_id: str, task_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "accept_task")
    decided, refusal = _decide(actor, room, task_id, accept=True)
    if decided is None:
        return refusal
    _audit(request, "room.task_accept", room.id, task_id=decided.task_id)
    return _task_json(decided)


@router.post("/{room_id}/tasks/{task_id}/dismiss")
def api_dismiss_task(room_id: str, task_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "accept_task")
    decided, refusal = _decide(actor, room, task_id, accept=False)
    if decided is None:
        return refusal
    _audit(request, "room.task_dismiss", room.id, task_id=decided.task_id)
    return _task_json(decided)
