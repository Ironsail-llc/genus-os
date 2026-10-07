"""Exchange calendar behaviour for :class:`~robothor.workspace.tests.fake_graph.FakeGraphTenant`.

``install_calendar(tenant)`` registers the calendar routes into the tenant's
route registry (``tenant.route``) and returns a :class:`FakeExchangeCalendar`
holding the state a test inspects. Modelled on what Exchange Online does, as
far as the calendar tools care:

* events live per mailbox, under an id that is IMMUTABLE when the request
  asked for ``Prefer: IdType="ImmutableId"`` (``AAkALg-imm-…``) and a
  folder-scoped id otherwise (``AAMkAD-mut-…``);
* every write gives the event a new ``@odata.etag``, and a PATCH whose
  ``If-Match`` names an older one is answered 412;
* times are stored as written (local wall clock + zone) and returned in UTC
  when ``outlook.timezone="UTC"`` is preferred, with the zone the event was
  written in reported as ``originalStartTimeZone``; all-day events come back
  as floating midnight;
* ``calendarView`` expands a weekly series master into occurrences
  (``type: occurrence``, ``seriesMasterId``) inside the window;
* ``isOnlineMeeting`` events get a Teams join URL;
* ``accept`` / ``decline`` / ``tentativelyAccept`` set the mailbox's own
  response; ``cancel`` is organiser-only; both ``cancel`` and an organiser's
  ``DELETE`` of a meeting with attendees send cancellations. Every message
  Exchange would have sent is appended to :attr:`FakeExchangeCalendar.notifications`.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx

from robothor.workspace.microsoft.timezones import to_iana
from robothor.workspace.tests.fake_graph import graph_error

if TYPE_CHECKING:
    from robothor.workspace.tests.fake_graph import FakeGraphTenant

__all__ = ["FakeExchangeCalendar", "install_calendar"]

_MAILBOX = r"/users/(?P<mailbox>[^/]+)"
_CALENDAR = _MAILBOX + r"/(?:calendar|calendars/(?P<calendar>[^/]+))"
_EVENT = _MAILBOX + r"/(?:calendar/|calendars/[^/]+/)?events/(?P<id>[^/]+)"
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass
class FakeExchangeCalendar:
    """State of every mailbox's calendar, plus what Exchange would have mailed."""

    #: mailbox -> events as stored (Graph shape, times as written).
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: ``{"kind": invite|update|cancel|response, "to": [...], "event": id}``.
    notifications: list[dict[str, Any]] = field(default_factory=list)
    #: When set, a write whose start/end ``timeZone`` is not a Windows name is
    #: refused 400, like a tenant that does not take IANA names.
    reject_iana: bool = False
    _counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def box(self, mailbox: str) -> list[dict[str, Any]]:
        return self.events.setdefault(mailbox.lower(), [])

    def add(self, mailbox: str, event: dict[str, Any]) -> dict[str, Any]:
        """Seed an event (Graph shape, times local + zone). Returns it with id/etag."""
        stored = json.loads(json.dumps(event))
        stored.setdefault("id", f"AAkALg-imm-{next(self._counter)}")
        stored.setdefault("isCancelled", False)
        stored.setdefault("attendees", [])
        stored.setdefault("type", "singleInstance")
        self._touch(stored)
        self.box(mailbox).append(stored)
        return stored

    def find(self, mailbox: str, event_id: str) -> dict[str, Any] | None:
        return next((e for e in self.box(mailbox) if e["id"] == event_id), None)

    def _touch(self, event: dict[str, Any]) -> None:
        event["@odata.etag"] = f'W/"etag-{next(self._counter)}"'
        event["changeKey"] = event["@odata.etag"][3:-1]


def _parse(value: str) -> datetime:
    head, _, frac = value.partition(".")
    return datetime.fromisoformat(head)


def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(to_iana(name) or "UTC")


