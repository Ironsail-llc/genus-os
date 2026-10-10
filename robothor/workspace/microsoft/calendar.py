"""Exchange Online calendars behind the workspace ``CalendarProvider`` protocol.

Every guard (do-not-contact, no-auto scheduling, dedup, calendar identity,
cancellation, the read / merge / conditional write / read-back loop) runs in
the ``gws_*`` handlers and :mod:`robothor.engine.calendar_attendees` before
anything here is called. This module only translates:

* **Reading.** Graph events become :class:`~robothor.workspace.types.NormalizedEvent`
  dicts shaped exactly like Google Calendar v3 events (``responseStatus``
  values, ``organizer.self``, ``start.dateTime`` with an offset plus an IANA
  ``timeZone``, ``date`` for all-day, ``RRULE`` recurrence, ``etag``), because
  that is what the guards, the dedup and the CRM write-through read. Listing
  uses ``calendarView``, which expands series into occurrences the way
  ``singleEvents=true`` does.
* **Writing.** A Google-shaped create or patch body becomes Graph's. Anything
  Exchange cannot express faithfully raises
  :class:`~robothor.workspace.errors.Unsupported` BEFORE any request is sent:
  a recurrence rule outside the supported subset, a Google Meet link, or a
  ``send_updates`` other than ``all`` on a meeting with attendees -- Exchange
  always notifies attendees of an organiser's writes and has no quiet mode.
* **Conditional edits.** ``conditional_patch`` sends ``If-Match: <etag>``;
  Graph's 412 surfaces as :class:`~robothor.workspace.errors.PreconditionFailed`
  (``status_code: 412`` through the bridge) so the shared retry loop re-reads
  and merges once.
* **RSVP** is a Graph action (``accept`` / ``decline`` / ``tentativelyAccept``),
  not an attendee-list PATCH: :meth:`GraphCalendar.rsvp`, used by
  ``calendar_attendees.respond``.
* **Delete** of a meeting this mailbox organises, with attendees, is
  ``/cancel`` (which notifies them); anything else is a plain ``DELETE``.

Times: requests carry ``Prefer: outlook.timezone="UTC"`` (the Graph client's
default), so Graph answers in UTC; each event is rendered back in its own zone
(``originalStartTimeZone``, a Windows name mapped to IANA) so the agent reads
local times. Writes send IANA zone names, which Graph accepts; a tenant that
rejects one gets the CLDR Windows name instead, once.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from robothor.workspace.errors import Unsupported, WorkspaceError
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.microsoft.timezones import to_iana, to_windows
from robothor.workspace.types import MICROSOFT365_CAPABILITIES, CalendarRef, Capabilities

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from robothor.workspace.types import EventQuery

__all__ = [
    "GraphCalendar",
    "event_to_graph",
    "graph_recurrence_to_rrule",
    "patch_to_graph",
    "rrule_to_graph",
    "to_normalized",
]

#: Read bodies as plain text: the description the agent sees and verifies.
_TEXT_PREFER = 'outlook.body-content-type="text"'

#: Graph ``responseType`` -> Calendar v3 ``responseStatus``.
_RESPONSE_IN = {
    "none": "needsAction",
    "notResponded": "needsAction",
    "tentativelyAccepted": "tentative",
    "accepted": "accepted",
    "declined": "declined",
    "organizer": "accepted",
}

#: Calendar v3 RSVP -> the Graph action that sends it.
_RSVP_ACTIONS = {"accepted": "accept", "declined": "decline", "tentative": "tentativelyAccept"}

_DAY_NAMES = {
    "MO": "monday",
    "TU": "tuesday",
    "WE": "wednesday",
    "TH": "thursday",
    "FR": "friday",
    "SA": "saturday",
    "SU": "sunday",
}
_DAY_CODES = {name: code for code, name in _DAY_NAMES.items()}
_WEEKDAY_ORDER = tuple(_DAY_NAMES.values())
_INDEX_NAMES = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}
_INDEX_NUMBERS = {name: number for number, name in _INDEX_NAMES.items()}
_FREQ = {
    "daily": "DAILY",
    "weekly": "WEEKLY",
    "absoluteMonthly": "MONTHLY",
    "relativeMonthly": "MONTHLY",
    "absoluteYearly": "YEARLY",
    "relativeYearly": "YEARLY",
}
#: The RRULE parts this provider translates; anything else is refused.
SUPPORTED_RRULE_PARTS = frozenset({"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL"})

_FRACTION = re.compile(r"(\.\d{1,6})\d*")
_BYDAY = re.compile(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)")
_UNTIL = re.compile(r"(\d{4})(\d{2})(\d{2})(?:T\d{6}Z?)?")
_EMAIL = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")
_TAG = re.compile(r"<[^>]+>")

#: Keys a create body may carry. Anything else is refused, never dropped.
_CREATE_KEYS = frozenset(
    {"summary", "description", "location", "start", "end", "attendees", "conferenceData"}
    | {"recurrence", "hangoutLink"}
)


def _always_notifies(mode: Any) -> str:
    return (
        "Exchange Online always notifies a meeting's attendees when its organiser creates, "
        f"changes or cancels it, so send_updates={mode!r} cannot be honoured. Nothing was "
        "written. Set calendar_send_updates to 'all' to let the assistant manage meetings "
        "with attendees on Microsoft 365."
    )


# ── times ─────────────────────────────────────────────────────────────


def _parse(value: Any) -> datetime | None:
    """An ISO 8601 date-time; Graph's seven fractional digits are cut to six."""
    text = _FRACTION.sub(r"\1", str(value or "").strip())
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None


