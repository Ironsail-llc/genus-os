"""``room_run``: run a report workspace now, or preview it.

A wrapper over ``robothor.rooms.runs.start_room_run``, the same function the
engine's ``POST /api/rooms/{id}/run`` calls. The caller is the verified human
the run is for (``rooms._actor``), never anything in the arguments; a run with
no person behind it is refused. The run goes on in the background: the result
is the edition being built and the workspace link, and a run already in
progress is joined rather than started twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from robothor.rooms import runs
from robothor.rooms.errors import RoomForbidden, RoomNotFound
from robothor.rooms.models import edition_json, room_link

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}

_IN_A_RUN = (
    "A workspace run cannot start workspace runs or request periods: a run that starts "
    "runs is a loop. Say in your result what should run next; a person can start it."
)
_IN_A_SUB_AGENT = (
    "A sub-agent cannot start workspace runs or request periods; return to the run that "
    "spawned you, which can ask the person."
)


async def nested_refusal(ctx: ToolContext) -> dict[str, Any] | None:
    """A refusal when the calling run is a workspace run (its room carrier is
    unattended: scheduled, on demand, period or preview) or a sub-agent run;
    None for a person's own turn. Structural, not a count: such a run starts
    no workspace work at all."""
    import asyncio

    from robothor.rooms import editions
    from robothor.rooms.run_context import current_room_run

    carrier = current_room_run()
    if carrier is not None and carrier.unattended:
        return {"error": _IN_A_RUN, "nested": True}
    if ctx.run_id:
        record = await asyncio.to_thread(editions.load_run_record, ctx.tenant_id, ctx.run_id)
        if record is not None and record.is_child:
            return {"error": _IN_A_SUB_AGENT, "nested": True}
    return None


_NOT_FOUND = (
    "No workspace with that id is visible to you. Use room_list to find the workspaces "
    "you can open; never guess an id or pass a title."
)


async def _room_run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.engine.tools.handlers.rooms import _actor

    room_id = args.get("room_id")
    if not isinstance(room_id, str) or not room_id:
        return {"error": "room_id is required"}
    if refusal := await nested_refusal(ctx):
        return refusal
    actor = _actor(ctx)
    if actor is None:
        return {"error": "running a workspace needs the verified person this run is for"}
    preview = args.get("preview", False)
    if not isinstance(preview, bool):
        return {"error": "preview must be true or false"}
    try:
        edition, joined = await runs.start_room_run(
            actor, room_id, preview=preview, period=args.get("period")
        )
    except RoomNotFound:
        return {"error": _NOT_FOUND, "not_found": True}
    except RoomForbidden as exc:
        return {"error": str(exc)}
    except (runs.RunRefused, ValueError) as exc:
        return {"error": str(exc)}
    out: dict[str, Any] = {
        "edition": edition_json(edition),
        "started": not joined,
        "joined": joined,
        "link": room_link(edition.room_id),
    }
    if edition.preview:
        out["note"] = "a preview: only you will see it, and nobody is notified"
    return out


HANDLERS["room_run"] = _room_run
