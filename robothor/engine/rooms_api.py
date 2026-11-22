"""Workspace (room) routes the engine serves: the ones that start runs.

The bridge owns the rest of ``/api/rooms``. A run lives in THIS process, so
asking for a period and running a room now (or previewing it) come here.

Who is asking: a human token is its own identity (any ``requested_by`` in the
body is ignored). The bridge calls with its SERVICE token (its engine
credential, ``engine:control``, which ``engine/auth.py`` requires of a service
here) after its own access check and names the person in ``requested_by``. Only
the bridge's service identity may name a person: any other service gets 403; the engine then reads that
person's role from their account, never from the body, and checks their
access again. An account that is missing, inactive, or lives in another tenant
gets the same 404 as no access (so a parent tenant's admin acting in a child
cannot be served through the service path; they call with their own token).
Refusals map as the rest of the rooms API: no access 404, access without the
action 403.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool, StrictStr

from robothor.engine.auth import request_context
from robothor.rooms import periods, runs
from robothor.rooms.access import Actor
from robothor.rooms.errors import RoomForbidden, RoomNotFound
from robothor.rooms.keys import canonical_uuid
from robothor.rooms.models import edition_json

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

    from robothor.auth.deps import AuthContext

__all__ = ["register", "router"]

logger = logging.getLogger(__name__)

#: What a refused period says. Fixed text: an exception's own message can quote
#: input or internals, so it never reaches a response.
_PERIOD_MESSAGES = {
    "invalid_time_zone": "the workspace's time zone is not a valid IANA name",
    "invalid_grain": "grain must be day, week, month or custom",
    "invalid_anchor": "anchor must be a date (YYYY-MM-DD), YYYY-MM, start/end, or latest",
    "not_over": "that period is not over yet",
    "not_a_report": "only a report workspace has periods",
}

router = APIRouter(prefix="/api/rooms", tags=["rooms"])

#: The service identity of the bridge's engine credential
#: (``crm/bridge/routers/_engine_client.SERVICE_ID``).
BRIDGE_SERVICE_ID = "genus-bridge"


class PeriodRequest(BaseModel):
    grain: StrictStr
    #: ``YYYY-MM-DD`` (``YYYY-MM`` for a month, ``start/end`` for custom);
    #: omitted or ``latest`` for the last complete period.
    anchor: StrictStr | None = None
    #: The person a SERVICE caller (the bridge) acts for. Ignored for a human token.
    requested_by: StrictStr | None = None
    #: The company that person is at home in, when they act from another one
    #: (switched in). A claim the engine checks; ignored for a human token.
    acting_from: StrictStr | None = None


class RunRequest(BaseModel):
    #: A preview: an edition only the requester sees, and nobody is told.
    preview: StrictBool = False
    #: ``{grain, start, end}`` with ISO dates; omitted for the last complete period.
    period: dict[str, Any] | None = None
    #: The person a SERVICE caller (the bridge) acts for. Ignored for a human token.
    requested_by: StrictStr | None = None
    #: The company that person is at home in, when they act from another one
    #: (switched in). A claim the engine checks; ignored for a human token.
    acting_from: StrictStr | None = None


#: What a refused run says, by reason. Fixed text, as for periods.
_RUN_MESSAGES = {
    "paused": "the workspace is paused; someone assigned to it can run it",
    "archived": "the workspace is archived; unarchive it to run it",
    "invalid_run": "period must be {grain, start, end} with ISO dates, in a report workspace",
}


class RequesterRequired(Exception):  # noqa: N818 - a refusal, read as one
    """A service call that names no person."""


class NotTheBridge(Exception):  # noqa: N818 - a refusal, read as one
    """A service other than the bridge tried to act for a person."""


def _requester(
    auth: AuthContext, requested_by: str | None, acting_from: str | None = None
) -> Actor | None:
    """The person asking: the token's own identity, or for a service token the
    account ``requested_by`` names, with that account's role, in this engine's
    company. That account stands here when it lives in this company and is
    active; or when it acts from another (``acting_from``, as the bridge
    admitted a switched-in caller) and passes the rule the switch applies
    (:func:`_switched_in`). Otherwise None (answered 404, as for anyone the
    engine does not know). Nothing in the body sets the role. Blocking."""
    if not auth.is_service:
        return Actor.from_auth(auth)
    if auth.user_id != BRIDGE_SERVICE_ID:
        raise NotTheBridge
    if not requested_by:
        raise RequesterRequired
    user_id = canonical_uuid(requested_by)
    if user_id is None:
        return None
    from robothor.auth import accounts

    if acting_from and acting_from != auth.tenant_id:
        return _switched_in(user_id, acting_from, auth.tenant_id)
    account = accounts.get_account_by_id(user_id)
    if (
        not account
        or str(account.get("tenant_id") or "") != auth.tenant_id
        or account.get("status") != "active"
    ):
        return None
    return Actor(user_id=user_id, role=str(account.get("role") or ""), tenant_id=auth.tenant_id)


#: Who may act in a descendant company (``POST /api/auth/switch-tenant``).
_SWITCH_ROLES = frozenset({"owner", "admin"})


def _switched_in(user_id: str, home: str, here: str) -> Actor | None:
    """The person acting here from ``home``, by the rule the switch applies:
    their account is active and lives in ``home``, they are an owner or admin
    there, and this company is among ``home``'s descendants. All of it read
    here, under ``home``'s scope; any failure to read refuses. Blocking."""
    from robothor.auth import accounts
    from robothor.db.connection import tenant_scope
    from robothor.engine.permissions import resolve_accessible_tenants

    try:
        with tenant_scope(home):
            account = accounts.get_account_by_id(user_id)
            if (
                not account
                or str(account.get("tenant_id") or "") != home
                or account.get("status") != "active"
                or account.get("role") not in _SWITCH_ROLES
            ):
                return None
            role = str(account["role"])
            if here not in resolve_accessible_tenants(home, role):
                return None
    except Exception:  # noqa: BLE001 - cannot check, so refuse
        logger.warning("could not check a switched-in requester; refused", exc_info=True)
        return None
    return Actor(user_id=user_id, role=role, tenant_id=here)