def _event_time(value: Any, original_zone: Any, *, all_day: bool) -> dict[str, Any]:
    """One Graph ``dateTimeTimeZone`` as a Calendar v3 time."""
    if not isinstance(value, dict):
        return {}
    moment = _parse(value.get("dateTime"))
    if moment is None:
        return {}
    labelled = to_iana(value.get("timeZone")) or "UTC"
    zone = to_iana(original_zone)
    if all_day:
        # All-day events float: Graph reports them at midnight whatever zone
        # it labels them with. Take the date as written; only a non-midnight
        # value (converted to the requested zone) is shifted back first.
        if (moment.tzinfo is None and moment.time() == time(0)) or not zone:
            return {"date": moment.date().isoformat()}
        aware = moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo(labelled))
        return {"date": aware.astimezone(ZoneInfo(zone)).date().isoformat()}
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(labelled))
    shown = zone or labelled
    return {"dateTime": moment.astimezone(ZoneInfo(shown)).isoformat(), "timeZone": shown}


def _write_time(value: Any, default_zone: str) -> tuple[dict[str, Any], bool]:
    """A Calendar v3 time as Graph's ``dateTimeTimeZone``, and whether it is all-day."""
    if not isinstance(value, dict):
        raise Unsupported("an event time must be an object with dateTime or date")
    named = value.get("timeZone")
    zone = to_iana(named) if named else default_zone
    if zone is None:
        raise Unsupported(f"unknown time zone {named!r}; use an IANA name such as Europe/Berlin")
    if value.get("date"):
        try:
            day = date.fromisoformat(str(value["date"]))
        except ValueError:
            raise Unsupported(f"cannot read the date {value['date']!r}") from None
        return {"dateTime": f"{day.isoformat()}T00:00:00", "timeZone": zone}, True
    moment = _parse(value.get("dateTime"))
    if moment is None:
        raise Unsupported(f"cannot read the date-time {value.get('dateTime')!r}")
    if moment.tzinfo is not None:
        moment = moment.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    return {"dateTime": moment.isoformat(), "timeZone": zone}, False


def _query_instant(value: Any) -> datetime:
    moment = _parse(value)
    if moment is None:
        raise Unsupported(f"cannot read the time bound {value!r}")
    # A bound with no offset is UTC, which is how Graph reads one.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# ── recurrence ────────────────────────────────────────────────────────


