"""GraphCalendar: Exchange events in, Google Calendar v3 shapes out, and back."""

from __future__ import annotations

from typing import Any

import pytest

from robothor.workspace.errors import PreconditionFailed, Unsupported
from robothor.workspace.microsoft.calendar import (
    GraphCalendar,
    graph_recurrence_to_rrule,
    rrule_to_graph,
    to_normalized,
)
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.microsoft.timezones import to_iana, to_windows
from robothor.workspace.protocols import CalendarProvider
from robothor.workspace.tests.fake_graph import FakeGraphTenant
from robothor.workspace.tests.fake_graph_calendar import FakeExchangeCalendar, install_calendar
from robothor.workspace.types import CalendarRef

ASSISTANT = "assistant@example.com"
OWNER = "owner@example.com"
GUEST = "guest@example.com"


class StaticToken:
    def __init__(self, tenant: FakeGraphTenant) -> None:
        tenant.issued_tokens.append("static-token")

    async def token(self) -> str:
        return "static-token"


@pytest.fixture
def tenant() -> FakeGraphTenant:
    return FakeGraphTenant()


@pytest.fixture
def exchange(tenant: FakeGraphTenant) -> FakeExchangeCalendar:
    return install_calendar(tenant)


@pytest.fixture
async def cal(tenant: FakeGraphTenant, exchange: FakeExchangeCalendar):
    graph = GraphClient(StaticToken(tenant), transport=tenant.transport())
    calendar = GraphCalendar(
        graph,
        assistant_mailbox=ASSISTANT,
        owner_mailbox=OWNER,
        default_timezone=lambda: "America/New_York",
    )
    yield calendar
    await graph.aclose()


def _meeting(**extra: Any) -> dict[str, Any]:
    event = {
        "subject": "Planning",
        "start": {"dateTime": "2026-10-08T10:00:00", "timeZone": "Eastern Standard Time"},
        "end": {"dateTime": "2026-10-08T10:30:00", "timeZone": "Eastern Standard Time"},
        "organizer": {"emailAddress": {"address": OWNER}},
        "attendees": [
            {
                "type": "required",
                "status": {"response": "accepted"},
                "emailAddress": {"address": GUEST, "name": "Guest"},
            }
        ],
    }
    event.update(extra)
    return event


# ── reading: Graph -> NormalizedEvent ────────────────────────────────


def test_implements_the_protocol(cal: GraphCalendar) -> None:
    assert isinstance(cal, CalendarProvider)
    assert cal.capabilities["provider"] == "microsoft365"
    assert cal.capabilities["send_updates_modes"] == ("all",)
    assert cal.capabilities["online_meeting"] == "teams_meeting"


def test_resolve_own_and_operator(cal: GraphCalendar) -> None:
    assert cal.resolve("own") == CalendarRef(ASSISTANT, "own", mailbox=ASSISTANT)
    # The configured owner mailbox wins over the owner.yaml address.
    assert cal.resolve("operator", address="alias@example.com") == CalendarRef(
        OWNER, "operator", mailbox=OWNER
    )


def test_windows_zone_names_map_to_iana() -> None:
    assert to_iana("Eastern Standard Time") == "America/New_York"
    assert to_iana("W. Europe Standard Time") == "Europe/Berlin"
    assert to_iana("tzone://Microsoft/Utc") == "UTC"
    assert to_iana("Europe/Lisbon") == "Europe/Lisbon"
    assert to_iana("Not A Zone") is None
    assert to_windows("America/New_York") == "Eastern Standard Time"
    assert to_windows("Asia/Calcutta") == "India Standard Time"


