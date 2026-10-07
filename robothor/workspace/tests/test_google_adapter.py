"""The Google adapter issues exactly the gws calls the handlers issued before it.

It is a pass-through: the CLI argv (JSON key order included), the timeout and
the returned dict are unchanged. The characterization goldens in
``robothor/engine/tests/test_gws_goldens.py`` pin the same thing end to end;
these pin each method on its own.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from robothor.engine import calendar_attendees
from robothor.engine.tools.handlers import gws
from robothor.workspace.google.adapter import (
    GoogleCalendar,
    GoogleCalendarSync,
    GoogleMail,
    GoogleMailSync,
)
from robothor.workspace.protocols import CalendarProvider, MailProvider
from robothor.workspace.query import parse_query
from robothor.workspace.types import CalendarRef


class Recorder:
    def __init__(self, reply: Any = None) -> None:
        self.calls: list[tuple[list[str], int]] = []
        self.reply = reply if reply is not None else {"ok": True}
        self.threads: list[str] = []

    def __call__(self, args: list[str], timeout: int = 30) -> Any:
        import threading

        self.threads.append(threading.current_thread().name)
        self.calls.append((list(args), timeout))
        return self.reply


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder()
    monkeypatch.setattr(gws, "_run_gws", r)
    return r


def test_protocol_conformance() -> None:
    assert isinstance(GoogleMail(), MailProvider)
    assert isinstance(GoogleCalendar(), CalendarProvider)


def test_search_passes_the_original_query_string(rec: Recorder) -> None:
    text = 'from:bob@example.com filename:pdf "Q3"'
    GoogleMailSync().search(parse_query(text), max_results=7)
    assert rec.calls == [
        (
            [
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps({"userId": "me", "q": text, "maxResults": 7}),
            ],
            30,
        )
    ]


def test_get_message_and_thread(rec: Recorder) -> None:
    mail = GoogleMailSync()
    mail.get_message("m1", fmt="metadata")
    mail.get_thread("t1", fmt="full")
    assert [c[0] for c in rec.calls] == [
        [
            "gmail",
            "users",
            "messages",
            "get",
            "--params",
            json.dumps({"userId": "me", "id": "m1", "format": "metadata"}),
        ],
        [
            "gmail",
            "users",
            "threads",
            "get",
            "--params",
            json.dumps({"userId": "me", "id": "t1", "format": "full"}),
        ],
    ]


def test_send_and_reply(rec: Recorder) -> None:
    mail = GoogleMailSync()
    mail.send("UkFX", thread_id=None)
    mail.send("UkFX", thread_id="")
    mail.send("UkFX", thread_id="t1")
    mail.reply("UkFX", thread_id="t2")
    base = ["gmail", "users", "messages", "send", "--params", '{"userId":"me"}', "--json"]
    assert rec.calls == [
        ([*base, json.dumps({"raw": "UkFX"})], 30),
        ([*base, json.dumps({"raw": "UkFX"})], 30),
        ([*base, json.dumps({"raw": "UkFX", "threadId": "t1"})], 30),
        ([*base, json.dumps({"raw": "UkFX", "threadId": "t2"})], 30),
    ]


def test_modify(rec: Recorder) -> None:
    GoogleMailSync().modify("m1", add_labels=["STARRED"], remove_labels=["UNREAD"])
    GoogleMailSync().modify("m1", add_labels=[], remove_labels=["INBOX"])
    assert [c[0][-1] for c in rec.calls] == [
        json.dumps({"addLabelIds": ["STARRED"], "removeLabelIds": ["UNREAD"]}),
        json.dumps({"removeLabelIds": ["INBOX"]}),
    ]
    assert rec.calls[0][0][:6] == [
        "gmail",
        "users",
        "messages",
        "modify",
        "--params",
        json.dumps({"userId": "me", "id": "m1"}),
    ]


def test_result_is_passed_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = {"error": "boom", "hint": "auth: gws is not signed in"}
    monkeypatch.setattr(gws, "_run_gws", Recorder(reply))
    assert GoogleMailSync().get_message("m1", fmt="full") is reply


def test_shapers_are_the_gmail_parsers() -> None:
    mail = GoogleMailSync()
    raw = {"id": "m1", "threadId": "t1", "payload": {"headers": []}}
    assert mail.shape_envelope(raw) == gws._shape_envelope(raw)
    assert mail.shape_message(raw, max_chars=10) == gws._shape_message(raw, max_chars=10)


# ── calendar ──────────────────────────────────────────────────────────


def test_resolve_own_and_operator() -> None:
    cal = GoogleCalendarSync()
    assert cal.resolve("own") == CalendarRef("primary", "own")
    assert cal.resolve("operator", address="alice@example.com") == CalendarRef(
        "alice@example.com", "operator", mailbox="alice@example.com"
    )
    with pytest.raises(ValueError):
        cal.resolve("operator")


def test_list_keeps_the_callers_key_order(rec: Recorder) -> None:
    cal = GoogleCalendarSync()
    ref = CalendarRef("alice@example.com", "operator")
    cal.list(
        ref,
        {"time_min": "a", "single_events": True, "order_by": "startTime", "max_results": 5},
    )
    cal.list(
        ref,
        {
            "time_min": "a",
            "time_max": "b",
            "single_events": True,
            "order_by": "startTime",
            "max_results": 250,
        },
    )
    assert [c[0][-1] for c in rec.calls] == [
        json.dumps(
            {
                "calendarId": "alice@example.com",
                "timeMin": "a",
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 5,
            }
        ),
        json.dumps(
            {
                "calendarId": "alice@example.com",
                "timeMin": "a",
                "timeMax": "b",
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 250,
            }
        ),
    ]


def test_create(rec: Recorder) -> None:
    cal = GoogleCalendarSync()
    event = {"summary": "S", "start": {"dateTime": "x"}, "end": {"dateTime": "y"}}
    cal.create(CalendarRef("primary", "own"), event, conference=True, send_updates="all")
    cal.create(CalendarRef("primary", "own"), event, conference=False, send_updates=None)
    assert [c[0] for c in rec.calls] == [
        [
            "calendar",
            "events",
            "insert",
            "--params",
            json.dumps({"calendarId": "primary", "conferenceDataVersion": 1, "sendUpdates": "all"}),
            "--json",
            json.dumps(event),
        ],
        [
            "calendar",
            "events",
            "insert",
            "--params",
            json.dumps({"calendarId": "primary"}),
            "--json",
            json.dumps(event),
        ],
    ]


def test_delete(rec: Recorder) -> None:
    GoogleCalendarSync().delete(CalendarRef("primary", "own"), "e1", send_updates="none")
    assert rec.calls == [
        (
            [
                "calendar",
                "events",
                "delete",
                "--params",
                json.dumps({"calendarId": "primary", "eventId": "e1", "sendUpdates": "none"}),
            ],
            30,
        )
    ]


class FakeTransport:
    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1
        self.requests: list[tuple] = []
        self.closed = False

    def __enter__(self) -> FakeTransport:
        return self

    def __exit__(self, *args: Any) -> None:
        self.closed = True

    def request(self, method: str, calendar_id: str, event_id: str, **kwargs: Any) -> Any:
        self.requests.append((method, calendar_id, event_id, kwargs))
        return {"id": event_id, "etag": '"v1"'}


def test_session_shares_one_transport_and_uses_the_patchable_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeTransport()
    monkeypatch.setattr(calendar_attendees, "CalendarTransport", lambda: fake)
    ref = CalendarRef("alice@example.com", "operator")
    with GoogleCalendarSync().session() as api:
        api.get(ref, "e1")
        api.conditional_patch(ref, "e1", {"summary": "x"}, etag='"v1"', send_updates="all")
    assert fake.closed
    assert fake.requests == [
        ("GET", "alice@example.com", "e1", {}),
        (
            "PATCH",
            "alice@example.com",
            "e1",
            {"body": {"summary": "x"}, "etag": '"v1"', "send_updates": "all"},
        ),
    ]


async def test_async_facade_runs_the_cli_off_the_event_loop(rec: Recorder) -> None:
    import threading

    loop_thread = threading.current_thread().name
    await GoogleMail().get_message("m1", fmt="full")
    await GoogleCalendar().delete(CalendarRef("primary", "own"), "e1", send_updates="all")
    assert len(rec.calls) == 2
    assert loop_thread not in rec.threads


async def test_async_get_and_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTransport()
    monkeypatch.setattr(calendar_attendees, "CalendarTransport", lambda: fake)
    ref = CalendarRef("primary", "own")
    cal = GoogleCalendar()
    got = await cal.get(ref, "e1")
    await cal.conditional_patch(ref, "e1", {"summary": "x"}, etag='"v1"', send_updates="none")
    assert got == {"id": "e1", "etag": '"v1"'}
    assert [r[0] for r in fake.requests] == ["GET", "PATCH"]


async def test_async_respond_delegates_to_the_guarded_rsvp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def respond(calendar: Any, event_id: str, response: str, **kwargs: Any) -> dict:
        seen.update(calendar=calendar, event_id=event_id, response=response, **kwargs)
        return {"status": "updated"}

    monkeypatch.setattr(calendar_attendees, "respond", respond)
    cal = GoogleCalendar()
    ref = CalendarRef("primary", "own")
    out = await cal.respond(ref, "e1", "accepted", screen=lambda *a: None, send_updates="all")
    assert out == {"status": "updated"}
    assert seen["calendar"] == ref
    assert seen["provider"] is cal
    assert seen["send_updates"] == "all"


def test_async_facade_exposes_the_blocking_twin() -> None:
    assert isinstance(GoogleMail().blocking, GoogleMailSync)
    assert isinstance(GoogleCalendar().blocking, GoogleCalendarSync)
    assert asyncio.iscoroutinefunction(GoogleMail().search)