def graph_recurrence_to_rrule(recurrence: Any) -> list[str]:
    """Graph ``patternedRecurrence`` -> ``["RRULE:..."]`` (empty when unreadable)."""
    if not isinstance(recurrence, dict):
        return []
    pattern = recurrence.get("pattern") or {}
    rng = recurrence.get("range") or {}
    kind = str(pattern.get("type") or "")
    freq = _FREQ.get(kind)
    if not freq:
        return []
    parts = [f"FREQ={freq}"]
    interval = int(pattern.get("interval") or 1)
    if interval > 1:
        parts.append(f"INTERVAL={interval}")
    days = [_DAY_CODES[d] for d in pattern.get("daysOfWeek") or [] if d in _DAY_CODES]
    if kind in ("absoluteYearly", "relativeYearly") and pattern.get("month"):
        parts.append(f"BYMONTH={int(pattern['month'])}")
    if kind == "weekly" and days:
        parts.append("BYDAY=" + ",".join(days))
    elif kind in ("relativeMonthly", "relativeYearly") and days:
        index = _INDEX_NUMBERS.get(str(pattern.get("index") or "first"), 1)
        if len(days) == 1:
            parts.append(f"BYDAY={index}{days[0]}")
        else:
            parts.append("BYDAY=" + ",".join(days))
            parts.append(f"BYSETPOS={index}")
    elif kind in ("absoluteMonthly", "absoluteYearly") and pattern.get("dayOfMonth"):
        parts.append(f"BYMONTHDAY={int(pattern['dayOfMonth'])}")
    if rng.get("type") == "numbered" and rng.get("numberOfOccurrences"):
        parts.append(f"COUNT={int(rng['numberOfOccurrences'])}")
    elif rng.get("type") == "endDate" and rng.get("endDate"):
        parts.append("UNTIL=" + str(rng["endDate"]).replace("-", ""))
    return ["RRULE:" + ";".join(parts)]


def _rrule_parts(rules: Any) -> dict[str, str]:
    if isinstance(rules, str):
        rules = [rules]
    rules = [str(r).strip() for r in rules or [] if str(r).strip()]
    if len(rules) != 1:
        raise Unsupported(
            "Exchange takes exactly one RRULE per series; EXDATE, RDATE and multiple "
            "rules are not supported"
        )
    rule = rules[0]
    if not rule.upper().startswith("RRULE:"):
        raise Unsupported(f"only an RRULE recurrence is supported, not {rule.split(':')[0]!r}")
    parts: dict[str, str] = {}
    for chunk in rule[6:].split(";"):
        key, sep, value = chunk.partition("=")
        key = key.strip().upper()
        if not sep or not key or key in parts:
            raise Unsupported(f"cannot read the recurrence rule {rule!r}")
        parts[key] = value.strip().upper()
    extra = sorted(set(parts) - SUPPORTED_RRULE_PARTS)
    if extra:
        raise Unsupported(
            f"recurrence part(s) {', '.join(extra)} cannot be expressed on Exchange; "
            "supported: FREQ (DAILY/WEEKLY/MONTHLY/YEARLY), INTERVAL, BYDAY, BYMONTHDAY, "
            "COUNT, UNTIL"
        )
    return parts