def _instant(time: dict[str, Any]) -> datetime:
    moment = _parse(time["dateTime"])
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_zone(time.get("timeZone") or "UTC"))
    return moment.astimezone(UTC)


def _graph_time(moment: datetime) -> dict[str, str]:
    return {
        "dateTime": moment.astimezone(UTC).replace(tzinfo=None).isoformat() + ".0000000",
        "timeZone": "UTC",
    }


def _render(event: dict[str, Any], mailbox: str, prefer: str) -> dict[str, Any]:
    out = json.loads(json.dumps(event))
    out["originalStartTimeZone"] = event["start"].get("timeZone", "UTC")
    out["originalEndTimeZone"] = event["end"].get("timeZone", "UTC")
    if 'outlook.timezone="UTC"' in prefer:
        for key in ("start", "end"):
            if event.get("isAllDay"):
                raw = _parse(event[key]["dateTime"]).replace(tzinfo=None)
                out[key] = {"dateTime": raw.isoformat() + ".0000000", "timeZone": "UTC"}
            else:
                out[key] = _graph_time(_instant(event[key]))
    organizer = (event.get("organizer") or {}).get("emailAddress", {}).get("address", "")
    out["isOrganizer"] = organizer.lower() == mailbox.lower()
    out.setdefault(
        "responseStatus",
        {"response": "organizer" if out["isOrganizer"] else "notResponded"},
    )
    out["webLink"] = f"https://outlook.office365.example/calendar/item/{event['id']}"
    return out


def _occurrences(
    master: dict[str, Any], start: datetime, end: datetime
) -> list[tuple[dict[str, Any], datetime]]:
    """A weekly series master expanded into ``(occurrence, start)`` in ``[start, end)``."""
    pattern = master["recurrence"]["pattern"]
    rng = master["recurrence"]["range"]
    if pattern.get("type") != "weekly":
        raise NotImplementedError("the fake expands weekly series only")
    zone = _zone(master["start"].get("timeZone") or "UTC")
    first = _parse(master["start"]["dateTime"]).replace(tzinfo=zone)
    length = _instant(master["end"]) - _instant(master["start"])
    days = {_WEEKDAYS.index(d) for d in pattern.get("daysOfWeek") or [_WEEKDAYS[first.weekday()]]}
    interval = int(pattern.get("interval") or 1)
    limit = int(rng.get("numberOfOccurrences") or 0) if rng.get("type") == "numbered" else 0
    until = date.fromisoformat(rng["endDate"]) if rng.get("type") == "endDate" else None
    out: list[tuple[dict[str, Any], datetime]] = []
    week_start = first.date() - timedelta(days=first.weekday())
    count = 0
    for week in range(0, 520, interval):
        for offset in range(7):
            day = week_start + timedelta(days=week * 7 + offset)
            if day.weekday() not in days or day < first.date():
                continue
            if until and day > until:
                return out
            count += 1
            if limit and count > limit:
                return out
            local = datetime.combine(day, first.timetz().replace(tzinfo=None), tzinfo=zone)
            at = local.astimezone(UTC)
            if at >= end:
                return out
            if at + length > start:
                occurrence = json.loads(json.dumps(master))
                occurrence.pop("recurrence", None)
                occurrence["id"] = f"{master['id']}-occ-{day.isoformat()}"
                occurrence["type"] = "occurrence"
                occurrence["seriesMasterId"] = master["id"]
                occurrence["start"] = {
                    "dateTime": local.replace(tzinfo=None).isoformat(),
                    "timeZone": master["start"].get("timeZone"),
                }
                occurrence["end"] = {
                    "dateTime": (local + length).replace(tzinfo=None).isoformat(),
                    "timeZone": master["end"].get("timeZone"),
                }
                out.append((occurrence, at))
    return out


def _attendee_addresses(event: dict[str, Any]) -> list[str]:
    return [a["emailAddress"]["address"] for a in event.get("attendees") or []]