def test_timed_event_maps_to_calendar_v3() -> None:
    raw = {
        "id": "AAkALg-imm-1",
        "@odata.etag": 'W/"abc"',
        "subject": "Planning",
        "body": {"contentType": "text", "content": "Agenda\r\n"},
        "location": {"displayName": "Room 4"},
        "start": {"dateTime": "2026-10-08T14:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-08T14:30:00.0000000", "timeZone": "UTC"},
        "originalStartTimeZone": "Eastern Standard Time",
        "isAllDay": False,
        "isCancelled": False,
        "isOrganizer": True,
        "organizer": {"emailAddress": {"address": OWNER}},
        "attendees": [
            {
                "type": "required",
                "status": {"response": "none"},
                "emailAddress": {"address": GUEST},
            },
            {
                "type": "optional",
                "status": {"response": "tentativelyAccepted"},
                "emailAddress": {"address": "b@example.com", "name": "B"},
            },
            {
                "type": "required",
                "status": {"response": "declined"},
                "emailAddress": {"address": "c@example.com"},
            },
        ],
        "webLink": "https://outlook.office365.example/x",
        "onlineMeeting": {"joinUrl": "https://teams.microsoft.example/l/meetup-join/1"},
    }
    event = to_normalized(raw, OWNER)
    assert event["id"] == "AAkALg-imm-1"
    assert event["etag"] == 'W/"abc"'
    assert event["status"] == "confirmed"
    assert event["summary"] == "Planning"
    assert event["description"] == "Agenda"
    assert event["location"] == "Room 4"
    assert event["start"] == {
        "dateTime": "2026-10-08T10:00:00-04:00",
        "timeZone": "America/New_York",
    }
    assert event["end"]["dateTime"] == "2026-10-08T10:30:00-04:00"
    assert event["attendees"] == [
        {"email": GUEST, "responseStatus": "needsAction"},
        {
            "email": "b@example.com",
            "responseStatus": "tentative",
            "optional": True,
            "displayName": "B",
        },
        {"email": "c@example.com", "responseStatus": "declined"},
    ]
    assert event["organizer"] == {"email": OWNER, "self": True}
    assert event["htmlLink"] == "https://outlook.office365.example/x"
    assert event["conferenceData"]["conferenceSolution"]["key"]["type"] == "teamsForBusiness"
    assert event["conferenceData"]["entryPoints"][0]["uri"].endswith("/meetup-join/1")
    assert event["attendeesOmitted"] is False
    assert "recurrence" not in event


def test_self_entry_carries_the_mailbox_response() -> None:
    raw = {
        "id": "e",
        "start": {"dateTime": "2026-10-08T14:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-08T15:00:00.0000000", "timeZone": "UTC"},
        "isOrganizer": False,
        "responseStatus": {"response": "accepted"},
        "organizer": {"emailAddress": {"address": GUEST}},
        "attendees": [
            {
                "type": "required",
                "status": {"response": "none"},
                "emailAddress": {"address": OWNER},
            },
            {
                "type": "required",
                "status": {"response": "none"},
                "emailAddress": {"address": GUEST},
            },
        ],
        "isCancelled": True,
    }
    event = to_normalized(raw, OWNER)
    assert event["attendees"][0] == {"email": OWNER, "responseStatus": "accepted", "self": True}
    assert event["attendees"][1] == {
        "email": GUEST,
        "responseStatus": "needsAction",
        "organizer": True,
    }
    assert event["organizer"] == {"email": GUEST, "self": False}
    assert event["status"] == "cancelled"
    # No zone to localise into: UTC, said as such.
    assert event["start"] == {"dateTime": "2026-10-08T14:00:00+00:00", "timeZone": "UTC"}


def test_all_day_maps_to_dates() -> None:
    raw = {
        "id": "e",
        "isAllDay": True,
        "start": {"dateTime": "2026-10-09T00:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-10T00:00:00.0000000", "timeZone": "UTC"},
        "originalStartTimeZone": "Pacific Standard Time",
    }
    event = to_normalized(raw, OWNER)
    assert event["start"] == {"date": "2026-10-09"}
    assert event["end"] == {"date": "2026-10-10"}