def _positive_int(value: str, what: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise Unsupported(f"{what} must be a whole number, not {value!r}") from None
    if number < 1:
        raise Unsupported(f"{what} must be at least 1")
    return number


def rrule_to_graph(rules: Any, *, start: str, zone: str) -> dict[str, Any]:
    """``["RRULE:..."]`` -> Graph ``patternedRecurrence``, or :class:`Unsupported`.

    ``start`` is the series' first date (ISO), ``zone`` its IANA zone.
    """
    parts = _rrule_parts(rules)
    freq = parts.get("FREQ")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        raise Unsupported(f"FREQ={freq} cannot be expressed on Exchange")
    first = date.fromisoformat(start)
    interval = _positive_int(parts.get("INTERVAL", "1"), "INTERVAL")
    days: list[tuple[int | None, str]] = []
    for token in filter(None, parts.get("BYDAY", "").split(",")):
        match = _BYDAY.fullmatch(token)
        if not match:
            raise Unsupported(f"cannot read BYDAY={parts['BYDAY']}")
        days.append((int(match[1]) if match[1] else None, _DAY_NAMES[match[2]]))
    ordinals = [n for n, _ in days if n is not None]
    month_days = [d for d in parts.get("BYMONTHDAY", "").split(",") if d]
    if len(month_days) > 1:
        raise Unsupported("Exchange takes a single BYMONTHDAY")
    month_day = _positive_int(month_days[0], "BYMONTHDAY") if month_days else None
    if month_day is not None and month_day > 31:
        raise Unsupported("BYMONTHDAY must be between 1 and 31")

    pattern: dict[str, Any] = {"interval": interval, "firstDayOfWeek": "monday"}
    if freq in ("DAILY", "WEEKLY"):
        if month_day is not None or ordinals:
            raise Unsupported(f"FREQ={freq} with BYMONTHDAY or a numbered BYDAY is not supported")
        if freq == "DAILY" and not days:
            pattern["type"] = "daily"
        else:
            if freq == "DAILY" and interval != 1:
                raise Unsupported("FREQ=DAILY with BYDAY and an INTERVAL is not supported")
            pattern["type"] = "weekly"
            pattern["daysOfWeek"] = [d for _, d in days] or [_WEEKDAY_ORDER[first.weekday()]]
    else:
        yearly = freq == "YEARLY"
        if yearly:
            pattern["month"] = first.month
        if month_day is not None and days:
            raise Unsupported("BYMONTHDAY and BYDAY together are not supported")
        if days:
            if len(days) != 1 or days[0][0] not in _INDEX_NAMES:
                raise Unsupported(
                    "a monthly or yearly BYDAY must be one numbered weekday "
                    "(1MO..4MO or -1MO for the last)"
                )
            pattern["type"] = "relativeYearly" if yearly else "relativeMonthly"
            pattern["daysOfWeek"] = [days[0][1]]
            pattern["index"] = _INDEX_NAMES[days[0][0]]
        else:
            pattern["type"] = "absoluteYearly" if yearly else "absoluteMonthly"
            pattern["dayOfMonth"] = month_day or first.day

    rng: dict[str, Any] = {"startDate": first.isoformat(), "recurrenceTimeZone": zone}
    if "COUNT" in parts and "UNTIL" in parts:
        raise Unsupported("COUNT and UNTIL cannot both be set")
    if "COUNT" in parts:
        rng["type"] = "numbered"
        rng["numberOfOccurrences"] = _positive_int(parts["COUNT"], "COUNT")
    elif "UNTIL" in parts:
        match = _UNTIL.fullmatch(parts["UNTIL"])
        if not match:
            raise Unsupported(f"cannot read UNTIL={parts['UNTIL']}")
        rng["type"] = "endDate"
        rng["endDate"] = f"{match[1]}-{match[2]}-{match[3]}"
    else:
        rng["type"] = "noEnd"
    return {"pattern": pattern, "range": rng}


# ── Graph event -> NormalizedEvent ────────────────────────────────────


def _body_text(raw: dict[str, Any]) -> str:
    body = raw.get("body")
    if isinstance(body, dict) and body.get("content") is not None:
        text = str(body.get("content") or "")
        if str(body.get("contentType") or "").lower() == "html":
            text = html.unescape(_TAG.sub("", text))
    else:
        text = str(raw.get("bodyPreview") or "")
    return text.replace("\r\n", "\n").rstrip()


def _address(entry: Any) -> tuple[str, str]:
    email = (entry or {}).get("emailAddress") or {} if isinstance(entry, dict) else {}
    return str(email.get("address") or ""), str(email.get("name") or "")


def to_normalized(raw: dict[str, Any], mailbox: str) -> dict[str, Any]:
    """A Graph event, read from ``mailbox``, as a Calendar v3 event."""
    me = mailbox.casefold()
    organizer, _ = _address(raw.get("organizer"))
    is_organizer = bool(raw.get("isOrganizer"))
    own = _RESPONSE_IN.get(str((raw.get("responseStatus") or {}).get("response") or ""))
    attendees: list[dict[str, Any]] = []
    for entry in raw.get("attendees") or []:
        email, name = _address(entry)
        if not email:
            continue
        response = str(((entry.get("status") or {}).get("response")) or "none")
        out: dict[str, Any] = {
            "email": email,
            "responseStatus": _RESPONSE_IN.get(response, "needsAction"),
        }
        if entry.get("type") == "optional":
            out["optional"] = True
        if organizer and email.casefold() == organizer.casefold():
            out["organizer"] = True
        if email.casefold() == me:
            out["self"] = True
            # The attendee list in a guest's copy can lag; the event's own
            # responseStatus is this mailbox's answer.
            if own and not is_organizer:
                out["responseStatus"] = own
        if name and name.casefold() != email.casefold():
            out["displayName"] = name
        attendees.append(out)

    all_day = bool(raw.get("isAllDay"))
    event: dict[str, Any] = {
        "id": str(raw.get("id") or ""),
        "etag": str(raw.get("@odata.etag") or ""),
        "status": "cancelled" if raw.get("isCancelled") else "confirmed",
        "summary": str(raw.get("subject") or ""),
        "start": _event_time(raw.get("start"), raw.get("originalStartTimeZone"), all_day=all_day),
        "end": _event_time(
            raw.get("end"),
            raw.get("originalEndTimeZone") or raw.get("originalStartTimeZone"),
            all_day=all_day,
        ),
        "attendeesOmitted": False,
    }
    description = _body_text(raw)
    if description:
        event["description"] = description
    location = str((raw.get("location") or {}).get("displayName") or "")
    if location:
        event["location"] = location
    if attendees:
        event["attendees"] = attendees
    if organizer:
        event["organizer"] = {"email": organizer, "self": is_organizer}
    if raw.get("webLink"):
        event["htmlLink"] = str(raw["webLink"])
    join_url = str((raw.get("onlineMeeting") or {}).get("joinUrl") or "")
    if join_url:
        event["conferenceData"] = {
            "conferenceSolution": {"key": {"type": "teamsForBusiness"}, "name": "Microsoft Teams"},
            "entryPoints": [{"entryPointType": "video", "uri": join_url}],
        }
    rules = graph_recurrence_to_rrule(raw.get("recurrence"))
    if rules:
        event["recurrence"] = rules
    if raw.get("seriesMasterId") and raw.get("type") in ("occurrence", "exception"):
        event["recurringEventId"] = str(raw["seriesMasterId"])
    event["provider_extra"] = {
        "changeKey": raw.get("changeKey"),
        "iCalUId": raw.get("iCalUId"),
        "type": raw.get("type"),
        "isOrganizer": is_organizer,
    }
    return event


# ── NormalizedEvent -> Graph ──────────────────────────────────────────


def _graph_attendees(entries: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in entries or []:
        if isinstance(entry, str):
            entry = {"email": entry}
        email = str(entry.get("email") or "").strip()
        if not email:
            continue
        address: dict[str, str] = {"address": email}
        if entry.get("displayName"):
            address["name"] = str(entry["displayName"])
        out.append(
            {"emailAddress": address, "type": "optional" if entry.get("optional") else "required"}
        )
    return out


def _meet_refusal() -> Unsupported:
    return Unsupported(
        "a Google Meet conference cannot be attached to an Exchange event; Microsoft 365 "
        "meetings get a Microsoft Teams link instead (with_meet=true)"
    )


def event_to_graph(event: dict[str, Any], *, zone: str, conference: bool) -> dict[str, Any]:
    """A Calendar v3 create body as a Graph event. Refuses rather than drops."""
    unknown = sorted(set(event) - _CREATE_KEYS)
    if unknown:
        raise Unsupported(f"cannot set {', '.join(unknown)} on an Exchange event")
    conference_data = event.get("conferenceData")
    if event.get("hangoutLink") or (
        isinstance(conference_data, dict) and set(conference_data) - {"createRequest"}
    ):
        raise _meet_refusal()
    body: dict[str, Any] = {"subject": str(event.get("summary") or "")}
    if event.get("description"):
        body["body"] = {"contentType": "text", "content": str(event["description"])}
    if event.get("location"):
        body["location"] = {"displayName": str(event["location"])}
    start, start_all_day = _write_time(event.get("start"), zone)
    end, end_all_day = _write_time(event.get("end"), zone)
    if start_all_day != end_all_day:
        raise Unsupported("start and end must both be dates (all-day) or both date-times")
    body["start"], body["end"] = start, end
    if start_all_day:
        body["isAllDay"] = True
    attendees = _graph_attendees(event.get("attendees"))
    if attendees:
        body["attendees"] = attendees
    if conference:
        body["isOnlineMeeting"] = True
        body["onlineMeetingProvider"] = "teamsForBusiness"
    if event.get("recurrence"):
        body["recurrence"] = rrule_to_graph(
            event["recurrence"], start=start["dateTime"][:10], zone=start["timeZone"]
        )
    return body


def patch_to_graph(body: dict[str, Any], *, zone: str) -> dict[str, Any]:
    """A Calendar v3 PATCH delta as a Graph PATCH body. Refuses rather than drops."""
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key == "summary":
            out["subject"] = str(value or "")
        elif key == "description":
            out["body"] = {"contentType": "text", "content": str(value or "")}
        elif key == "location":
            out["location"] = {"displayName": str(value or "")}
        elif key in ("start", "end"):
            out[key], all_day = _write_time(value, zone)
            if all_day:
                out["isAllDay"] = True
            elif isinstance(value, dict) and "date" in value:
                out["isAllDay"] = False
        elif key == "attendees":
            out["attendees"] = _graph_attendees(value)
        else:
            raise Unsupported(f"cannot change {key!r} on an Exchange event")
    return out


def _with_windows_zones(body: dict[str, Any]) -> dict[str, Any]:
    """``body`` with each IANA zone swapped for its Windows name (or UTC)."""
    out = dict(body)
    for key in ("start", "end"):
        value = body.get(key)
        if not isinstance(value, dict):
            continue
        windows = to_windows(value.get("timeZone"))
        if windows:
            out[key] = {**value, "timeZone": windows}
        elif body.get("isAllDay"):
            out[key] = {**value, "timeZone": "UTC"}
        else:
            local = datetime.fromisoformat(value["dateTime"]).replace(
                tzinfo=ZoneInfo(value["timeZone"])
            )
            moment = local.astimezone(UTC).replace(tzinfo=None)
            out[key] = {"dateTime": moment.isoformat(), "timeZone": "UTC"}
    recurrence = body.get("recurrence")
    if isinstance(recurrence, dict):
        rng = dict(recurrence.get("range") or {})
        rng["recurrenceTimeZone"] = to_windows(rng.get("recurrenceTimeZone")) or "UTC"
        out["recurrence"] = {**recurrence, "range": rng}
    return out


def _zone_rejected(exc: WorkspaceError) -> bool:
    text = f"{exc.code or ''} {exc}".replace(" ", "").lower()
    return exc.status == 400 and "timezone" in text


def _default_zone() -> str:
    from robothor.constants import DEFAULT_TIMEZONE, platform_timezone

    try:
        return platform_timezone()
    except Exception:  # noqa: BLE001 - a settings problem must not lose the event
        return DEFAULT_TIMEZONE


# ── the provider ──────────────────────────────────────────────────────


class GraphCalendar:
    """:class:`~robothor.workspace.protocols.CalendarProvider` over Microsoft Graph.

    ``graph`` is a :class:`GraphClient`, or an async factory for one (built on
    first use, once per event loop -- an httpx client cannot cross loops).
    """

    capabilities: Capabilities = MICROSOFT365_CAPABILITIES

    def __init__(
        self,
        graph: GraphClient | Callable[[], Awaitable[GraphClient]],
        *,
        assistant_mailbox: str,
        owner_mailbox: str = "",
        default_timezone: Callable[[], str] | None = None,
    ) -> None:
        assistant = assistant_mailbox.strip().lower()
        if not _EMAIL.match(assistant):
            raise ValueError("assistant_mailbox must be an email address")
        self.assistant_mailbox = assistant
        self.owner_mailbox = owner_mailbox.strip().lower()
        self._static = graph if isinstance(graph, GraphClient) else None
        self._factory = None if isinstance(graph, GraphClient) else graph
        self._default_timezone = default_timezone or _default_zone
        self._client: GraphClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    # ── plumbing ────────────────────────────────────────────────────────

    async def _graph(self) -> GraphClient:
        if self._static is not None:
            return self._static
        assert self._factory is not None
        loop = asyncio.get_running_loop()
        if self._client is not None and self._client_loop is loop:
            return self._client
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        async with self._lock:
            if self._client is None or self._client_loop is not loop:
                self._client = await self._factory()
                self._client_loop = loop
        return self._client

    def _zone(self) -> str:
        return to_iana(self._default_timezone()) or "UTC"

    def _mailbox(self, ref: CalendarRef) -> str:
        if ref.mailbox:
            return ref.mailbox.strip().lower()
        calendar_id = ref.calendar_id.strip()
        if _EMAIL.match(calendar_id):
            return calendar_id.lower()
        return self.assistant_mailbox

    def _calendar_path(self, ref: CalendarRef) -> str:
        mailbox = self._mailbox(ref)
        calendar_id = ref.calendar_id.strip()
        base = f"/users/{quote(mailbox, safe='@.')}"
        if (
            not calendar_id
            or calendar_id.lower() == "primary"
            or calendar_id.lower() == mailbox
            or _EMAIL.match(calendar_id)
        ):
            return f"{base}/calendar"
        return f"{base}/calendars/{quote(calendar_id, safe='')}"

    def _event_path(self, ref: CalendarRef, event_id: str) -> str:
        if not event_id or not str(event_id).strip():
            raise Unsupported("an event id is required")
        mailbox = quote(self._mailbox(ref), safe="@.")
        return f"/users/{mailbox}/events/{quote(str(event_id).strip(), safe='')}"

    # ── protocol ────────────────────────────────────────────────────────

    def resolve(self, kind: Literal["own", "operator"], *, address: str = "") -> CalendarRef:
        if kind == "own":
            return CalendarRef(self.assistant_mailbox, "own", mailbox=self.assistant_mailbox)
        if kind == "operator":
            mailbox = self.owner_mailbox or address.strip().lower()
            if mailbox:
                return CalendarRef(mailbox, "operator", mailbox=mailbox)
        raise ValueError(f"cannot resolve calendar kind {kind!r} without an address")

    async def list(self, ref: CalendarRef, query: EventQuery) -> dict[str, Any]:
        if query.get("single_events") is False:
            raise Unsupported("Exchange lists occurrences only (single_events=true)")
        if query.get("order_by") not in (None, "startTime"):
            raise Unsupported(f"cannot order an Exchange calendar by {query.get('order_by')!r}")
        start = _query_instant(query.get("time_min"))
        end = (
            _query_instant(query["time_max"])
            if query.get("time_max")
            else start + timedelta(days=366)
        )
        max_results = max(1, int(query.get("max_results") or 250))
        params = {
            "startDateTime": start.astimezone(UTC).isoformat(),
            "endDateTime": end.astimezone(UTC).isoformat(),
            "$orderby": "start/dateTime",
            "$top": min(max_results, 250),
        }
        graph = await self._graph()
        items = await graph.get_all(
            self._calendar_path(ref) + "/calendarView",
            params,
            max_items=max_results,
            headers={"Prefer": _TEXT_PREFER},
        )
        mailbox = self._mailbox(ref)
        return {"items": [to_normalized(item, mailbox) for item in items]}

    async def get(self, ref: CalendarRef, event_id: str) -> dict[str, Any]:
        graph = await self._graph()
        raw = await graph.get(self._event_path(ref, event_id), headers={"Prefer": _TEXT_PREFER})
        return to_normalized(raw, self._mailbox(ref))

    async def create(
        self,
        ref: CalendarRef,
        event: dict[str, Any],
        *,
        conference: bool,
        send_updates: str | None,
    ) -> dict[str, Any]:
        if event.get("attendees") and send_updates != "all":
            raise Unsupported(_always_notifies(send_updates))
        body = event_to_graph(event, zone=self._zone(), conference=conference)
        path = self._calendar_path(ref) + "/events"
        graph = await self._graph()
        headers = {"Prefer": _TEXT_PREFER}
        try:
            raw = await graph.post(path, body, headers=headers)
        except WorkspaceError as exc:
            # A 400 is a definite "not created", so one retry is safe.
            fallback = _with_windows_zones(body)
            if not _zone_rejected(exc) or fallback == body:
                raise
            raw = await graph.post(path, fallback, headers=headers)
        return to_normalized(raw, self._mailbox(ref))

    async def conditional_patch(
        self,
        ref: CalendarRef,
        event_id: str,
        body: dict[str, Any],
        *,
        etag: str,
        send_updates: str,
    ) -> dict[str, Any]:
        graph_body = patch_to_graph(body, zone=self._zone())
        if not etag:
            raise Unsupported("refusing an unconditional write: no etag")
        if send_updates != "all":
            current = await self.get(ref, event_id)
            guests = [a for a in current.get("attendees", []) if not a.get("self")]
            if (current.get("organizer") or {}).get("self") and (guests or body.get("attendees")):
                raise Unsupported(_always_notifies(send_updates))
        graph = await self._graph()
        raw = await graph.patch(
            self._event_path(ref, event_id),
            graph_body,
            headers={"If-Match": etag, "Prefer": _TEXT_PREFER},
        )
        return to_normalized(raw, self._mailbox(ref))

    async def rsvp(
        self,
        ref: CalendarRef,
        event_id: str,
        response: str,
        *,
        comment: str | None = None,
        send_response: bool = True,
    ) -> dict[str, Any]:
        """Send this mailbox's answer to an invitation (a Graph action, not a PATCH)."""
        action = _RSVP_ACTIONS.get(response)
        if action is None:
            raise Unsupported(f"response must be one of {', '.join(_RSVP_ACTIONS)}")
        payload: dict[str, Any] = {"sendResponse": bool(send_response)}
        if comment is not None:
            payload["comment"] = comment
        graph = await self._graph()
        await graph.post(self._event_path(ref, event_id) + "/" + action, payload)
        return {}

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
        # The guarded read / screen / write / read-back is provider-neutral and
        # lives in calendar_attendees; it calls `rsvp` for the write.
        from robothor.engine import calendar_attendees
        from robothor.workspace.bridge import bind_engine_loop

        with bind_engine_loop(asyncio.get_running_loop()):
            return await asyncio.to_thread(
                calendar_attendees.respond,
                ref,
                event_id,
                response,
                screen=screen,
                comment=comment,
                send_updates=send_updates,
                cancelled=cancelled,
                provider=self,
            )

    async def delete(self, ref: CalendarRef, event_id: str, *, send_updates: str) -> dict[str, Any]:
        current = await self.get(ref, event_id)
        guests = [a["email"] for a in current.get("attendees", []) if not a.get("self")]
        organises = bool((current.get("organizer") or {}).get("self"))
        graph = await self._graph()
        path = self._event_path(ref, event_id)
        if organises and guests and current.get("status") != "cancelled":
            if send_updates != "all":
                raise Unsupported(_always_notifies(send_updates))
            # `cancel` is the organiser's delete that tells the attendees.
            await graph.post(path + "/cancel", {"comment": ""})
            return {"method": "cancel", "cancellation_sent_to": guests}
        await graph.delete(path)
        return {"method": "delete"}

    async def enable_meeting_artifacts(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """No-op: Teams transcription follows the tenant's meeting policy.

        The Google provider turns Meet transcription on per meeting; Graph has
        no equivalent per-event switch the assistant can set at booking time.
        """
        return None
