"""A workspace as a place a scheduled agent's output goes (``delivery.channel: room``).

A manifest agent with ``delivery.channel: room`` and ``delivery.to: <room id>``
delivers its output into that workspace: an assistant turn in the room's
conversation, attributed to the agent (``author_agent_id``, its display name
and the run it came from), the room's activity mark moved and a
``room.message`` event with ids only, and then a short note with the link to
each member who has not muted the room (``fanout.fan_out_note``). The members
open the workspace through its access check to read the turn; the note never
carries the full text.

What "delivered" means: the turn was written (``acknowledged`` 1 of 1). A
target that is not a workspace of this run's tenant is
``failed:room_unknown_target``, an archived one ``failed:room_archived``, and
a turn the store did not write ``failed:room_no_session_write``; nothing else
is written in any of those cases. The delivery waits for the room's turn lock,
so it never lands in the middle of somebody's conversation turn.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from robothor.engine.channels.base import SendReceipt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from robothor.engine.models import AgentConfig, AgentRun

logger = logging.getLogger(__name__)

__all__ = ["RoomChannel"]


def _tenant(run: Any) -> str:
    from robothor.engine.channels.webchat import _tenant_id

    return str(getattr(run, "tenant_id", "") or _tenant_id())


def _failed(target: str, status: str) -> SendReceipt:
    return SendReceipt(acknowledged=0, expected=1, status=status, target=target)


class RoomChannel:
    """A workspace's conversation, reachable as ``delivery.channel: room``."""

    name = "room"
    inbound_router: Any | None = None

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def health(self) -> dict[str, Any]:
        return {"channel": self.name, "configured": True, "ok": True}

    async def send_attachment(
        self, target: str, path: str, caption: str = "", *, as_: str = "auto", **kw: Any
    ) -> Any:
        raise NotImplementedError("a workspace takes text deliveries; publish files elsewhere")

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        timeout: float = 300.0,
        target: str = "",
        addressee: str = "",
    ) -> str | None:
        """Not implemented: a workspace is shared, so nobody in it is THE one to ask."""
        raise NotImplementedError("a workspace has nobody to ask on its own; ask a person")

    async def resolve_identity(self, native_id: str) -> Any:
        """Not implemented: a workspace delivery carries no sender identity."""
        raise NotImplementedError("a workspace carries no sender identity to resolve")

    async def send(
        self,
        target: str,
        text: str,
        *,
        config: AgentConfig | None = None,
        run: AgentRun | None = None,
        **kw: Any,
    ) -> SendReceipt:
        """Deliver ``text`` into the workspace ``target`` names."""
        from robothor.engine import chat_rooms, chat_store
        from robothor.rooms import dal, fanout
        from robothor.rooms.keys import canonical_uuid

        room_id = canonical_uuid((target or "").strip())
        tenant_id = _tenant(run)
        room = await asyncio.to_thread(dal.get_room, tenant_id, room_id) if room_id else None
        if room is None:
            logger.error("Room delivery names no workspace of this tenant")
            return _failed(target or "", "failed:room_unknown_target")
        if room.archived:
            return _failed(room.id, "failed:room_archived")
        agent_id = str(getattr(config, "id", "") or "")
        name = str(getattr(config, "name", "") or agent_id)
        async with fanout.room_lock(room.id):
            message_id = await chat_store.save_channel_surface_async(
                room.session_key,
                text,
                agent_id,
                author_display_name=name,
                surfaced_from_run_id=getattr(run, "id", None),
                channel=self.name,
                tenant_id=room.tenant_id,
            )
            if message_id is None:
                return _failed(room.id, "failed:room_no_session_write")

            async def done() -> list[int]:
                return [int(message_id)]

            await chat_rooms.saved(chat_rooms.ChatTarget(room.session_key, room), done())
        note = fanout.note_text(room, name or room.title, text)
        try:
            await fanout.fan_out_note(
                room, note, metadata={"room_id": room.id, "chat_message_id": str(message_id)}
            )
        except Exception as exc:  # noqa: BLE001 - the turn is delivered; notes are extra
            logger.warning("workspace delivery notes not sent: %s", type(exc).__name__)
        return SendReceipt(
            acknowledged=1, expected=1, platform_ids=[str(message_id)], target=room.id, body=text
        )
