"""Workspace (room) routes the engine serves: the ones that start runs.

The bridge owns the rest of ``/api/rooms``. A run lives in THIS process, so
asking for a period (and, later, running a room now) comes here. The caller is
the verified token; the body only names the period. Refusals map as the rest
of the rooms API: no access 404, access without the action 403.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictStr

from robothor.engine.auth import request_context
from robothor.rooms import periods
from robothor.rooms.access import Actor
from robothor.rooms.errors import RoomForbidden, RoomNotFound
from robothor.rooms.models import edition_json

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

__all__ = ["register", "router"]

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


class PeriodRequest(BaseModel):
    grain: StrictStr
    #: ``YYYY-MM-DD`` (``YYYY-MM`` for a month, ``start/end`` for custom);
    #: omitted or ``latest`` for the last complete period.
    anchor: StrictStr | None = None


def _refusal(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status)


@router.post("/{room_id}/periods")
async def request_room_period(room_id: str, body: PeriodRequest, request: Request) -> JSONResponse:
    """The period's edition: 200 when it exists, 202 when a run was started."""
    actor = Actor.from_auth(request_context(request))
    try:
        edition, started = await periods.request_period(actor, room_id, body.grain, body.anchor)
    except RoomNotFound:
        return _refusal(404, "room_not_found", "no such workspace")
    except RoomForbidden as exc:
        return _refusal(403, "forbidden", str(exc))
    except periods.OnDemandCapReached as exc:
        return _refusal(429, "cap_reached", str(exc), limit=exc.limit, used=exc.used)
    except periods.RoomRunUnavailable as exc:
        return _refusal(503, "runs_unavailable", str(exc))
    except ValueError as exc:
        return _refusal(422, "invalid_period", str(exc))
    return JSONResponse(edition_json(edition), status_code=202 if started else 200)


def register(app: FastAPI) -> None:
    """Mount the engine's rooms routes."""
    app.include_router(router)
