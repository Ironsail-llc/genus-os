"""The event bus as a named channel.

``DeliveryMode.LOG`` already publishes to the Redis event bus, but only as a
*mode* — an agent whose output should be announced somewhere and also recorded
had no way to name the bus as its destination. Registering it as a channel makes
``delivery.channel: event_bus`` mean what it reads like, using the same
publisher and the same statuses.

``webchat`` is now its own module (``channels/webchat.py``) rather than the
absence this note used to record: a member's Helm session and inbox are a real
destination with two rows to write, which is nothing like a stream publish. What
stays true here is the distinction that made the bus a channel in the first
place — this one is a SINK, so :meth:`EventBusChannel.ask` raises permanently,
while webchat's waits on a durable row.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robothor.engine.channels.base import SendReceipt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from robothor.engine.models import AgentConfig, AgentRun

logger = logging.getLogger(__name__)

__all__ = ["EventBusChannel"]


class EventBusChannel:
    """Publish to the Redis event bus, reachable as ``delivery.channel: event_bus``."""

    name = "event_bus"
    inbound_router: Any | None = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        try:
            from robothor.events import bus

            return {"channel": self.name, "enabled": bool(bus.EVENT_BUS_ENABLED)}
        except Exception as exc:  # noqa: BLE001
            return {"channel": self.name, "enabled": False, "error": str(exc)}

    async def send(
        self,
        target: str,
        text: str,
        *,
        config: AgentConfig | None = None,
        run: AgentRun | None = None,
        **kw: Any,
    ) -> SendReceipt:
        """Publish ``text``, reporting the publisher's own verdict.

        ``bus.publish`` returns a stream message id or ``None`` and never
        raises, so ``_deliver_event_bus`` already derives its status from that
        id. This wrapper reuses it rather than publishing a second way.

        ``post_delivery`` is False: there is no per-recipient message here for a
        reply to resolve against, so recording one in the channel bus would
        invent a conversation turn that never happened.
        """
        if config is None or run is None:
            return SendReceipt(
                acknowledged=0,
                expected=1,
                status="failed:event_bus_no_run",
                target=target,
                post_delivery=False,
            )

        from robothor.engine import delivery

        before = run.delivery_status
        ok = bool(await delivery._deliver_event_bus(config, text, run))
        after = run.delivery_status
        return SendReceipt(
            acknowledged=1 if ok else 0,
            expected=1,
            status=after if after != before else None,
            target=target,
            body=text,
            post_delivery=False,
        )

    async def ask(
        self,
        question: str,
        options: Sequence[str] = (),
        *,
        timeout: float = 300.0,
        target: str = "",
        addressee: str = "",
    ) -> str | None:
        """Not implemented, and never will be: a bus has nobody to ask.

        Raising rather than returning ``None`` is deliberate even though both
        mean "no answer". ``None`` says *the person did not reply*; this says
        *there is no person*, and the caller writes the two down differently.
        """
        raise NotImplementedError("the event bus is a sink; it cannot ask a question")

    async def resolve_identity(self, native_id: str) -> Any:
        """Not implemented: the bus carries no sender identity."""
        raise NotImplementedError("the event bus carries no sender identity to resolve")