def install_calendar(tenant: FakeGraphTenant) -> FakeExchangeCalendar:
    """Register the Exchange calendar routes on ``tenant``; returns its state."""
    state = FakeExchangeCalendar()
    tenant.calendar = state  # type: ignore[attr-defined]

    def mailbox_of(match: Any) -> str:
        return unquote(match["mailbox"]).lower()

    def prefer(request: httpx.Request) -> str:
        return request.headers.get("prefer", "")

    def bad_zone(body: dict[str, Any]) -> bool:
        for key in ("start", "end"):
            zone = (body.get(key) or {}).get("timeZone")
            if zone is None:
                continue
            if state.reject_iana and "/" in zone:
                return True
            if to_iana(zone) is None:
                return True
        return False

    @tenant.route("GET", _CALENDAR + r"/calendarView")
    async def calendar_view(tenant, request, match):
        mailbox = mailbox_of(match)
        params = request.url.params
        if "startDateTime" not in params or "endDateTime" not in params:
            return graph_error(400, "ErrorInvalidParameter", "startDateTime/endDateTime required")
        start = datetime.fromisoformat(params["startDateTime"])
        end = datetime.fromisoformat(params["endDateTime"])
        start = start if start.tzinfo else start.replace(tzinfo=UTC)
        end = end if end.tzinfo else end.replace(tzinfo=UTC)
        found: list[tuple[datetime, dict[str, Any]]] = []
        for event in state.box(mailbox):
            if event.get("recurrence"):
                found += [(at, occ) for occ, at in _occurrences(event, start, end)]
                continue
            at = _instant(event["start"])
            if at < end and _instant(event["end"]) > start:
                found.append((at, event))
        found.sort(key=lambda pair: pair[0])
        top = int(params.get("$top", "10"))
        skip = int(params.get("$skip", "0"))
        page = [_render(e, mailbox, prefer(request)) for _, e in found[skip : skip + top]]
        body: dict[str, Any] = {"value": page}
        if skip + top < len(found):
            query = request.url.copy_merge_params({"$skip": str(skip + top)})
            body["@odata.nextLink"] = str(query)
        return httpx.Response(200, json=body)

    @tenant.route("GET", _EVENT)
    async def get_event(tenant, request, match):
        mailbox = mailbox_of(match)
        event = state.find(mailbox, unquote(match["id"]))
        if event is None:
            return graph_error(404, "ErrorItemNotFound", "The specified object was not found.")
        return httpx.Response(200, json=_render(event, mailbox, prefer(request)))

    @tenant.route("POST", _CALENDAR + r"/events")
    async def create_event(tenant, request, match):
        mailbox = mailbox_of(match)
        body = json.loads(request.content or b"{}")
        if bad_zone(body):
            return graph_error(
                400, "TimeZoneNotSupportedException", "A valid TimeZone value must be specified."
            )
        prefix = "AAkALg-imm" if 'IdType="ImmutableId"' in prefer(request) else "AAMkAD-mut"
        body["id"] = f"{prefix}-{next(state._counter)}"
        body["organizer"] = {"emailAddress": {"address": mailbox}}
        if body.get("isOnlineMeeting"):
            body["onlineMeeting"] = {
                "joinUrl": f"https://teams.microsoft.example/l/meetup-join/{body['id']}"
            }
        for attendee in body.get("attendees") or []:
            attendee.setdefault("status", {"response": "none", "time": "0001-01-01T00:00:00Z"})
        stored = state.add(mailbox, body)
        if stored.get("attendees"):
            state.notifications.append(
                {"kind": "invite", "to": _attendee_addresses(stored), "event": stored["id"]}
            )
        return httpx.Response(201, json=_render(stored, mailbox, prefer(request)))

    @tenant.route("PATCH", _EVENT)
    async def update_event(tenant, request, match):
        mailbox = mailbox_of(match)
        event = state.find(mailbox, unquote(match["id"]))
        if event is None:
            return graph_error(404, "ErrorItemNotFound", "The specified object was not found.")
        etag = request.headers.get("if-match")
        if etag and etag != event["@odata.etag"]:
            return graph_error(412, "ErrorIrresolvableConflict", "The change key is stale.")
        body = json.loads(request.content or b"{}")
        if bad_zone(body):
            return graph_error(
                400, "TimeZoneNotSupportedException", "A valid TimeZone value must be specified."
            )
        if "attendees" in body:
            previous = {
                a["emailAddress"]["address"].lower(): a.get("status")
                for a in event.get("attendees") or []
            }
            for attendee in body["attendees"]:
                address = attendee["emailAddress"]["address"].lower()
                attendee["status"] = previous.get(address) or {"response": "none"}
        event.update(body)
        state._touch(event)
        organizer = event["organizer"]["emailAddress"]["address"].lower()
        if organizer == mailbox and event.get("attendees"):
            state.notifications.append(
                {"kind": "update", "to": _attendee_addresses(event), "event": event["id"]}
            )
        return httpx.Response(200, json=_render(event, mailbox, prefer(request)))

    def respond_route(action: str, response: str) -> None:
        @tenant.route("POST", _EVENT + "/" + action)
        async def rsvp(tenant, request, match):
            mailbox = mailbox_of(match)
            event = state.find(mailbox, unquote(match["id"]))
            if event is None:
                return graph_error(404, "ErrorItemNotFound", "The specified object was not found.")
            organizer = event["organizer"]["emailAddress"]["address"].lower()
            if organizer == mailbox:
                return graph_error(
                    400, "ErrorInvalidRequest", "Your request can't be completed; organizer."
                )
            body = json.loads(request.content or b"{}")
            event["responseStatus"] = {"response": response, "time": "2026-10-07T12:00:00Z"}
            for attendee in event.get("attendees") or []:
                if attendee["emailAddress"]["address"].lower() == mailbox:
                    attendee["status"] = {"response": response}
            state._touch(event)
            if body.get("sendResponse", True):
                state.notifications.append(
                    {
                        "kind": "response",
                        "to": [organizer],
                        "event": event["id"],
                        "response": response,
                        "comment": body.get("comment", ""),
                    }
                )
            return httpx.Response(202)

    respond_route("accept", "accepted")
    respond_route("decline", "declined")
    respond_route("tentativelyAccept", "tentativelyAccepted")

    @tenant.route("POST", _EVENT + "/cancel")
    async def cancel(tenant, request, match):
        mailbox = mailbox_of(match)
        event = state.find(mailbox, unquote(match["id"]))
        if event is None:
            return graph_error(404, "ErrorItemNotFound", "The specified object was not found.")
        if event["organizer"]["emailAddress"]["address"].lower() != mailbox:
            return graph_error(
                400, "ErrorAccessDenied", "Your request can't be completed; not the organizer."
            )
        state.box(mailbox).remove(event)
        body = json.loads(request.content or b"{}")
        state.notifications.append(
            {
                "kind": "cancel",
                "to": _attendee_addresses(event),
                "event": event["id"],
                "comment": body.get("comment", ""),
            }
        )
        return httpx.Response(202)

    @tenant.route("DELETE", _EVENT)
    async def delete(tenant, request, match):
        mailbox = mailbox_of(match)
        event = state.find(mailbox, unquote(match["id"]))
        if event is None:
            return graph_error(404, "ErrorItemNotFound", "The specified object was not found.")
        state.box(mailbox).remove(event)
        organizer = event["organizer"]["emailAddress"]["address"].lower()
        if organizer == mailbox and event.get("attendees"):
            state.notifications.append(
                {"kind": "cancel", "to": _attendee_addresses(event), "event": event["id"]}
            )
        return httpx.Response(204)

    return state
