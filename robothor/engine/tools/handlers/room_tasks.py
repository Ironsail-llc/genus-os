"""Task tools in workspaces: ``room_task_accept``, ``room_task_dismiss``, ``room_open_task``.

Thin wrappers over ``robothor.rooms.tasks``, where every rule lives; the
bridge's task routes call the same functions. Each call runs in a worker
thread. The caller is always the human on the run (``rooms._actor``); a run
with no verified person behind it cannot use these tools, nor can a
sub-agent's run (``tasks.run_is_top_level`` on the ``agent_runs`` row).

Importing this module (dispatch imports every handler module) registers the
suggestion hook on the edition publish extension point, so a published
edition's suggestions become suggested tasks in the engine that published it.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from robothor.engine.tools.handlers.rooms import _NO_PERSON, _NOT_FOUND, _actor, _room
from robothor.rooms import editions
from robothor.rooms import tasks as room_tasks
from robothor.rooms.errors import (
    RoomForbidden,
    RoomNotFound,
    TaskAlreadyAccepted,
    TaskAlreadyDismissed,
    TaskNotFound,
)
from robothor.rooms.models import room_link

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.rooms.tasks import RoomTaskView

HANDLERS: dict[str, Any] = {}

editions.register_edition_published(room_tasks.on_edition_published)

_NO_TASK = {
    "error": (
        "No such task in this workspace. Use room_read to see the workspace's tasks; "
        "never guess an id."
    ),
    "not_found": True,
}


def _view(view: RoomTaskView) -> dict[str, Any]:
    return asdict(view)


def _refusal(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, RoomNotFound):
        return dict(_NOT_FOUND)
    if isinstance(exc, TaskNotFound):
        return dict(_NO_TASK)
    if isinstance(exc, TaskAlreadyAccepted):
        name = getattr(exc.by, "display_name", "") or "someone else"
        return {
            "error": f"{name} has already accepted this task; it stays theirs.",
            "already_accepted": True,
            "accepted_by": asdict(exc.by) if hasattr(exc.by, "user_id") else None,
        }
    if isinstance(exc, TaskAlreadyDismissed):
        return {"error": "This suggestion was dismissed.", "already_dismissed": True}
    return {"error": str(exc)}


_SUB_AGENT = {
    "error": (
        "Task tools act for the person in their own conversation; this run is a "
        "sub-agent's (or unknown), so it cannot take, dismiss or open a task."
    )
}


async def _own_run(ctx: ToolContext) -> bool:
    return bool(await asyncio.to_thread(room_tasks.run_is_top_level, ctx.tenant_id, ctx.run_id))


def _ids(args: dict[str, Any], *names: str) -> tuple[str, ...] | None:
    values = tuple(str(args.get(n) or "").strip() for n in names)
    return values if all(values) else None


async def _decide(fn: Any, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    ids = _ids(args, "room_id", "task_id")
    if ids is None:
        return {"error": "room_id and task_id are required"}
    room_id, task_id = ids
    if not await _own_run(ctx):
        return dict(_SUB_AGENT)
    try:
        view = await asyncio.to_thread(fn, actor, room_id, task_id)
    except (
        RoomNotFound,
        RoomForbidden,
        TaskNotFound,
        TaskAlreadyAccepted,
        TaskAlreadyDismissed,
    ) as exc:
        return _refusal(exc)
    return {"task": _view(view), "link": room_link(room_id)}


async def _room_task_accept(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _decide(room_tasks.accept_task, args, ctx)


async def _room_task_dismiss(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _decide(room_tasks.dismiss_task, args, ctx)


async def _room_open_task(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    actor = _actor(ctx)
    if actor is None:
        return dict(_NO_PERSON)
    ids = _ids(args, "task_id")
    if ids is None:
        return {"error": "task_id is required"}
    if not await _own_run(ctx):
        return dict(_SUB_AGENT)
    try:
        room = await asyncio.to_thread(room_tasks.open_task_room, actor, ids[0])
    except (RoomNotFound, TaskNotFound):
        return {
            "error": (
                "No task with that id is visible to you, or its workspace is not yours to "
                "join. Ask someone in that workspace to share it with you."
            ),
            "not_found": True,
        }
    except RoomForbidden as exc:
        return {"error": str(exc)}
    out = _room(room)
    out["task_id"] = room.task_id
    return {"room": out, "link": room_link(room.id)}


HANDLERS["room_task_accept"] = _room_task_accept
HANDLERS["room_task_dismiss"] = _room_task_dismiss
HANDLERS["room_open_task"] = _room_open_task
