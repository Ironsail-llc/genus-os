"""Workspaces ("rooms") bridge API.

Code says room; the dashboard says Workspaces. The gate is per room, not per
tenant: every handler opens with ``_guard(request, room_id, action)`` (or
``_human(request)`` where no room is named yet), which builds the caller from
the verified token and asks ``robothor.rooms.access``. No access is 404 (a room
id cannot be probed), access without the action is 403, a stale revision is
409 with the current one. Admin-only: ``?scope=all``, reassign and unarchive.

Definition changes go through ``robothor.rooms.definition``, which owns
validation (injection scan, schedule) and the revision
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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from robothor.crm.dal import send_notification
from robothor.events.bus import publish
from robothor.rooms import access, dal, definition, tasks
from robothor.rooms.errors import (
    InvalidBlocks,
    RevisionConflict,
    RoomBusy,
    RoomError,
    RoomForbidden,
    RoomNotFound,
    TaskAlreadyAccepted,
    TaskAlreadyDismissed,
    TaskNotFound,
)
from robothor.rooms.keys import canonical_uuid
from robothor.rooms.models import edition_json, room_link
from robothor.rooms.notify import parse_notify, stored_notify
from routers import _manifest_backend
from routers._audit import audited
from routers._engine_client import engine_request

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from datetime import date, datetime

    from robothor.rooms.models import Edition, Revision, Room, RoomMember, RoomSummary

logger = logging.getLogger(__name__)


#: Tenant roles that may hold a room at all, and the ones that may do work in
#: one (create, open a task, be reassigned to). A viewer reads only.
_HUMAN_ROLES = frozenset({"owner", "admin", "member", "user", "viewer"})
_WORKING_ROLES = _HUMAN_ROLES - {"viewer"}
_ADMIN_ROLES = frozenset({"owner", "admin"})
_CREATABLE_KINDS = frozenset({"report", "agent"})


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


#: The definition changes a workspace accepts, by either spelling, to the names
#: ``apply_definition_change`` takes. Nothing here limits cost or frequency.
_CHANGE_KEYS = {
    spelling: name
    for name in (
        "title",
        "instructions",
        "schedule_text",
        "tz",
        "default_grain",
        "paused",
        "notify_default",
    )
    for spelling in (name, _camel(name))
}


# ─── errors ──────────────────────────────────────────────────────────────
#
# One body for every refusal: ``{"detail": {"error": <code>, "message": <text>,
# "field": <camelCase field or null>, ...}}``, with ``currentRev`` on a revision
# conflict and whatever else a code names. ``_RoomsRoute`` applies it to the
# rooms errors, malformed bodies and the sign-in refusal raised anywhere in a
# handler; the engine's own answers pass through unchanged.


def _problem(
    status: int, code: str, message: str, *, field: str | None = None, **extra: Any
) -> JSONResponse:
    detail: dict[str, Any] = {"error": code, "message": message, "field": field, **extra}
    return JSONResponse({"detail": detail}, status_code=status)


#: The message each refusal reads. Fixed text only: an exception's own words
#: (a query, a path, a validator's echo of the input) never reach a response.
_MESSAGES = {
    "not_found": "Workspace not found.",
    "forbidden": "You don't have access to do that in this workspace.",
    "revision_conflict": "The workspace changed since you opened it.",
    "room_busy": "The agent is answering someone else in this workspace.",
    "invalid_blocks": "The edition's content is not in a form the dashboard can show.",
    "unsafe_instructions": (
        "The instructions look like an attempt to override the agent (a prompt injection)."
    ),
    "invalid_schedule": (
        "The schedule could not be understood. Use an interval such as “every 6 hours” "
        "or a cron expression such as “0 8 * * 1” (08:00 every Monday)."
    ),
    "ineligible_account": "That person has no active account here that can take this on.",
    "invalid_request": "The request is not valid.",
    "unauthenticated": "Sign in required.",
    "internal_error": "Something went wrong.",
}

#: A refused value the client can correct, by the field it names: still a
#: fixed sentence, never the exception's text.
_FIELD_MESSAGES = {
    "agentId": "That agent is not available in this company.",
    "tz": "That is not a time zone; use a name such as Europe/London.",
}

#: Why a refusal was a 403, when this router decided it (``Refusal.key``).
_FORBIDDEN = {
    "person": "Workspaces need a signed-in person.",
    "role": "Your role cannot do this.",
    "list_all": "Listing every workspace needs a company admin.",
    "unarchive": "Restoring an archived workspace needs a company admin.",
    "edit": (
        "Only the workspace's assigned people can change it; duplicate the workspace to "
        "make your own copy."
    ),
}


#: Body fields a refusal may name, by every spelling a client may send. A
#: field outside this table is never named.
_BODY_FIELDS = [
    "kind",
    "title",
    "instructions",
    "agent_id",
    "schedule_text",
    "tz",
    "default_grain",
    "share_with",
    "expected_rev",
    "changes",
    "assigned",
    "user_id",
    "preview",
    "period",
    "grain",
    "anchor",
    "end",
    "in_app",
    "muted",
    "channels",
    "rev",
]
_FIELDS = {spelling: _camel(name) for name in _BODY_FIELDS for spelling in (name, _camel(name))}


class Refusal(RoomForbidden):  # noqa: N818 - reads as the refusal it is
    """A 403 this router decided, with the reason it names (a ``_FORBIDDEN`` key)."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def room_error_response(_request: Any, exc: Exception) -> JSONResponse:
    """The one mapping from a rooms error to an HTTP answer."""
    if isinstance(exc, RoomNotFound):
        return _problem(404, "not_found", _MESSAGES["not_found"])
    if isinstance(exc, RoomForbidden):
        key = exc.key if isinstance(exc, Refusal) else ""
        return _problem(403, "forbidden", _FORBIDDEN.get(key, _MESSAGES["forbidden"]))
    if isinstance(exc, RevisionConflict):
        return _problem(
            409,
            "revision_conflict",
            _MESSAGES["revision_conflict"],
            currentRev=int(exc.current_rev),
        )
    if isinstance(exc, RoomBusy):
        return _problem(409, "room_busy", _MESSAGES["room_busy"])
    if isinstance(exc, TaskNotFound):
        return _problem(404, "not_found", "That task is not linked to this workspace.")
    if isinstance(exc, TaskAlreadyAccepted):
        by = _task_person(exc.by) or {"userId": "", "displayName": "Someone"}
        return _problem(
            409,
            "already_accepted",
            "Someone else has already taken this task.",
            acceptedBy=by,
        )
    if isinstance(exc, TaskAlreadyDismissed):
        return _problem(409, "already_dismissed", "This task was already dismissed.")
    if isinstance(exc, InvalidBlocks):
        return _problem(422, "invalid_blocks", _MESSAGES["invalid_blocks"], field="blocks")
    return _internal(exc)


