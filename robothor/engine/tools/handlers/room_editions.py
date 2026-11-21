"""``room_publish_edition``: an agent publishes an edition or dashboard into a room.

The whole publish runs in one worker thread (``robothor.rooms.editions``):
access, validation, provenance, storage, the ``room.edition`` event and the
published hooks are all blocking calls. Which room the run is in, and whether
it is a preview, come from ``robothor.rooms.run_context``, never arguments.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robothor.engine.tools.dispatch import ToolContext

HANDLERS: dict[str, Any] = {}


async def _room_publish_edition(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    from robothor.rooms import editions
    from robothor.rooms.run_context import current_room_run

    result: dict[str, Any] = await asyncio.to_thread(
        editions.publish_from_run,
        args,
        tenant_id=ctx.tenant_id,
        agent_id=ctx.agent_id,
        run_id=ctx.run_id,
        user_id=ctx.user_id,
        user_role=ctx.user_role,
        run=current_room_run(),
    )
    return result


HANDLERS["room_publish_edition"] = _room_publish_edition
