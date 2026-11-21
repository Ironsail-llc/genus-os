"""Workspace (room) routes the engine serves: the ones that start runs.

The bridge owns the rest of ``/api/rooms``. A run lives in THIS process, so
asking for a period (and, later, running a room now) comes here.

Who is asking: a human token is its own identity (any ``requested_by`` in the
body is ignored). The bridge calls with its SERVICE token after its own access
check and names the person in ``requested_by``; the engine then reads that
person's role from their account, never from the body, and checks their
access again. An account that is missing, inactive, or lives in another tenant
gets the same 404 as no access (so a parent tenant's admin acting in a child
cannot be served through the service path; they call with their own token).
Refusals map as the rest of the rooms API: no access 404, access without the
action 403.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictStr

from robothor.engine.auth import request_context
from robothor.rooms import periods
from robothor.rooms.access import Actor
from robothor.rooms.errors import RoomForbidden, RoomNotFound
from robothor.rooms.keys import canonical_uuid
from robothor.rooms.models import edition_json

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

    from robothor.auth.deps import AuthContext

__all__ = ["register", "router"]

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


class PeriodRequest(BaseModel):
    grain: StrictStr
    #: ``YYYY-MM-DD`` (``YYYY-MM`` for a month, ``start/end`` for custom);
    #: omitted or ``latest`` for the last complete period.
    anchor: StrictStr | None = None
    #: The person a SERVICE caller (the bridge) acts for. Ignored for a human token.
    requested_by: StrictStr | None = None


class RequesterRequired(Exception):  # noqa: N818 - a refusal, read as one
    """A service call that names no person."""


def _requester(auth: AuthContext, requested_by: str | None) -> Actor | None:
    """The person asking: the token's own identity, or for a service token the
    account ``requested_by`` names, with that account's role. None when that
    account does not stand in the token's tenant. Blocking (one SELECT)."""
    if not auth.is_service:
        return Actor.from_auth(auth)
    if not requested_by:
        raise RequesterRequired
    user_id = canonical_uuid(requested_by)
    if user_id is None:
        return None
    from robothor.auth import accounts

    account = accounts.get_account_by_id(user_id)
    if (
        not account
        or str(account.get("tenant_id") or "") != auth.tenant_id
        or account.get("status") != "active"
    ):
        return None
    return Actor(user_id=user_id, role=str(account.get("role") or ""), tenant_id=auth.tenant_id)


def _refusal(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


@router.post("/{room_id}/periods")
async def request_room_period(room_id: str, body: PeriodRequest, request: Request) -> JSONResponse:
    """The period's edition: 200 when it exists, 202 when a run was started."""
    try:
        actor = await asyncio.to_thread(_requester, request_context(request), body.requested_by)
    except RequesterRequired:
        return _refusal(400, "requested_by_required", "a service call names the person asking")
    if actor is None:
        return _refusal(404, "room_not_found", "no such workspace")
    try:
        edition, started = await periods.request_period(actor, room_id, body.grain, body.anchor)
    except RoomNotFound:
        return _refusal(404, "room_not_found", "no such workspace")
    except RoomForbidden as exc:
        return _refusal(403, "forbidden", str(exc))
    except periods.OnDemandCapReached as exc:
        return _refusal(429, "on_demand_cap", str(exc), limit=exc.limit, used=exc.used)
    except periods.RoomRunUnavailable as exc:
        return _refusal(503, "runs_unavailable", str(exc))
    except ValueError as exc:
        return _refusal(422, "invalid_period", str(exc))
    return JSONResponse(edition_json(edition), status_code=202 if started else 200)


def register(app: FastAPI) -> None:
    """Mount the engine's rooms routes."""
    app.include_router(router)