def _internal(exc: BaseException) -> JSONResponse:
    """An unexpected failure: logged here with its traceback, a generic 500 out."""
    logger.error("workspace route failed", exc_info=exc)
    return _problem(500, "internal_error", _MESSAGES["internal_error"])


def _invalid(exc: ValueError, field: str | None = None) -> JSONResponse:
    """A 422 for a refused value: the definition's own codes, else ``invalid_request``."""
    if isinstance(exc, definition.UnsafeInstructions):
        code, field = "unsafe_instructions", "instructions"
    elif isinstance(exc, definition.InvalidSchedule):
        code, field = "invalid_schedule", "scheduleText"
    elif isinstance(exc, Ineligible):
        code, field = "ineligible_account", _FIELDS.get(exc.field, field)
    elif isinstance(exc, definition.RevisionNotFound):
        return _problem(404, "not_found", "That revision does not exist.", field="rev")
    else:
        code = "invalid_request"
        named = getattr(exc, "field", None)
        field = _FIELDS.get(named, field) if isinstance(named, str) else field
        if field in _FIELD_MESSAGES:
            return _problem(422, code, _FIELD_MESSAGES[field], field=field)
    return _problem(422, code, _MESSAGES[code], field=field)


def _malformed(exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = [part for part in first.get("loc", ()) if part != "body"]
    field = _FIELDS.get(loc[0]) if loc and isinstance(loc[0], str) else None
    return _problem(422, "invalid_request", _MESSAGES["invalid_request"], field=field)


class _RoomsRoute(APIRoute):
    """Every rooms route answers refusals in the one error shape."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def answer(request: Request) -> Response:
            try:
                return await handler(request)
            except RoomError as exc:
                return room_error_response(request, exc)
            except RequestValidationError as exc:
                return _malformed(exc)
            except StarletteHTTPException as exc:  # FastAPI's HTTPException included
                if exc.status_code == 401:
                    return _problem(401, "unauthenticated", _MESSAGES["unauthenticated"])
                if exc.status_code < 500:
                    # A client error (the framework's 400 for an unreadable body):
                    # its own status, a fixed message, nothing logged as a failure.
                    return _problem(
                        exc.status_code, "invalid_request", _MESSAGES["invalid_request"]
                    )
                return _internal(exc)
            except Exception as exc:  # noqa: BLE001 - answered generically, logged in full
                return _internal(exc)

        return answer


router = APIRouter(prefix="/api/rooms", tags=["rooms"], route_class=_RoomsRoute)


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
        raise Refusal("person")
    if actor.role not in roles:
        raise Refusal("role")
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


class Ineligible(ValueError):  # noqa: N818 - reads as the refusal it is
    """The account cannot be shared with or handed a workspace."""

    def __init__(self, message: str, field: str) -> None:
        super().__init__(message)
        self.field = field


class AgentsUnavailableError(RuntimeError):
    """The acting company's engine could not say which agents it has."""


def _company_agents(actor: access.Actor) -> frozenset[str]:
    """The agent ids the acting company's engine loads.

    The engine owns the fleet; this asks it the way the fleet views do
    (``_manifest_backend``) rather than reading a manifest directory in the
    bridge process, which may not be the company's or may not be mounted.
    """
    try:
        scan = _manifest_backend.scan_from_worker_thread(actor.tenant_id)
    except HTTPException as exc:
        raise AgentsUnavailableError("the manifest scan failed") from exc
    if getattr(scan, "dir_readable", True) is False:
        raise AgentsUnavailableError("the agent directory is not readable")
    return frozenset(
        str(m.get("id"))
        for m in getattr(scan, "manifests", ())
        if isinstance(m, dict) and m.get("id")
    )


def _eligible(
    actor: access.Actor, user_id: str, roles: frozenset[str], *, field: str = "userId"
) -> dict[str, Any]:
    """The active account of the actor's company ``user_id`` names, else ``Ineligible``."""
    canonical = canonical_uuid(user_id) or ""
    account = dal.get_accounts(actor.tenant_id, [canonical]).get(canonical) if canonical else None
    if not isinstance(account, dict) or account.get("status") != "active":
        raise Ineligible("no active account in this company has that id", field)
    if account.get("role") not in roles:
        raise Ineligible(f"a {account.get('role')} account cannot take this on", field)
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
    """The contract's ``Edition``: the one shape the engine answers too."""
    return edition_json(edition)


def _revision_json(revision: Revision, accounts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "rev": revision.rev,
        "changedBy": revision.changed_by,
        "changedByName": _name(accounts, revision.changed_by),
        "changedAt": _iso(revision.changed_at),
        "definition": revision.definition,
    }


def _person(accounts: dict[str, dict[str, Any]], user_id: str | None) -> dict[str, str] | None:
    return {"userId": user_id, "displayName": _name(accounts, user_id)} if user_id else None


def _task_person(person: Any) -> dict[str, str] | None:
    if person is None:
        return None
    return {"userId": person.user_id, "displayName": person.display_name or "Unknown user"}


def _task_json(view: tasks.RoomTaskView) -> dict[str, Any]:
    """A linked task as the workspace shows it (``robothor.rooms.tasks``)."""
    return {
        "taskId": view.task_id,
        "title": view.title,
        "status": view.status or "TODO",
        "origin": view.origin,
        "editionId": view.edition_id,
        "assignee": _task_person(view.assignee),
        "acceptedBy": _task_person(view.accepted_by),
        "dismissed": view.dismissed,
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
    #: None keeps an existing member's role (a new member joins shared).
    assigned: bool | None = None


class ReassignBody(_Body):
    user_id: str = Field(alias="userId")


class RunBody(_Body):
    preview: bool = False
    period: Any = None


class NotifyBody(_Body):
    in_app: bool = Field(True, alias="inApp")
    muted: bool = False
    channels: dict[str, bool] = Field(default_factory=dict)


class PeriodsBody(_Body):
    grain: str
    anchor: str
    end: str | None = None


# ─── list, create ────────────────────────────────────────────────────────


@router.get("")
def api_list_rooms(request: Request, scope: str = Query("mine", pattern="^(mine|all)$")) -> Any:
    actor = _human(request)
    if scope == "mine":
        rooms = dal.list_rooms_for_user(actor.tenant_id, actor.user_id)
        return {"rooms": [_summary_json(s) for s in rooms]}
    if not _is_admin(actor):
        raise Refusal("list_all")
    rooms = dal.list_all_rooms(actor.tenant_id, include_archived=True)
    members = dal.members_of_rooms(actor.tenant_id, [s.id for s in rooms])
    accounts = dal.get_accounts(
        actor.tenant_id, [m.user_id for room_members in members.values() for m in room_members]
    )
    listed = []
    for summary in rooms:
        body = _summary_json(summary)
        rows = [_member_json(m, accounts) for m in members.get(summary.id, [])]
        body["members"] = rows
        body["assigned"] = [
            {"userId": m["userId"], "displayName": m["displayName"]} for m in rows if m["assigned"]
        ]
        listed.append(body)
    return {"rooms": listed}


@router.get("/directory")
def api_directory(request: Request) -> Any:
    """Who the caller can share a workspace with: the active people of their
    acting company. Names only; no email, no role."""
    actor = _human(request, _WORKING_ROLES)
    users = dal.list_directory(actor.tenant_id)
    return {"users": [{"userId": u["id"], "displayName": u["display_name"]} for u in users]}


@router.post("", status_code=201)
def api_create_room(request: Request, body: CreateRoomBody) -> Any:
    actor = _human(request, _WORKING_ROLES)
    try:
        if body.kind not in _CREATABLE_KINDS:
            return _problem(
                422,
                "invalid_request",
                "A workspace is a report or an agent; open a task as a workspace instead.",
                field="kind",
            )
        if not body.title.strip():
            return _problem(422, "invalid_request", "A workspace needs a title.", field="title")
        share_with = [
            _eligible(actor, u, _HUMAN_ROLES, field="shareWith")["id"] for u in body.share_with
        ]
        try:
            agents = _company_agents(actor)
        except AgentsUnavailableError:
            return _problem(
                502,
                "agents_unavailable",
                "the company's agents could not be checked; try again in a moment",
                field="agentId",
            )
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
            agent_known=agents.__contains__,
        )
    except ValueError as exc:
        return _invalid(exc)
    _audit(request, "room.create", room.id, kind=room.kind, shared_with=len(share_with))
    _announce(actor, room.id)
    return _room_json(actor, room)


@router.post("/from-task/{task_id}")
def api_open_task(task_id: str, request: Request) -> Any:
    """A CRM task's workspace, made on first open (``robothor.rooms.tasks``):
    the opener assigned; later, only the task's assignee joins this way, and
    never after being removed. Anyone else gets not-found until shared in."""
    actor = _human(request, _WORKING_ROLES)
    room = tasks.open_task_room(actor, task_id)
    _audit(request, "room.open_task", room.id, task_id=room.task_id)
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
        raise Refusal("edit") from exc
    if not body.changes or any(k not in _CHANGE_KEYS for k in body.changes):
        return _problem(
            422,
            "invalid_request",
            "Change only the title, instructions, schedule, time zone, default period, "
            "notifications or pause.",
            field="changes",
        )
    changes = {_CHANGE_KEYS[k]: v for k, v in body.changes.items()}
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
    archived = definition.archive_room(actor, room.id)
    _audit(request, "room.archive", room.id)
    _announce(actor, room.id)
    return _room_json(actor, archived)


@router.post("/{room_id}/unarchive")
def api_unarchive_room(room_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "archive")
    if not _is_admin(actor):
        raise Refusal("unarchive")
    restored = definition.unarchive_room(actor, room.id)
    _audit(request, "room.unarchive", room.id)
    _announce(actor, room.id)
    return _room_json(actor, restored)


@router.post("/{room_id}/duplicate", status_code=201)
def api_duplicate_room(room_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "duplicate")
    if actor.role not in _WORKING_ROLES:
        raise Refusal("role")
    copy = definition.duplicate_room(actor, room.id)
    _audit(request, "room.duplicate", copy.id, source_room_id=room.id)
    _announce(actor, copy.id)
    return _room_json(actor, copy)


@router.post("/{room_id}/read")
def api_mark_read(room_id: str, request: Request) -> Any:
    """Move the caller's own read marks to now. Nobody else's change."""
    actor, room = _guard(request, room_id, "read")
    member = dal.mark_read(actor.tenant_id, room.id, actor.user_id)
    return {"read": member is not None}


# ─── notification preferences ───────────────────────────────────────────

#: How a channel name reads in the dashboard; anything else is title-cased.
_CHANNEL_LABELS = {"telegram": "Telegram", "slack": "Slack", "whatsapp": "WhatsApp"}


def _notify_json(notify: Any, linked: list[str]) -> dict[str, Any]:
    """The member's choices exactly as fan-out reads them (``rooms.notify``)."""
    prefs = parse_notify(notify)
    return {
        "inApp": prefs.in_app,
        "muted": prefs.muted,
        "channels": dict.fromkeys(prefs.channels, True),
        "linkedChannels": [
            {"name": name, "label": _CHANNEL_LABELS.get(name, name.title())} for name in linked
        ],
    }


@router.get("/{room_id}/notify")
def api_get_notify(room_id: str, request: Request) -> Any:
    """The caller's own notification preferences for this workspace."""
    actor, room = _guard(request, room_id, "read")
    member = dal.get_member(actor.tenant_id, room.id, actor.user_id)
    linked = dal.linked_channels(actor.tenant_id, actor.user_id)
    return _notify_json(member.notify if member else {}, linked)


@router.put("/{room_id}/notify")
def api_set_notify(room_id: str, request: Request, body: NotifyBody) -> Any:
    """Set the caller's own preferences. Read access is enough: it touches only
    their row. A channel can be turned on only once the caller has linked it."""
    actor, room = _guard(request, room_id, "read")
    linked = dal.linked_channels(actor.tenant_id, actor.user_id)
    unlinked = sorted(name for name, on in body.channels.items() if on and name not in linked)
    if unlinked:
        return _problem(
            422,
            "channel_not_linked",
            f"link {', '.join(unlinked)} to your account before turning it on",
            field="channels",
        )
    notify = stored_notify(in_app=body.in_app, muted=body.muted, channels=body.channels)
    member = dal.set_notify(actor.tenant_id, room.id, actor.user_id, notify)
    if member is None:
        return _problem(409, "not_a_member", "only a member of this workspace has preferences")
    _audit(request, "room.notify", room.id, muted=body.muted)
    return _notify_json(member.notify, linked)


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
        saved = definition.restore_room_revision(
            actor, room.id, rev, expected_rev=body.expected_rev if body else None
        )
    except ValueError as exc:
        return _invalid(exc)
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
    wanted = body.assigned if body else None
    if wanted and account.get("role") not in _WORKING_ROLES:
        return _invalid(Ineligible(f"a {account.get('role')} account cannot be assigned", "userId"))
    existing = dal.get_member(actor.tenant_id, room.id, account["id"])
    if existing is not None and wanted is False and existing.assigned:
        refusal = _keeps_a_manager(actor, room, existing.user_id)
        if refusal is not None:
            return refusal
    try:
        shared = definition.share_room(actor, room.id, account["id"], assigned=wanted)
    except ValueError as exc:
        return _invalid(exc)
    member = shared.member
    _audit(request, "room.share", room.id, user_id=member.user_id, assigned=member.assigned)
    _announce(actor, room.id)
    if shared.added:
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


def _keeps_a_manager(actor: access.Actor, room: Room, user_id: str) -> JSONResponse | None:
    """Refuse taking ``user_id`` out of the assigned set when the workspace would
    run as someone without access, or have nobody left to manage it."""
    if user_id == room.run_as_user_id:
        return _problem(
            422,
            "run_as_user",
            "the workspace runs as this person; reassign it to someone else first",
            field="userId",
        )
    others = [
        m for m in dal.list_members(actor.tenant_id, room.id) if m.assigned and m.user_id != user_id
    ]
    if not others:
        return _problem(
            422,
            "last_assigned",
            "this is the workspace's only assigned member; assign someone else first",
            field="userId",
        )
    return None


@router.delete("/{room_id}/members/{user_id}")
def api_unshare(room_id: str, user_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "share")
    member = dal.get_member(actor.tenant_id, room.id, user_id)
    if member is not None and (member.assigned or member.user_id == room.run_as_user_id):
        refusal = _keeps_a_manager(actor, room, member.user_id)
        if refusal is not None:
            return refusal
    if not dal.remove_member(actor.tenant_id, room.id, user_id, removed_by=actor.user_id):
        return _problem(404, "not_found", "not a member of this workspace", field="userId")
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
    editions = dal.list_editions(
        actor.tenant_id, room.id, viewer_id=actor.user_id, include_unpublished=True, limit=limit
    )
    return {"editions": [_edition_json(e) for e in editions]}


@router.get("/{room_id}/editions/{edition_id}")
def api_get_edition(room_id: str, edition_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "read")
    edition = dal.get_edition(actor.tenant_id, edition_id, viewer_id=actor.user_id)
    if edition is None or edition.room_id != room.id:
        return _problem(404, "not_found", "edition not found")
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
    if body.end is not None:
        payload["end"] = body.end
    status, answer = await _engine(actor, room, "periods", payload)
    await asyncio.to_thread(_audit, request, "room.request_period", room.id, grain=body.grain)
    return JSONResponse(answer, status_code=status)


# ─── tasks ───────────────────────────────────────────────────────────────


@router.get("/{room_id}/tasks")
def api_list_tasks(room_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "read")
    return {"tasks": [_task_json(v) for v in tasks.list_tasks(actor, room.id)]}


@router.post("/{room_id}/tasks/{task_id}/accept")
def api_accept_task(room_id: str, task_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "accept_task")
    view = tasks.accept_task(actor, room.id, task_id)
    _audit(request, "room.task_accept", room.id, task_id=view.task_id)
    _announce(actor, room.id, task_id=view.task_id)
    return _task_json(view)


@router.post("/{room_id}/tasks/{task_id}/dismiss")
def api_dismiss_task(room_id: str, task_id: str, request: Request) -> Any:
    actor, room = _guard(request, room_id, "accept_task")
    view = tasks.dismiss_task(actor, room.id, task_id)
    _audit(request, "room.task_dismiss", room.id, task_id=view.task_id)
    _announce(actor, room.id, task_id=view.task_id)
    return _task_json(view)
