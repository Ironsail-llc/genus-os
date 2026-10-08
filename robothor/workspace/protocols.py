"""What a mail or calendar provider must do, and the bundle the tools receive.

The ``gws_*`` handlers keep every guard (do-not-contact, no-auto scheduling,
dedup, duplicate-reply, reply-all assembly, CRM write-through, benchmark
refusal) and call a provider only for TRANSPORT. So the protocols are thin and
the data is Google-shaped (see :mod:`robothor.workspace.types`):

* A message or thread a provider returns is its RAW form; the handler turns it
  into a tool result with the provider's own ``shape_envelope`` /
  ``shape_message``. A thread fetched with ``fmt="metadata"`` carries Gmail's
  ``payload.headers`` list (``[{"name", "value"}]``), because the shared
  duplicate-reply and reply-all logic reads those headers directly.
* Sends take an RFC 5322 message, base64url-encoded, built by the handler --
  the recipients the do-not-contact screen approved are exactly the ones in it.
* Failures come back as ``{"error": ..., "hint": ...}`` dicts (the gws CLI's
  contract, which every handler already checks), or are raised as
  :class:`~robothor.workspace.errors.WorkspaceError`, which
  :func:`robothor.workspace.bridge.blocking` turns into the same dict.

The protocols are async. The handlers run in a worker thread (they always
have: the gws CLI is a subprocess), so they call a provider through
:func:`robothor.workspace.bridge.blocking`, which uses a provider's synchronous
twin when it has one (Google) and the engine's event loop when it does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from robothor.workspace.types import (
        CalendarRef,
        Capabilities,
        EventQuery,
        MailQuery,
    )

__all__ = [
    "BlockingCalendar",
    "BlockingMail",
    "CalendarProvider",
    "CalendarSession",
    "MailProvider",
    "Workspace",
]


@runtime_checkable
class MailProvider(Protocol):
    """Mail transport. Every method returns the provider's dict unchanged."""

    async def search(self, query: MailQuery, *, max_results: int) -> dict[str, Any]:
        """``{"messages": [{"id", "threadId"}, ...]}`` for at most ``max_results``."""
        ...

    async def get_message(self, message_id: str, *, fmt: str) -> dict[str, Any]:
        """One raw message; ``fmt`` is ``full``, ``metadata`` or ``minimal``."""
        ...

    async def get_thread(self, thread_id: str, *, fmt: str) -> dict[str, Any]:
        """``{"id", "messages": [raw message, ...]}``, oldest first."""
        ...

    async def send(self, raw: str, *, thread_id: str | None = None) -> dict[str, Any]:
        """Send a new message (in ``thread_id`` when given). Returns a SendResult."""
        ...

    async def reply(self, raw: str, *, thread_id: str) -> dict[str, Any]:
        """Send a reply in ``thread_id``. Returns a SendResult."""
        ...

    async def modify(
        self, message_id: str, *, add_labels: Any, remove_labels: Any
    ) -> dict[str, Any]: ...

    def shape_envelope(
        self, raw: dict[str, Any], *, max_header_chars: int | None = None
    ) -> dict[str, Any]:
        """A raw message as a :data:`~robothor.workspace.types.MailEnvelope`."""
        ...

    def shape_message(self, raw: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
        """A raw message as a :data:`~robothor.workspace.types.MailMessage`."""
        ...


@runtime_checkable
class CalendarProvider(Protocol):
    """Calendar transport. Events are :class:`NormalizedEvent` dicts."""

    capabilities: Capabilities

    def resolve(self, kind: Literal["own", "operator"], *, address: str = "") -> CalendarRef:
        """The assistant's own calendar, or the operator's (``address``)."""
        ...

    async def list(self, ref: CalendarRef, query: EventQuery) -> dict[str, Any]:
        """``{"items": [event, ...]}``."""
        ...

    async def get(self, ref: CalendarRef, event_id: str) -> dict[str, Any]: ...

    async def create(
        self,
        ref: CalendarRef,
        event: dict[str, Any],
        *,
        conference: bool,
        send_updates: str | None,
    ) -> dict[str, Any]:
        """Insert. ``send_updates=None`` means no attendees: send nothing, say nothing."""
        ...

    async def conditional_patch(
        self,
        ref: CalendarRef,
        event_id: str,
        body: dict[str, Any],
        *,
        etag: str,
        send_updates: str,
    ) -> dict[str, Any]:
        """One write, only if the event is still at ``etag``. 412 -> ``status_code: 412``."""
        ...

    async def respond(
        self,
        ref: CalendarRef,
        event_id: str,
        response: str,
        *,
        screen: Callable[..., dict[str, Any] | None],
        comment: str | None = None,
        send_updates: str = "all",
        cancelled: Any = None,
    ) -> dict[str, Any]:
        """RSVP as the calendar's owner, the organiser screened first."""
        ...

    async def delete(
        self, ref: CalendarRef, event_id: str, *, send_updates: str
    ) -> dict[str, Any]: ...


class CalendarSession(Protocol):
    """One authenticated connection for a read / write / read-back sequence."""

    def get(self, ref: CalendarRef, event_id: str) -> dict[str, Any]: ...

    def conditional_patch(
        self,
        ref: CalendarRef,
        event_id: str,
        body: dict[str, Any],
        *,
        etag: str,
        send_updates: str,
    ) -> dict[str, Any]: ...


class BlockingMail(Protocol):
    """:class:`MailProvider`'s methods, synchronous, for a worker thread."""

    def search(self, query: MailQuery, *, max_results: int) -> dict[str, Any]: ...
    def get_message(self, message_id: str, *, fmt: str) -> dict[str, Any]: ...
    def get_thread(self, thread_id: str, *, fmt: str) -> dict[str, Any]: ...
    def send(self, raw: str, *, thread_id: str | None = None) -> dict[str, Any]: ...
    def reply(self, raw: str, *, thread_id: str) -> dict[str, Any]: ...
    def modify(self, message_id: str, *, add_labels: Any, remove_labels: Any) -> dict[str, Any]: ...
    def shape_envelope(
        self, raw: dict[str, Any], *, max_header_chars: int | None = None
    ) -> dict[str, Any]: ...
    def shape_message(self, raw: dict[str, Any], *, max_chars: int) -> dict[str, Any]: ...


class BlockingCalendar(Protocol):
    """:class:`CalendarProvider`'s methods, synchronous, plus a session."""

    def resolve(self, kind: Literal["own", "operator"], *, address: str = "") -> CalendarRef: ...
    def list(self, ref: CalendarRef, query: EventQuery) -> dict[str, Any]: ...
    def create(
        self,
        ref: CalendarRef,
        event: dict[str, Any],
        *,
        conference: bool,
        send_updates: str | None,
    ) -> dict[str, Any]: ...
    def delete(self, ref: CalendarRef, event_id: str, *, send_updates: str) -> dict[str, Any]: ...
    def session(self) -> AbstractContextManager[CalendarSession]: ...


@dataclass(frozen=True)
class Workspace:
    """The providers one tenant's ``gws_*`` tools use."""

    provider: str
    mail: MailProvider
    calendar: CalendarProvider
    capabilities: Capabilities