def test_occurrence_names_its_series() -> None:
    raw = {
        "id": "occ",
        "type": "occurrence",
        "seriesMasterId": "master",
        "start": {"dateTime": "2026-10-08T14:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-08T15:00:00.0000000", "timeZone": "UTC"},
    }
    assert to_normalized(raw, OWNER)["recurringEventId"] == "master"


# ── recurrence, both ways ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rule", "pattern", "rng"),
    [
        (
            "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;COUNT=6",
            {"type": "weekly", "interval": 1, "daysOfWeek": ["monday", "wednesday"]},
            {"type": "numbered", "numberOfOccurrences": 6},
        ),
        (
            "RRULE:FREQ=DAILY;INTERVAL=2;UNTIL=20261031",
            {"type": "daily", "interval": 2},
            {"type": "endDate", "endDate": "2026-10-31"},
        ),
        (
            "RRULE:FREQ=MONTHLY;BYMONTHDAY=15",
            {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 15},
            {"type": "noEnd"},
        ),
        (
            "RRULE:FREQ=MONTHLY;BYDAY=-1FR",
            {"type": "relativeMonthly", "interval": 1, "daysOfWeek": ["friday"], "index": "last"},
            {"type": "noEnd"},
        ),
        (
            "RRULE:FREQ=YEARLY",
            {"type": "absoluteYearly", "interval": 1, "dayOfMonth": 8, "month": 10},
            {"type": "noEnd"},
        ),
        (
            "RRULE:FREQ=WEEKLY;UNTIL=20261231T235959Z",
            {"type": "weekly", "interval": 1, "daysOfWeek": ["thursday"]},
            {"type": "endDate", "endDate": "2026-12-31"},
        ),
    ],
)
def test_rrule_subset_to_patterned_recurrence(rule, pattern, rng) -> None:
    out = rrule_to_graph([rule], start="2026-10-08", zone="America/New_York")
    expected_pattern = {"firstDayOfWeek": "monday", **pattern}
    assert out["pattern"] == expected_pattern
    assert out["range"] == {
        **rng,
        "startDate": "2026-10-08",
        "recurrenceTimeZone": "America/New_York",
    }


@pytest.mark.parametrize(
    "rules",
    [
        ["RRULE:FREQ=HOURLY"],
        ["RRULE:FREQ=WEEKLY;BYSETPOS=1"],
        ["RRULE:FREQ=MONTHLY;BYMONTH=3"],
        ["RRULE:FREQ=WEEKLY;COUNT=2;UNTIL=20261231"],
        ["RRULE:FREQ=MONTHLY;BYMONTHDAY=-1"],
        ["RRULE:FREQ=MONTHLY;BYDAY=2MO,3TU"],
        ["RRULE:FREQ=WEEKLY", "EXDATE:20261015T100000"],
        ["RRULE:FREQ=WEEKLY", "RRULE:FREQ=DAILY"],
        ["RRULE:FREQ=WEEKLY;BYDAY=1MO"],
        ["FREQ=WEEKLY;INTERVAL=0"],
    ],
)
def test_rrule_outside_the_subset_is_unsupported(rules) -> None:
    with pytest.raises(Unsupported):
        rrule_to_graph(rules, start="2026-10-08", zone="UTC")


def test_patterned_recurrence_to_rrule() -> None:
    assert graph_recurrence_to_rrule(
        {
            "pattern": {"type": "weekly", "interval": 2, "daysOfWeek": ["monday", "friday"]},
            "range": {"type": "numbered", "numberOfOccurrences": 4},
        }
    ) == ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,FR;COUNT=4"]
    assert graph_recurrence_to_rrule(
        {
            "pattern": {"type": "relativeMonthly", "interval": 1, "daysOfWeek": ["tuesday"]},
            "range": {"type": "endDate", "endDate": "2027-01-31"},
        }
    ) == ["RRULE:FREQ=MONTHLY;BYDAY=1TU;UNTIL=20270131"]
    assert graph_recurrence_to_rrule(
        {"pattern": {"type": "absoluteYearly", "interval": 1, "month": 3, "dayOfMonth": 4}}
    ) == ["RRULE:FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=4"]