def _refusal(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


@router.post("/{room_id}/periods")
async def request_room_period(room_id: str, body: PeriodRequest, request: Request) -> JSONResponse:
    """The period's edition: 200 when it exists, 202 when a run was started."""
    try:
        return await _answer(room_id, body, request)
    except Exception:
        logger.exception("period request failed for a workspace")
        return _refusal(500, "internal_error", "the period could not be requested")


async def _answer(room_id: str, body: PeriodRequest, request: Request) -> JSONResponse:
    try:
        actor = await asyncio.to_thread(
            _requester, request_context(request), body.requested_by, body.acting_from
        )
    except NotTheBridge:
        return _refusal(403, "forbidden", "only the bridge may act for a person")
    except RequesterRequired:
        return _refusal(400, "requested_by_required", "a service call names the person asking")
    if actor is None:
        return _refusal(404, "room_not_found", "no such workspace")
    try:
        edition, started = await periods.request_period(actor, room_id, body.grain, body.anchor)
    except RoomNotFound:
        return _refusal(404, "room_not_found", "no such workspace")
    except RoomForbidden:
        return _refusal(403, "forbidden", "you may not request a period in this workspace")
    except runs.RunRefused as exc:
        reason = exc.reason if exc.reason in ("paused", "archived") else "paused"
        return _refusal(409, reason, _RUN_MESSAGES[reason])
    except periods.RoomRunUnavailable:
        return _refusal(503, "runs_unavailable", "workspace runs are not available right now")
    except periods.PeriodError as exc:
        reason = exc.reason if exc.reason in _PERIOD_MESSAGES else "invalid_anchor"
        return _refusal(422, "invalid_period", _PERIOD_MESSAGES[reason], reason=reason)
    return JSONResponse(edition_json(edition), status_code=202 if started else 200)


@router.post("/{room_id}/run")
async def run_room_now(room_id: str, body: RunRequest, request: Request) -> JSONResponse:
    """Run a report room now (or preview it): 202 started, 200 joined a run in progress."""
    try:
        return await _run_answer(room_id, body, request)
    except Exception:
        logger.exception("run request failed for a workspace")
        return _refusal(500, "internal_error", "the workspace could not be run")


async def _run_answer(room_id: str, body: RunRequest, request: Request) -> JSONResponse:
    try:
        actor = await asyncio.to_thread(
            _requester, request_context(request), body.requested_by, body.acting_from
        )
    except NotTheBridge:
        return _refusal(403, "forbidden", "only the bridge may act for a person")
    except RequesterRequired:
        return _refusal(400, "requested_by_required", "a service call names the person asking")
    if actor is None:
        return _refusal(404, "room_not_found", "no such workspace")
    try:
        edition, joined = await runs.start_room_run(
            actor, room_id, preview=body.preview, period=body.period
        )
    except RoomNotFound:
        return _refusal(404, "room_not_found", "no such workspace")
    except RoomForbidden:
        return _refusal(403, "forbidden", "you may not run this workspace")
    except runs.RunRefused as exc:
        reason = exc.reason if exc.reason in ("paused", "archived") else "paused"
        return _refusal(409, reason, _RUN_MESSAGES[reason])
    except ValueError:
        return _refusal(422, "invalid_run", _RUN_MESSAGES["invalid_run"])
    return JSONResponse(edition_json(edition), status_code=200 if joined else 202)


def register(app: FastAPI) -> None:
    """Mount the engine's rooms routes."""
    app.include_router(router)
