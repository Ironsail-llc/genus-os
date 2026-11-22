"""``room_request_period``: the edition of a report workspace's period.

A wrapper over ``robothor.rooms.periods.request_period``. The caller is the
human the run is for (``ctx.user_id`` / ``ctx.user_role``), never anything in
the arguments; a refusal comes back as an ``error`` the agent can relay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.rooms import periods
from robothor.rooms.access import Actor
from robothor.rooms.errors import RoomForbidden, RoomNotFound
from robothor.rooms.models import edition_json, room_link
from robothor.rooms.runs import RunRefused

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


async def _room_request_period(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    room_id, grain = args.get("room_id"), args.get("grain")
    if not isinstance(room_id, str) or not room_id or not isinstance(grain, str) or not grain:
        return {"error": "room_id and grain are required"}
    if not ctx.user_id:
        return {"error": "asking a workspace for a period needs the person this run is for"}
    from robothor.engine.tools.handlers.room_runs import nested_refusal

    if refusal := await nested_refusal(ctx):
        return refusal
    actor = Actor(user_id=ctx.user_id, role=ctx.user_role, tenant_id=ctx.tenant_id)
    try:
        edition, started = await periods.request_period(actor, room_id, grain, args.get("anchor"))
    except RoomNotFound:
        return {"error": "workspace not found"}
    except RoomForbidden as exc:
        return {"error": str(exc)}
    except RunRefused as exc:
        return {"error": str(exc), "refused": exc.reason}
    except (periods.RoomRunUnavailable, ValueError) as exc:
        return {"error": str(exc)}
    return {
        "edition": edition_json(edition),
        "started": started,
        "link": room_link(edition.room_id),
    }


HANDLERS["room_request_period"] = _room_request_period