# ── list ──────────────────────────────────────────────────────────────


async def test_list_uses_calendar_view_and_expands_series(cal, tenant, exchange) -> None:
    exchange.add(OWNER, _meeting())
    exchange.add(
        OWNER,
        _meeting(
            subject="Weekly",
            start={"dateTime": "2026-10-05T09:00:00", "timeZone": "Eastern Standard Time"},
            end={"dateTime": "2026-10-05T09:15:00", "timeZone": "Eastern Standard Time"},
            recurrence={
                "pattern": {"type": "weekly", "interval": 1, "daysOfWeek": ["monday"]},
                "range": {"type": "numbered", "numberOfOccurrences": 10, "startDate": "2026-10-05"},
            },
        ),
    )
    listed = await cal.list(
        cal.resolve("operator"),
        {
            "time_min": "2026-10-01T00:00:00Z",
            "time_max": "2026-10-20T00:00:00-04:00",
            "single_events": True,
            "order_by": "startTime",
            "max_results": 3,
        },
    )
    summaries = [(e["summary"], e["start"]["dateTime"]) for e in listed["items"]]
    assert summaries == [
        ("Weekly", "2026-10-05T09:00:00-04:00"),
        ("Planning", "2026-10-08T10:00:00-04:00"),
        ("Weekly", "2026-10-12T09:00:00-04:00"),
    ]
    assert listed["items"][0]["recurringEventId"]
    (request,) = tenant.requests_matching("GET", f"/users/{OWNER}/calendar/calendarView")
    assert request.url.params["startDateTime"] == "2026-10-01T00:00:00+00:00"
    assert request.url.params["endDateTime"] == "2026-10-20T04:00:00+00:00"
    assert request.url.params["$top"] == "3"
    assert 'outlook.body-content-type="text"' in request.headers["prefer"]


async def test_list_pages_until_max_results(cal, tenant, exchange) -> None:
    for day in range(1, 8):
        exchange.add(
            ASSISTANT,
            _meeting(
                start={"dateTime": f"2026-10-0{day}T10:00:00", "timeZone": "UTC"},
                end={"dateTime": f"2026-10-0{day}T11:00:00", "timeZone": "UTC"},
            ),
        )
    listed = await cal.list(
        cal.resolve("own"),
        {"time_min": "2026-10-01T00:00:00Z", "time_max": "2026-10-31T00:00:00Z", "max_results": 5},
    )
    assert len(listed["items"]) == 5


async def test_explicit_calendar_id_addresses_that_calendar(cal, tenant) -> None:
    await cal.list(
        CalendarRef("AQMkShared", "other"), {"time_min": "2026-10-01T00:00:00Z", "max_results": 1}
    )
    assert tenant.requests_matching("GET", f"/users/{ASSISTANT}/calendars/AQMkShared/calendarView")


# ── create ────────────────────────────────────────────────────────────


