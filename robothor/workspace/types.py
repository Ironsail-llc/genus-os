"""The shapes every workspace provider speaks, named after what Google already returns.

The ``gws_*`` handlers were written against the Gmail and Calendar v3 APIs, and
their guards read Google's field names (``threadId``, ``responseStatus``,
``attendeesOmitted``...). So the neutral types ARE those names: the Google
adapter passes data through untouched, and a second provider (Microsoft 365)
translates into them. Nothing here changes what a Google call returns.

* :data:`MailEnvelope` / :data:`MailMessage` -- exactly the keys the gws shapers
  (:mod:`robothor.workspace.google.gmail_parse`) produce for a tool result.
* :class:`SendResult` -- what a send or reply returns.
* :class:`NormalizedEvent` -- the Calendar v3 subset the calendar code reads.
* :class:`CalendarRef` -- WHICH calendar, replacing the literal ``"primary"``.
* :class:`MailQuery` -- a parsed Gmail search; see :mod:`robothor.workspace.query`.
* :class:`Capabilities` -- what a provider can express, so a handler can refuse
  instead of silently widening.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

__all__ = [
    "GOOGLE_CAPABILITIES",
    "AllOf",
    "AnyOf",
    "Attachment",
    "CalendarKind",
    "CalendarRef",
    "Capabilities",
    "EventAttendee",
    "EventOrganizer",
    "EventQuery",
    "EventTime",
    "MailEnvelope",
    "MailMessage",
    "MailQuery",
    "NormalizedEvent",
    "QueryNode",
    "SendResult",
    "Term",
    "as_calendar_ref",
]


# ── mail ──────────────────────────────────────────────────────────────


class Attachment(TypedDict):
    filename: str
    mime_type: str
    size_bytes: int


#: The described-but-unread form of one message (``_shape_envelope``).
MailEnvelope = TypedDict(
    "MailEnvelope",
    {
        "id": str,
        "thread_id": str,
        "date": str,
        "from": str,
        "to": str,
        "subject": str,
        "snippet": str,
        "labels": list[str],
    },
)

#: One message, whole (``_shape_message``): the envelope plus body and extras.
MailMessage = TypedDict(
    "MailMessage",
    {
        "id": str,
        "thread_id": str,
        "date": str,
        "from": str,
        "to": str,
        "subject": str,
        "snippet": str,
        "labels": list[str],
        "cc": str,
        "message_id": str,
        "body_chars": int,
        "body_text": str,
        "body_truncated": bool,
        "attachments": NotRequired[list[Attachment]],
    },
)


class SendResult(TypedDict, total=False):
    """A sent message. ``id`` is what verification reads back by."""

    id: str
    threadId: str  # noqa: N815 - Google's field name, read by the guards
    labelIds: list[str]  # noqa: N815
    #: RFC 5322 Message-ID, when the provider returns one (Graph does).
    internetMessageId: str  # noqa: N815


# ── mail query ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Term:
    """One search term. ``field`` is an operator name, ``text`` or ``phrase``."""

    field: str
    value: str
    negated: bool = False


@dataclass(frozen=True)
class AnyOf:
    """``a OR b`` / ``{a b}``: any one of the parts matches."""

    parts: tuple[QueryNode, ...]


@dataclass(frozen=True)
class AllOf:
    """``(a b)``: every part matches."""

    parts: tuple[QueryNode, ...]


QueryNode = Term | AnyOf | AllOf


@dataclass(frozen=True)
class MailQuery:
    """A Gmail search, parsed. ``original`` is what the caller wrote, verbatim.

    ``clauses`` are ANDed. ``raw`` keeps every token the parser did not
    understand (an unknown operator, an unbalanced group), so nothing is ever
    silently dropped: Google receives ``original`` unchanged, and a provider
    that cannot express ``raw`` must refuse the search rather than widen it.
    """

    original: str
    clauses: tuple[QueryNode, ...] = ()
    raw: tuple[str, ...] = ()

    @property
    def is_structured(self) -> bool:
        """Whether every part of the query was understood."""
        return not self.raw


# ── calendar ──────────────────────────────────────────────────────────

CalendarKind = Literal["own", "operator", "other"]
_KINDS = ("own", "operator", "other")


@dataclass(frozen=True)
class CalendarRef:
    """Which calendar a call targets, and whose it is.

    ``calendar_id`` is what the provider addresses (``primary`` for the
    assistant's own Google calendar, the operator's address for theirs);
    ``kind`` is the honest half every result reports; ``mailbox`` is the owning
    mailbox when it is known (Microsoft 365 addresses calendars by mailbox).
    """

    calendar_id: str
    kind: CalendarKind
    mailbox: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"calendar kind must be one of {_KINDS}, not {self.kind!r}")


def as_calendar_ref(calendar: CalendarRef | str) -> CalendarRef:
    """A ref for a bare calendar id (callers that predate :class:`CalendarRef`)."""
    if isinstance(calendar, CalendarRef):
        return calendar
    return CalendarRef(str(calendar), "other")


class EventTime(TypedDict, total=False):
    dateTime: str | None  # noqa: N815
    date: str | None
    timeZone: str  # noqa: N815


class EventAttendee(TypedDict, total=False):
    email: str
    responseStatus: str
    optional: bool
    organizer: bool
    self: bool
    comment: str


class EventOrganizer(TypedDict, total=False):
    email: str
    self: bool


class NormalizedEvent(TypedDict, total=False):
    """The Calendar v3 subset the calendar code reads and writes.

    ``provider_extra`` carries anything provider-specific a later write needs
    (a Microsoft change key, say) without inventing a Google field for it.
    """

    id: str
    etag: str
    status: str
    summary: str
    description: str
    location: str
    start: EventTime
    end: EventTime
    attendees: list[EventAttendee]
    organizer: EventOrganizer
    recurrence: list[str]
    recurringEventId: str  # noqa: N815
    htmlLink: str  # noqa: N815
    hangoutLink: str  # noqa: N815
    conferenceData: dict[str, Any]  # noqa: N815
    attendeesOmitted: bool  # noqa: N815
    provider_extra: dict[str, Any]


class EventQuery(TypedDict, total=False):
    """A calendar listing. Key ORDER is kept by the Google adapter, so the CLI
    argv it builds is byte-identical to what the handler built inline."""

    time_min: str
    time_max: str
    single_events: bool
    order_by: str
    max_results: int


# ── capabilities ──────────────────────────────────────────────────────


class Capabilities(TypedDict):
    provider: str
    #: The ``sendUpdates`` values the provider can honour exactly.
    send_updates_modes: tuple[str, ...]
    #: ``gmail_labels`` (arbitrary label ids) or ``categories`` (Outlook).
    labels: Literal["gmail_labels", "categories"]
    #: The online meeting a create can attach.
    online_meeting: Literal["hangouts_meet", "teams_meeting", "none"]


GOOGLE_CAPABILITIES: Capabilities = {
    "provider": "google",
    "send_updates_modes": ("all", "externalOnly", "none"),
    "labels": "gmail_labels",
    "online_meeting": "hangouts_meet",
}
