"""The provider-neutral half of ingestion: what an ingestor is, and how it publishes.

An :class:`Ingestor` turns one provider resource (a mailbox's inbox, a
calendar) into events on the platform bus, once per change, in rounds:
:meth:`Ingestor.run_once` reads what changed since its saved position,
publishes, and saves the new position. Where it left off and what it already
published live in an :class:`~robothor.workspace.ingest.state.IngestStore`.

Events go out through a :data:`Publisher`, which by default is
:func:`robothor.events.bus.publish` -- the same bus, streams and event names
the Google sync scripts use (:mod:`robothor.events.contract`).
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from robothor.workspace.errors import WorkspaceError

__all__ = [
    "SOURCE",
    "IngestReport",
    "Ingestor",
    "PublishFailed",
    "Publisher",
    "bus_publisher",
]

#: The ``source`` field of every event an ingestor publishes.
SOURCE = "workspace_ingest"

#: ``await publish(stream, event_type, payload)``. Raises when the event was not sent.
Publisher = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class PublishFailed(WorkspaceError):  # noqa: N818 - domain vocabulary
    """The bus did not take the event. The ingestor stops the round and retries it."""


@dataclass
class IngestReport:
    """What one round did. Counts only: never a subject, address or id."""

    resource: str
    mode: str = "incremental"  # "initial" | "incremental" | "resync"
    read: int = 0
    published: int = 0
    #: Unseen items left unpublished as history: below the high-water mark
    #: after a reset (the replay a lost delta would otherwise cause), or in an
    #: ordinary round older than the seen set's retention.
    held_back: int = 0
    logged: int = 0
    #: New messages logged to the CRM as an interaction this round.
    crm_logged: int = 0


class Ingestor(abc.ABC):
    """One provider resource, ingested in rounds."""

    provider: str
    resource: str
    mailbox: str

    @abc.abstractmethod
    async def run_once(self) -> IngestReport:
        """Read what changed, publish what is new, save the position."""


def bus_publisher(tenant_id: str, *, source: str = SOURCE) -> Publisher:
    """A :data:`Publisher` over the platform event bus, for platform tenant ``tenant_id``.

    The bus never raises and answers ``None`` when an event did not land;
    that becomes :class:`PublishFailed` here, so the ingestor does not record
    as published an event no consumer will ever see. With the bus switched
    off (``EVENT_BUS_ENABLED=false``) nothing is published and nothing fails.
    """

    async def publish(stream: str, event_type: str, payload: dict[str, Any]) -> None:
        from robothor.events import bus

        if not bus.EVENT_BUS_ENABLED:
            return
        message_id = await asyncio.to_thread(
            bus.publish, stream, event_type, payload, source=source, tenant_id=tenant_id
        )
        if message_id is None:
            raise PublishFailed(f"event bus did not accept {event_type}")

    return publish