async def test_create_writes_iana_zone_and_returns_immutable_id(cal, tenant, exchange) -> None:
    event = {
        "summary": "Planning",
        "description": "Agenda",
        "location": "Room 4",
        "start": {"dateTime": "2026-10-08T10:00:00"},
        "end": {"dateTime": "2026-10-08T11:00:00-04:00"},
        "attendees": [{"email": GUEST}, {"email": "b@example.com", "optional": True}],
    }
    created = await cal.create(cal.resolve("operator"), event, conference=False, send_updates="all")
    (post,) = tenant.requests_matching("POST", f"/users/{OWNER}/calendar/events")
    import json

    sent = json.loads(post.content)
    assert sent["subject"] == "Planning"
    assert sent["body"] == {"contentType": "text", "content": "Agenda"}
    assert sent["location"] == {"displayName": "Room 4"}
    assert sent["start"] == {"dateTime": "2026-10-08T10:00:00", "timeZone": "America/New_York"}
    assert sent["end"] == {"dateTime": "2026-10-08T11:00:00", "timeZone": "America/New_York"}
    assert sent["attendees"] == [
        {"emailAddress": {"address": GUEST}, "type": "required"},
        {"emailAddress": {"address": "b@example.com"}, "type": "optional"},
    ]
    assert "isOnlineMeeting" not in sent
    assert created["id"].startswith("AAkALg-imm-")
    assert created["start"]["dateTime"] == "2026-10-08T10:00:00-04:00"
    assert exchange.notifications[0]["kind"] == "invite"


async def test_create_teams_meeting(cal, tenant, exchange) -> None:
    created = await cal.create(
        cal.resolve("own"),
        {
            "summary": "Sync",
            "start": {"dateTime": "2026-10-08T10:00:00Z"},
            "end": {"dateTime": "2026-10-08T10:30:00Z"},
            "conferenceData": {
                "createRequest": {
                    "requestId": "r",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        },
        conference=True,
        send_updates=None,
    )
    import json

    (post,) = tenant.requests_matching("POST", f"/users/{ASSISTANT}/calendar/events")
    sent = json.loads(post.content)
    assert sent["isOnlineMeeting"] is True
    assert sent["onlineMeetingProvider"] == "teamsForBusiness"
    assert "conferenceData" not in sent
    assert created["conferenceData"]["entryPoints"][0]["uri"].startswith(
        "https://teams.microsoft.example/"
    )


async def test_attaching_an_existing_meet_link_is_unsupported(cal, tenant) -> None:
    with pytest.raises(Unsupported, match="Google Meet"):
        await cal.create(
            cal.resolve("own"),
            {
                "summary": "S",
                "start": {"dateTime": "2026-10-08T10:00:00Z"},
                "end": {"dateTime": "2026-10-08T10:30:00Z"},
                "hangoutLink": "https://meet.google.example/abc",
            },
            conference=False,
            send_updates=None,
        )
    assert not tenant.requests


@pytest.mark.parametrize("mode", ["none", "externalOnly"])
async def test_send_updates_other_than_all_with_attendees_is_refused(cal, tenant, mode) -> None:
    with pytest.raises(Unsupported, match="always"):
        await cal.create(
            cal.resolve("own"),
            {
                "summary": "S",
                "start": {"dateTime": "2026-10-08T10:00:00Z"},
                "end": {"dateTime": "2026-10-08T10:30:00Z"},
                "attendees": [{"email": GUEST}],
            },
            conference=False,
            send_updates=mode,
        )
    assert not tenant.requests


async def test_unsupported_recurrence_is_refused_before_any_write(cal, tenant) -> None:
    with pytest.raises(Unsupported):
        await cal.create(
            cal.resolve("own"),
            {
                "summary": "S",
                "start": {"dateTime": "2026-10-08T10:00:00Z"},
                "end": {"dateTime": "2026-10-08T10:30:00Z"},
                "recurrence": ["RRULE:FREQ=MINUTELY"],
            },
            conference=False,
            send_updates=None,
        )
    assert not tenant.requests


async def test_create_with_recurrence_and_all_day(cal, tenant) -> None:
    import json

    await cal.create(
        cal.resolve("own"),
        {
            "summary": "Holiday",
            "start": {"date": "2026-10-09"},
            "end": {"date": "2026-10-10"},
            "recurrence": ["RRULE:FREQ=YEARLY"],
        },
        conference=False,
        send_updates=None,
    )
    (post,) = tenant.requests_matching("POST", f"/users/{ASSISTANT}/calendar/events")
    sent = json.loads(post.content)
    assert sent["isAllDay"] is True
    assert sent["start"] == {"dateTime": "2026-10-09T00:00:00", "timeZone": "America/New_York"}
    assert sent["end"] == {"dateTime": "2026-10-10T00:00:00", "timeZone": "America/New_York"}
    assert sent["recurrence"]["pattern"]["type"] == "absoluteYearly"


async def test_create_falls_back_to_windows_zone_when_iana_is_rejected(
    cal, tenant, exchange
) -> None:
    import json

    exchange.reject_iana = True
    created = await cal.create(
        cal.resolve("own"),
        {
            "summary": "S",
            "start": {"dateTime": "2026-10-08T10:00:00", "timeZone": "Europe/Berlin"},
            "end": {"dateTime": "2026-10-08T11:00:00", "timeZone": "Europe/Berlin"},
        },
        conference=False,
        send_updates=None,
    )
    posts = tenant.requests_matching("POST", f"/users/{ASSISTANT}/calendar/events")
    assert len(posts) == 2
    assert json.loads(posts[1].content)["start"]["timeZone"] == "W. Europe Standard Time"
    assert created["start"]["dateTime"] == "2026-10-08T10:00:00+02:00"


# ── get / conditional_patch ───────────────────────────────────────────


async def test_conditional_patch_sends_if_match_and_maps_fields(cal, tenant, exchange) -> None:
    import json

    seeded = exchange.add(OWNER, _meeting())
    ref = cal.resolve("operator")
    before = await cal.get(ref, seeded["id"])
    after = await cal.conditional_patch(
        ref,
        seeded["id"],
        {
            "summary": "Moved",
            "description": "New notes",
            "location": "Room 5",
            "start": {"dateTime": "2026-10-08T15:00:00-04:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-10-08T15:30:00-04:00", "timeZone": "America/New_York"},
            "attendees": [
                {"email": GUEST, "responseStatus": "accepted"},
                {"email": "new@example.com"},
            ],
        },
        etag=before["etag"],
        send_updates="all",
    )
    (patch,) = tenant.requests_matching("PATCH", f"/users/{OWNER}/events/.*")
    assert patch.headers["if-match"] == before["etag"]
    sent = json.loads(patch.content)
    assert sent == {
        "subject": "Moved",
        "body": {"contentType": "text", "content": "New notes"},
        "location": {"displayName": "Room 5"},
        "start": {"dateTime": "2026-10-08T15:00:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-10-08T15:30:00", "timeZone": "America/New_York"},
        "attendees": [
            {"emailAddress": {"address": GUEST}, "type": "required"},
            {"emailAddress": {"address": "new@example.com"}, "type": "required"},
        ],
    }
    assert after["etag"] != before["etag"]
    assert after["summary"] == "Moved"
    # Exchange kept the existing guest's RSVP.
    assert after["attendees"][0]["responseStatus"] == "accepted"


async def test_stale_etag_raises_precondition_failed(cal, exchange) -> None:
    seeded = exchange.add(OWNER, _meeting())
    ref = cal.resolve("operator")
    with pytest.raises(PreconditionFailed):
        await cal.conditional_patch(
            ref, seeded["id"], {"summary": "X"}, etag='W/"stale"', send_updates="all"
        )


async def test_patch_to_all_day_toggles_is_all_day(cal, tenant, exchange) -> None:
    import json

    seeded = exchange.add(OWNER, _meeting())
    ref = cal.resolve("operator")
    before = await cal.get(ref, seeded["id"])
    await cal.conditional_patch(
        ref,
        seeded["id"],
        {
            "start": {"date": "2026-10-09", "dateTime": None},
            "end": {"date": "2026-10-10", "dateTime": None},
        },
        etag=before["etag"],
        send_updates="all",
    )
    (patch,) = tenant.requests_matching("PATCH", f"/users/{OWNER}/events/.*")
    sent = json.loads(patch.content)
    assert sent["isAllDay"] is True
    assert sent["start"] == {"dateTime": "2026-10-09T00:00:00", "timeZone": "America/New_York"}


async def test_patch_with_quiet_updates_on_a_meeting_is_refused(cal, tenant, exchange) -> None:
    seeded = exchange.add(OWNER, _meeting())
    ref = cal.resolve("operator")
    before = await cal.get(ref, seeded["id"])
    with pytest.raises(Unsupported, match="always"):
        await cal.conditional_patch(
            ref, seeded["id"], {"summary": "X"}, etag=before["etag"], send_updates="none"
        )
    assert not tenant.requests_matching("PATCH", ".*")


async def test_patch_of_unknown_field_is_unsupported(cal, tenant, exchange) -> None:
    seeded = exchange.add(OWNER, _meeting())
    with pytest.raises(Unsupported):
        await cal.conditional_patch(
            cal.resolve("operator"),
            seeded["id"],
            {"colorId": "5"},
            etag="x",
            send_updates="all",
        )
    assert not tenant.requests_matching("PATCH", ".*")


# ── rsvp / delete ─────────────────────────────────────────────────────


async def test_rsvp_posts_the_graph_action(cal, tenant, exchange) -> None:
    import json

    seeded = exchange.add(
        OWNER,
        _meeting(
            organizer={"emailAddress": {"address": GUEST}},
            attendees=[
                {
                    "type": "required",
                    "status": {"response": "none"},
                    "emailAddress": {"address": OWNER},
                }
            ],
        ),
    )
    ref = cal.resolve("operator")
    await cal.rsvp(ref, seeded["id"], "tentative", comment="maybe", send_response=True)
    (post,) = tenant.requests_matching("POST", f"/users/{OWNER}/events/.*/tentativelyAccept")
    assert json.loads(post.content) == {"sendResponse": True, "comment": "maybe"}
    after = await cal.get(ref, seeded["id"])
    assert after["attendees"][0] == {"email": OWNER, "responseStatus": "tentative", "self": True}
    assert exchange.notifications[-1]["to"] == [GUEST]


async def test_delete_meeting_as_organiser_cancels_with_notice(cal, tenant, exchange) -> None:
    seeded = exchange.add(OWNER, _meeting())
    out = await cal.delete(cal.resolve("operator"), seeded["id"], send_updates="all")
    assert out["method"] == "cancel"
    assert tenant.requests_matching("POST", f"/users/{OWNER}/events/.*/cancel")
    assert not tenant.requests_matching("DELETE", ".*")
    assert exchange.notifications == [
        {"kind": "cancel", "to": [GUEST], "event": seeded["id"], "comment": ""}
    ]


async def test_delete_without_attendees_is_a_plain_delete(cal, tenant, exchange) -> None:
    seeded = exchange.add(OWNER, _meeting(attendees=[]))
    out = await cal.delete(cal.resolve("operator"), seeded["id"], send_updates="none")
    assert out["method"] == "delete"
    assert tenant.requests_matching("DELETE", f"/users/{OWNER}/events/.*")
    assert exchange.notifications == []


async def test_delete_meeting_quietly_is_refused(cal, tenant, exchange) -> None:
    seeded = exchange.add(OWNER, _meeting())
    with pytest.raises(Unsupported, match="always"):
        await cal.delete(cal.resolve("operator"), seeded["id"], send_updates="none")
    assert not tenant.requests_matching("POST", ".*")
    assert not tenant.requests_matching("DELETE", ".*")


async def test_attendee_copy_is_deleted_not_cancelled(cal, tenant, exchange) -> None:
    seeded = exchange.add(OWNER, _meeting(organizer={"emailAddress": {"address": GUEST}}))
    out = await cal.delete(cal.resolve("operator"), seeded["id"], send_updates="all")
    assert out["method"] == "delete"
    assert not tenant.requests_matching("POST", ".*")
