"""Characterization goldens for every ``gws_*`` tool path, as Google runs today.

The Workspace tools are about to be moved behind a provider seam (Google today,
Microsoft 365 next). The safety rules live around the transport calls, not in
them, so a refactor that changes which CLI call is issued, in what order, with
what parameters, or what comes back, has changed behaviour — even when every
targeted test still passes. These goldens pin all of it:

* every ``_run_gws`` argv, exactly (the JSON strings inside it included), with
  ``--params`` / ``--json`` decoded alongside, and an outbound MIME message
  decoded into headers and body so a diff is readable;
* every conditional-request the calendar edits make (``CalendarTransport``);
* every do-not-contact lookup, guardrail event and scheduling-policy lookup;
* the CRM write-through: the arguments ``_record_sent_email`` and
  ``_record_calendar_event`` receive and the SQL statements they execute;
* the dict the tool returns.

Calls go through the registered handlers in ``gws.HANDLERS`` with a real
``ToolContext``, which is the path the engine's dispatcher takes.

Regenerate after an INTENDED behaviour change with ``GWS_GOLDEN_REGEN=1`` and
review the diff like code. Without it, a mismatch fails with a unified diff.

Nothing here reaches Google, a database or the ``gws`` CLI. Every address is a
generic fixture.
"""

from __future__ import annotations

import base64
import difflib
import email
import json
import os
import re
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from robothor.engine import calendar_attendees
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import gws as gws_handlers

GOLDEN_DIR = Path(__file__).parent / "golden" / "gws"
REGEN = os.environ.get("GWS_GOLDEN_REGEN") == "1"

OPERATOR = "alice@example.com"
ASSISTANT = "agent@example.com"
TENANT = "tenant-a"
RUN_ID = "run-golden"
#: What ``robothor.constants.DEFAULT_TENANT`` is pinned to while recording, so
#: the write-through's choice of tenant is visible (it does not use the call's).
DEFAULT_TENANT_PLACEHOLDER = "tenant-default"

#: Resolved CRM people, for the write-through's participant/timeline rows.
KNOWN_PEOPLE = {"bob@example.com": "person-bob"}


# ── Recorded Gmail fixtures ───────────────────────────────────────────


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _headers(**pairs: str) -> list[dict[str, str]]:
    return [{"name": k.replace("_", "-").title(), "value": v} for k, v in pairs.items()]


PLAIN_MESSAGE: dict[str, Any] = {
    "id": "msg-plain",
    "threadId": "thread-1",
    "labelIds": ["INBOX", "UNREAD"],
    "snippet": "The Q3 numbers are attached",
    "payload": {
        "mimeType": "text/plain",
        "headers": _headers(
            From="Bob <bob@example.com>",
            To="Agent <agent@example.com>",
            Cc="carol@example.com",
            Subject="Q3 numbers",
            Date="Tue, 16 Sep 2026 09:00:00 +0000",
            Message_Id="<plain-1@example.com>",
        ),
        "body": {"data": _b64("The Q3 numbers are attached.\nRegards,\nBob")},
    },
}

HTML_ONLY_MESSAGE: dict[str, Any] = {
    "id": "msg-html",
    "threadId": "thread-2",
    "labelIds": ["INBOX"],
    "snippet": "Invoice 42 is ready",
    "payload": {
        "mimeType": "text/html",
        "headers": _headers(
            From="billing@example.com",
            To="agent@example.com",
            Subject="Invoice 42",
            Date="Tue, 16 Sep 2026 10:00:00 +0000",
        ),
        "body": {
            "data": _b64(
                "<html><head><style>p{color:red}</style></head><body>"
                "<p>Invoice <b>42</b> is ready.</p>"
                "<script>alert(1)</script>"
                "<div>Total&nbsp;&pound;120</div></body></html>"
            )
        },
    },
}

MULTIPART_WITH_ATTACHMENT: dict[str, Any] = {
    "id": "msg-multi",
    "threadId": "thread-3",
    "labelIds": ["INBOX"],
    "snippet": "See attached",
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": _headers(
            From="bob@example.com",
            To="agent@example.com",
            Subject="Contract",
            Date="Tue, 16 Sep 2026 11:00:00 +0000",
        ),
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("See attached contract.")}},
                    {
                        "mimeType": "text/html",
                        "body": {"data": _b64("<p>See attached contract.</p>")},
                    },
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "contract.pdf",
                "body": {"attachmentId": "att-1", "size": 20481},
            },
            {
                "mimeType": "image/png",
                "filename": "chart.png",
                "body": {"attachmentId": "att-2", "size": 512},
            },
        ],
    },
}

HUGE_BODY_MESSAGE: dict[str, Any] = {
    "id": "msg-huge",
    "threadId": "thread-5",
    "labelIds": ["INBOX"],
    "snippet": "A very long newsletter",
    "payload": {
        "mimeType": "text/plain",
        "headers": _headers(
            From="news@example.com",
            To="agent@example.com",
            Subject="Newsletter",
            Date="Wed, 17 Sep 2026 08:00:00 +0000",
        ),
        "body": {"data": _b64("".join(f"line {n:05d} of the newsletter\n" for n in range(800)))},
    },
}


def _thread_message(mid: str, frm: str, to: str, subject: str, *, cc: str = "", msgid: str = ""):
    headers = {"From": frm, "To": to, "Subject": subject}
    if cc:
        headers["Cc"] = cc
    if msgid:
        headers["Message-ID"] = msgid
    return {
        "id": mid,
        "threadId": "thread-9",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "body": {"data": _b64(f"body of {mid}")},
        },
    }


#: Bob wrote to the assistant, cc Carol; the assistant answered; Bob came back.
THREAD_FROM_BOB: dict[str, Any] = {
    "id": "thread-9",
    "messages": [
        _thread_message(
            "m1",
            "Bob <bob@example.com>",
            "agent@example.com",
            "Kickoff",
            cc="Carol <carol@example.com>",
            msgid="<m1@example.com>",
        ),
        _thread_message(
            "m2",
            "Agent <agent@example.com>",
            "bob@example.com",
            "Re: Kickoff",
            cc="carol@example.com",
            msgid="<m2@example.com>",
        ),
        _thread_message(
            "m3",
            "Bob <bob@example.com>",
            "agent@example.com, carol@example.com",
            "Kickoff",
            msgid="<m3@example.com>",
        ),
    ],
}

THREAD_ALREADY_RE: dict[str, Any] = {
    "id": "thread-9",
    "messages": [
        _thread_message(
            "m1", "Bob <bob@example.com>", "agent@example.com", "RE: Budget", msgid="<b1@example.com>"
        )
    ],
}

#: The last message is the assistant's own — a second reply would be a duplicate.
THREAD_LAST_FROM_BOT: dict[str, Any] = {
    "id": "thread-9",
    "messages": [
        _thread_message("m1", "bob@example.com", "agent@example.com", "Hello"),
        _thread_message("m2", "Agent <AGENT@example.com>", "bob@example.com", "Re: Hello"),
    ],
}


def _search_router(ids: list[str], messages: dict[str, Any]) -> dict[str, Any]:
    return {
        "gmail users messages list": {
            "messages": [{"id": i, "threadId": f"t-{i}"} for i in ids],
            "resultSizeEstimate": len(ids),
        },
        "gmail users messages get": lambda params, body: messages[params["id"]],
    }


SENT = {"id": "sent-1", "threadId": "thread-9", "labelIds": ["SENT"]}
SENT_NEW_THREAD = {"id": "sent-2", "threadId": "sent-2", "labelIds": ["SENT"]}


# ── Calendar fixtures ─────────────────────────────────────────────────


def _existing_event(**overrides: Any) -> dict[str, Any]:
    event = {
        "id": "evt-existing",
        "status": "confirmed",
        "summary": "Planning",
        "htmlLink": "https://calendar.example.test/evt-existing",
        "start": {"dateTime": "2026-10-08T10:00:00-04:00"},
        "end": {"dateTime": "2026-10-08T10:30:00-04:00"},
        "attendees": [{"email": "bob@example.com", "responseStatus": "accepted"}],
    }
    event.update(overrides)
    return event


def _insert_echo(params: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """What Google returns for an insert: the event as stored, with an id."""
    out: dict[str, Any] = {
        "id": "evt-new",
        "status": "confirmed",
        "htmlLink": "https://calendar.example.test/evt-new",
        "summary": body["summary"],
        "start": body["start"],
        "end": body["end"],
        "organizer": {"email": params["calendarId"], "self": True},
        "creator": {"email": ASSISTANT},
    }
    for key in ("description", "location"):
        if key in body:
            out[key] = body[key]
    if body.get("attendees"):
        out["attendees"] = [
            {"email": a["email"], "responseStatus": "needsAction"} for a in body["attendees"]
        ]
    if "conferenceData" in body:
        out["hangoutLink"] = "https://meet.example.test/abc-defg-hij"
    return out


def _listing(*events: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "calendar#events", "summary": "calendar", "items": list(events)}


def _google_echo(value: dict[str, Any]) -> dict[str, Any]:
    """What Google stores and echoes for a time: nulls cleared, a naive time
    localised in its ``timeZone`` and returned with an explicit offset."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    out = {k: v for k, v in value.items() if v is not None}
    if "dateTime" in out:
        moment = datetime.fromisoformat(out["dateTime"])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(out["timeZone"]))
            out["dateTime"] = moment.isoformat()
    return out


class FakeCalendar:
    """In-memory stand-in for ``calendar_attendees.CalendarTransport``.

    ``patch_failures`` is consumed one entry per PATCH: a dict is returned as
    that PATCH's failure, ``None`` lets it succeed. ``after_get`` runs after
    every GET (used to flip a cancellation flag between read and write).
    """

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.event: dict[str, Any] = {
            "id": "meeting",
            "etag": '"v1"',
            "status": "confirmed",
            "summary": "Planning",
            "htmlLink": "https://calendar.example.test/meeting",
            "attendees": [
                {"email": OPERATOR, "responseStatus": "needsAction"},
                {"email": "Bob@Example.com", "responseStatus": "accepted", "optional": True},
            ],
            "start": {"dateTime": "2026-10-08T10:00:00-04:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-10-08T10:30:00-04:00", "timeZone": "America/New_York"},
            "organizer": {"email": "carol@example.com"},
        }
        self.patch_failures: list[dict[str, Any] | None] = []
        self.version = 1
        self.after_get: Any = None

    def __enter__(self) -> FakeCalendar:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def request(self, method: str, calendar_id: str, event_id: str, **kwargs: Any) -> Any:
        self.harness.calendar_requests.append(
            {"method": method, "calendar_id": calendar_id, "event_id": event_id, **kwargs}
        )
        if method == "GET":
            out = deepcopy(self.event)
            if self.after_get is not None:
                self.after_get()
            return out
        assert method == "PATCH"
        assert kwargs["etag"] == self.event["etag"], "every write is conditional"
        failure = self.patch_failures.pop(0) if self.patch_failures else None
        if failure is not None:
            if failure.get("status_code") == 412:
                # Someone else edited it in between: the version moves on.
                self.version += 1
                self.event["etag"] = f'"v{self.version}"'
            return deepcopy(failure)
        body = deepcopy(kwargs["body"])
        for key in ("start", "end"):
            if key in body:
                self.event[key] = _google_echo(body.pop(key))
        self.event.update(body)
        self.version += 1
        self.event["etag"] = f'"v{self.version}"'
        return deepcopy(self.event)


# ── The CRM boundary ──────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self._last = ""

    def execute(self, sql: str, params: Any = None) -> None:
        self._last = " ".join(sql.split())
        self.harness.crm_sql.append({"sql": self._last, "params": list(params or ())})

    def fetchone(self) -> Any:
        if "RETURNING id, (xmax = 0) AS inserted" in self._last:
            return ("crm-message-1", True)
        if "INTO message_thread" in self._last:
            return ("crm-thread-1",)
        if "INTO calendar_event" in self._last:
            return ("crm-event-1",)
        return None


class _FakeConnection:
    def __init__(self, harness: Harness) -> None:
        self.harness = harness

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.harness)

    def commit(self) -> None:
        self.harness.crm_sql.append({"commit": True})


# ── Harness ───────────────────────────────────────────────────────────


@dataclass
class Harness:
    gws_router: dict[str, Any] = field(default_factory=dict)
    gws_calls: list[dict[str, Any]] = field(default_factory=list)
    calendar_requests: list[dict[str, Any]] = field(default_factory=list)
    dnc_lookups: list[dict[str, Any]] = field(default_factory=list)
    guardrail_events: list[dict[str, Any]] = field(default_factory=list)
    policy_lookups: list[list[str]] = field(default_factory=list)
    crm_calls: list[dict[str, Any]] = field(default_factory=list)
    crm_sql: list[dict[str, Any]] = field(default_factory=list)
    dnc_flagged: set[str] = field(default_factory=set)
    dnc_raises: BaseException | None = None
    dnc_mode: str = "enforce"
    policies: dict[str, str] = field(default_factory=dict)
    cancelled: bool = False
    calendar: FakeCalendar | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def run_gws(self, args: list[str], timeout: int = 30) -> Any:
        verb = []
        for a in args:
            if a.startswith("--"):
                break
            verb.append(a)
        key = " ".join(verb)
        params = json.loads(args[args.index("--params") + 1]) if "--params" in args else None
        body = json.loads(args[args.index("--json") + 1]) if "--json" in args else None
        record: dict[str, Any] = {"argv": list(args), "timeout": timeout}
        if params is not None:
            record["params"] = params
        if body is not None:
            record["json"] = _decode_raw(body)
        with self.lock:
            self.gws_calls.append(record)
        handler = self.gws_router.get(key)
        if handler is None:
            raise AssertionError(f"unscripted gws call: {args}")
        return deepcopy(handler(params, body) if callable(handler) else handler)


def _decode_raw(body: dict[str, Any]) -> dict[str, Any]:
    """A ``raw`` RFC 2822 message decoded next to the opaque base64 string."""
    if not isinstance(body, dict) or "raw" not in body:
        return body
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
    payload = msg.get_payload(decode=True)
    return {
        **body,
        "raw_decoded": {
            "headers": [[k, v] for k, v in msg.items()],
            "body": payload.decode(msg.get_content_charset() or "utf-8") if payload else "",
        },
    }


class _Cancellation:
    """Stand-in for ``calendar_attendees.OperationCancellation``, driven by the harness."""

    harness: Harness

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id

    def set(self) -> None:
        type(self).harness.cancelled = True

    def is_set(self) -> bool:
        return type(self).harness.cancelled


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Harness:
    import yaml

    h = Harness()

    owner = tmp_path / "owner.yaml"
    owner.write_text(
        yaml.safe_dump({"tenant_id": "fixture", "first_name": "Alice", "email": OPERATOR})
    )
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(owner))
    monkeypatch.delenv("ROBOTHOR_OWNER_EMAIL", raising=False)
    monkeypatch.setattr(gws_handlers, "ROBOTHOR_EMAIL", ASSISTANT)
    monkeypatch.setattr(gws_handlers, "_run_gws", h.run_gws)

    # Governed flags, at their defaults, without a flag store.
    monkeypatch.setattr(
        "robothor.engine.feature_flags.calendar_send_updates", lambda: "all", raising=True
    )
    monkeypatch.setattr(
        "robothor.engine.feature_flags.do_not_contact_mode", lambda: h.dnc_mode, raising=True
    )

    # Do-not-contact list and the guardrail-event sink.
    def _dnc(addresses: list[str], tenant_id: str = "default") -> set[str]:
        h.dnc_lookups.append({"addresses": list(addresses), "tenant_id": tenant_id})
        if h.dnc_raises is not None:
            raise h.dnc_raises
        return {a for a in addresses if a in h.dnc_flagged}

    monkeypatch.setattr("robothor.crm.dal.do_not_contact_emails", _dnc)

    def _event(run_id: str, guard: str, action: str, **kwargs: Any) -> None:
        h.guardrail_events.append({"run_id": run_id, "guard": guard, "action": action, **kwargs})

    monkeypatch.setattr("robothor.engine.tracking.log_guardrail_event", _event)

    def _policies(emails: list[str]) -> dict[str, str]:
        h.policy_lookups.append(list(emails))
        return {e: p for e, p in h.policies.items() if e in emails}

    monkeypatch.setattr("robothor.engine.guardrails._lookup_scheduling_policies", _policies)

    # CRM write-through: capture the arguments, then run the real writer
    # against a recording connection.
    real_sent = gws_handlers._record_sent_email
    real_event = gws_handlers._record_calendar_event

    def _sent(**kwargs: Any) -> None:
        h.crm_calls.append({"fn": "_record_sent_email", "kwargs": deepcopy(kwargs)})
        real_sent(**kwargs)

    def _cal(**kwargs: Any) -> None:
        h.crm_calls.append({"fn": "_record_calendar_event", "kwargs": deepcopy(kwargs)})
        real_event(**kwargs)

    monkeypatch.setattr(gws_handlers, "_record_sent_email", _sent)
    monkeypatch.setattr(gws_handlers, "_record_calendar_event", _cal)
    monkeypatch.setattr("robothor.db.connection.get_connection", lambda: _FakeConnection(h))
    monkeypatch.setattr(gws_handlers, "_resolve_person_by_email", KNOWN_PEOPLE.get)
    monkeypatch.setattr("robothor.constants.DEFAULT_TENANT", DEFAULT_TENANT_PLACEHOLDER)

    # Calendar edits: in-memory transport, harness-driven cancellation.
    h.calendar = FakeCalendar(h)
    monkeypatch.setattr(calendar_attendees, "CalendarTransport", lambda: h.calendar)
    _Cancellation.harness = h
    monkeypatch.setattr(calendar_attendees, "OperationCancellation", _Cancellation)
    return h


# ── Scenarios ─────────────────────────────────────────────────────────


@dataclass
class Scenario:
    name: str
    tool: str
    args: dict[str, Any]
    router: dict[str, Any] = field(default_factory=dict)
    setup: Any = None
    tenant_id: str = TENANT
    is_benchmark: bool = False
    #: Calls after the first are issued concurrently (search metadata fetches);
    #: their order is not behaviour, so they are compared sorted.
    unordered_tail: bool = False


def _flag(*addresses: str):
    def setup(h: Harness) -> None:
        h.dnc_flagged = set(addresses)

    return setup


def _observe(*addresses: str):
    def setup(h: Harness) -> None:
        h.dnc_flagged = set(addresses)
        h.dnc_mode = "observe"

    return setup


def _lookup_fails(h: Harness) -> None:
    h.dnc_raises = RuntimeError("database unreachable")


def _no_auto(address: str):
    def setup(h: Harness) -> None:
        h.policies = {address: "no_auto"}

    return setup


def _patch_failures(*failures: dict[str, Any] | None):
    def setup(h: Harness) -> None:
        assert h.calendar is not None
        h.calendar.patch_failures = list(failures)

    return setup


def _already_cancelled(h: Harness) -> None:
    h.cancelled = True


def _cancelled_after_read(h: Harness) -> None:
    def flip() -> None:
        h.cancelled = True

    assert h.calendar is not None
    h.calendar.after_get = flip


def _guest_view(h: Harness) -> None:
    """The operator's copy of an invitation from Carol, flagged ``self``."""
    assert h.calendar is not None
    h.calendar.event["attendees"][0]["self"] = True


def _already_accepted(h: Harness) -> None:
    assert h.calendar is not None
    h.calendar.event["attendees"][0]["responseStatus"] = "accepted"


CONFLICT = {"error": "Calendar HTTP 412", "status_code": 412}

REPLY_ROUTER = {"gmail users threads get": THREAD_FROM_BOB, "gmail users messages send": SENT}
SEND_ROUTER = {"gmail users messages send": SENT_NEW_THREAD}
CREATE_ROUTER = {"calendar events list": _listing(), "calendar events insert": _insert_echo}
CREATE_ARGS = {
    "summary": "Quarterly review",
    "start": "2026-10-09T14:00:00-04:00",
    "end": "2026-10-09T15:00:00-04:00",
    "attendees": ["bob@example.com"],
    "description": "Numbers and next steps",
    "location": "Room 4",
}

GMAIL_SCENARIOS = [
    Scenario(
        "gmail_search_normal",
        "gws_gmail_search",
        {"query": "is:unread from:bob@example.com", "max_results": 5},
        _search_router(
            ["msg-plain", "msg-html", "msg-multi"],
            {
                "msg-plain": PLAIN_MESSAGE,
                "msg-html": HTML_ONLY_MESSAGE,
                "msg-multi": MULTIPART_WITH_ATTACHMENT,
            },
        ),
        unordered_tail=True,
    ),
    Scenario(
        "gmail_search_truncated_to_fit",
        "gws_gmail_search",
        {"query": "label:newsletters", "max_results": 25},
        _search_router(
            [f"id-{n:02d}" for n in range(25)],
            {
                f"id-{n:02d}": {
                    **PLAIN_MESSAGE,
                    "id": f"id-{n:02d}",
                    "snippet": f"{n} " + "y" * 900,
                }
                for n in range(25)
            },
        ),
        unordered_tail=True,
    ),
    Scenario(
        "gmail_search_max_results_capped",
        "gws_gmail_search",
        {"query": "x", "max_results": 500},
        {"gmail users messages list": {}},
    ),
    Scenario(
        "gmail_search_empty",
        "gws_gmail_search",
        {"query": "from:nobody@example.com"},
        {"gmail users messages list": {"resultSizeEstimate": 0}},
    ),
    Scenario(
        "gmail_search_one_fetch_fails",
        "gws_gmail_search",
        {"query": "x"},
        {
            "gmail users messages list": {"messages": [{"id": "good"}, {"id": "bad"}]},
            "gmail users messages get": lambda p, b: (
                {**PLAIN_MESSAGE, "id": "good"}
                if p["id"] == "good"
                else {"error": "Requested entity was not found.", "hint": "not_found"}
            ),
        },
        unordered_tail=True,
    ),
    Scenario(
        "gmail_get_message_plain",
        "gws_gmail_get",
        {"message_id": "msg-plain"},
        {"gmail users messages get": PLAIN_MESSAGE},
    ),
    Scenario(
        "gmail_get_message_html",
        "gws_gmail_get",
        {"message_id": "msg-html"},
        {"gmail users messages get": HTML_ONLY_MESSAGE},
    ),
    Scenario(
        "gmail_get_message_metadata",
        "gws_gmail_get",
        {"message_id": "msg-plain", "format": "metadata"},
        {"gmail users messages get": PLAIN_MESSAGE},
    ),
    Scenario(
        "gmail_get_message_attachments",
        "gws_gmail_get",
        {"message_id": "msg-multi"},
        {"gmail users messages get": MULTIPART_WITH_ATTACHMENT},
    ),
    Scenario(
        "gmail_get_message_huge_body",
        "gws_gmail_get",
        {"message_id": "msg-huge"},
        {"gmail users messages get": HUGE_BODY_MESSAGE},
    ),
    Scenario(
        "gmail_get_message_max_chars",
        "gws_gmail_get",
        {"message_id": "msg-plain", "max_chars": 10},
        {"gmail users messages get": PLAIN_MESSAGE},
    ),
    Scenario(
        "gmail_get_thread",
        "gws_gmail_get",
        {"thread_id": "thread-9"},
        {"gmail users threads get": THREAD_FROM_BOB},
    ),
    Scenario(
        "gmail_get_thread_metadata",
        "gws_gmail_get",
        {"thread_id": "thread-9", "format": "minimal"},
        {"gmail users threads get": THREAD_FROM_BOB},
    ),
    Scenario("gmail_get_no_id", "gws_gmail_get", {}),
    Scenario("gmail_get_bad_format", "gws_gmail_get", {"message_id": "m", "format": "raw"}),
    Scenario(
        "gmail_get_cli_error",
        "gws_gmail_get",
        {"message_id": "gone"},
        {"gmail users messages get": {"error": "Requested entity was not found.", "hint": "x"}},
    ),
    Scenario(
        "gmail_send_plain",
        "gws_gmail_send",
        {
            "to": "Bob <bob@example.com>",
            "cc": "carol@example.com",
            "subject": "Proposal",
            "body": "Hi Bob,\nThe proposal is attached.\n",
        },
        SEND_ROUTER,
    ),
    Scenario(
        "gmail_send_threaded",
        "gws_gmail_send",
        {
            "to": "bob@example.com",
            "subject": "Re: Kickoff",
            "body": "Following up.",
            "thread_id": "thread-9",
            "in_reply_to": "<m3@example.com>",
        },
        {"gmail users threads get": THREAD_FROM_BOB, "gmail users messages send": SENT},
    ),
    Scenario(
        "gmail_send_threaded_duplicate_skipped",
        "gws_gmail_send",
        {
            "to": "bob@example.com",
            "subject": "Re: Hello",
            "body": "Again.",
            "thread_id": "thread-9",
        },
        {"gmail users threads get": THREAD_LAST_FROM_BOT},
    ),
    Scenario(
        "gmail_send_html",
        "gws_gmail_send",
        {
            "to": "bob@example.com",
            "subject": "Report",
            "body": "<p>Hello <b>Bob</b></p>",
            "content_type": "html",
        },
        SEND_ROUTER,
    ),
    Scenario(
        "gmail_send_html_autodetected",
        "gws_gmail_send",
        {
            "to": "bob@example.com",
            "subject": "Report",
            "body": "<!DOCTYPE html><html><body>Hi</body></html>",
        },
        SEND_ROUTER,
    ),
    Scenario(
        "gmail_send_re_without_thread_warns",
        "gws_gmail_send",
        {"to": "bob@example.com", "subject": "Re: Kickoff", "body": "Answer."},
        SEND_ROUTER,
    ),
    Scenario(
        "gmail_send_unicode_body",
        "gws_gmail_send",
        {"to": "bob@example.com", "subject": "Café", "body": "Grüße — see you at 9 €"},
        SEND_ROUTER,
    ),
    Scenario(
        "gmail_send_cli_error",
        "gws_gmail_send",
        {"to": "bob@example.com", "subject": "S", "body": "B"},
        {"gmail users messages send": {"error": "quota exceeded", "hint": "rate_limited"}},
    ),
    Scenario(
        "gmail_reply_all",
        "gws_gmail_reply",
        {"thread_id": "thread-9", "body": "Thanks, Bob.", "cc": "Dave <dave@example.com>"},
        REPLY_ROUTER,
    ),
    Scenario(
        "gmail_reply_keeps_existing_re_prefix",
        "gws_gmail_reply",
        {"thread_id": "thread-9", "body": "Noted."},
        {"gmail users threads get": THREAD_ALREADY_RE, "gmail users messages send": SENT},
    ),
    Scenario(
        "gmail_reply_duplicate_skipped",
        "gws_gmail_reply",
        {"thread_id": "thread-9", "body": "Again."},
        {"gmail users threads get": THREAD_LAST_FROM_BOT},
    ),
    Scenario("gmail_reply_missing_thread_id", "gws_gmail_reply", {"body": "x"}),
    Scenario("gmail_reply_missing_body", "gws_gmail_reply", {"thread_id": "thread-9"}),
    Scenario(
        "gmail_reply_empty_thread",
        "gws_gmail_reply",
        {"thread_id": "thread-9", "body": "x"},
        {"gmail users threads get": {"id": "thread-9", "messages": []}},
    ),
    Scenario(
        "gmail_modify",
        "gws_gmail_modify",
        {"message_id": "msg-plain", "add_labels": ["STARRED"], "remove_labels": ["UNREAD"]},
        {"gmail users messages modify": {"id": "msg-plain", "labelIds": ["INBOX", "STARRED"]}},
    ),
    Scenario("gmail_modify_nothing_to_do", "gws_gmail_modify", {"message_id": "msg-plain"}),
    Scenario("gmail_modify_missing_id", "gws_gmail_modify", {"add_labels": ["STARRED"]}),
]

CALENDAR_SCENARIOS = [
    Scenario(
        "calendar_list_operator_default",
        "gws_calendar_list",
        {"time_min": "2026-10-08T00:00:00Z", "time_max": "2026-10-09T00:00:00Z"},
        {"calendar events list": _listing(_existing_event())},
    ),
    Scenario(
        "calendar_list_own",
        "gws_calendar_list",
        {"time_min": "2026-10-08T00:00:00Z", "calendar": "own", "max_results": "7"},
        {"calendar events list": _listing()},
    ),
    Scenario(
        "calendar_list_explicit_primary",
        "gws_calendar_list",
        {"time_min": "2026-10-08T00:00:00Z", "calendar_id": "primary", "max_results": 900},
        {"calendar events list": _listing()},
    ),
    Scenario(
        "calendar_list_explicit_other",
        "gws_calendar_list",
        {"time_min": "2026-10-08T00:00:00Z", "calendar_id": "team@example.com"},
        {"calendar events list": _listing()},
    ),
    Scenario(
        "calendar_list_invalid_calendar",
        "gws_calendar_list",
        {"time_min": "2026-10-08T00:00:00Z", "calendar": "primary"},
    ),
    Scenario("calendar_list_missing_time_min", "gws_calendar_list", {}),
    Scenario("calendar_create_operator", "gws_calendar_create", CREATE_ARGS, CREATE_ROUTER),
    Scenario(
        "calendar_create_own_adds_operator",
        "gws_calendar_create",
        {**CREATE_ARGS, "calendar": "own"},
        CREATE_ROUTER,
    ),
    Scenario(
        "calendar_create_no_attendees_no_meet",
        "gws_calendar_create",
        {
            "summary": "Flight to Lisbon",
            "start": "2026-10-10T08:00:00+01:00",
            "end": "2026-10-10T10:30:00+01:00",
            "with_meet": False,
        },
        CREATE_ROUTER,
    ),
    Scenario(
        "calendar_create_dedup_hit",
        "gws_calendar_create",
        CREATE_ARGS,
        {
            "calendar events list": _listing(
                _existing_event(
                    id="evt-dup",
                    summary="Quarterly Review!",
                    start={"dateTime": "2026-10-09T18:00:00Z"},
                    htmlLink="https://calendar.example.test/evt-dup",
                )
            )
        },
    ),
    Scenario(
        "calendar_create_dedup_miss_different_start",
        "gws_calendar_create",
        CREATE_ARGS,
        {
            "calendar events list": _listing(
                _existing_event(
                    id="evt-next-week",
                    summary="Quarterly review",
                    start={"dateTime": "2026-10-16T14:00:00-04:00"},
                )
            ),
            "calendar events insert": _insert_echo,
        },
    ),
    Scenario(
        "calendar_create_force_skips_dedup",
        "gws_calendar_create",
        {**CREATE_ARGS, "force": True},
        CREATE_ROUTER,
    ),
    Scenario(
        "calendar_create_invalid_calendar",
        "gws_calendar_create",
        {**CREATE_ARGS, "calendar": "primary"},
    ),
    Scenario(
        "calendar_create_missing_fields",
        "gws_calendar_create",
        {"summary": "x"},
    ),
    Scenario(
        "calendar_create_cli_error",
        "gws_calendar_create",
        {**CREATE_ARGS, "force": True},
        {"calendar events insert": {"error": "Forbidden", "hint": "permission"}},
    ),
    Scenario(
        "calendar_update_reschedule",
        "gws_calendar_update",
        {
            "event_id": "meeting",
            "start": "2026-10-09T14:00:00-04:00",
            "end": "2026-10-09T15:00:00-04:00",
        },
    ),
    Scenario(
        "calendar_update_start_keeps_length",
        "gws_calendar_update",
        {"event_id": "meeting", "start": "2026-10-09T14:00:00Z"},
    ),
    Scenario(
        "calendar_update_guests_and_fields",
        "gws_calendar_update",
        {
            "event_id": "meeting",
            "add_attendees": ["dave@example.com", "DAVE@example.com"],
            "remove_attendees": ["BOB@EXAMPLE.COM"],
            "summary": "Planning (moved)",
            "location": "Room 4",
        },
    ),
    Scenario(
        "calendar_update_nothing_to_change",
        "gws_calendar_update",
        {"event_id": "meeting", "summary": "Planning"},
    ),
    Scenario(
        "calendar_update_own_calendar",
        "gws_calendar_update",
        {"event_id": "meeting", "summary": "X", "calendar": "own"},
    ),
    Scenario(
        "calendar_update_412_then_retry_succeeds",
        "gws_calendar_update",
        {"event_id": "meeting", "summary": "New title"},
        setup=_patch_failures(CONFLICT, None),
    ),
    Scenario(
        "calendar_update_412_twice",
        "gws_calendar_update",
        {"event_id": "meeting", "summary": "New title"},
        setup=_patch_failures(CONFLICT, CONFLICT),
    ),
    Scenario(
        "calendar_update_write_outcome_unknown",
        "gws_calendar_update",
        {"event_id": "meeting", "summary": "New title"},
        setup=_patch_failures(
            {"error": "Calendar request outcome unknown", "outcome_unknown": True}
        ),
    ),
    Scenario(
        "calendar_update_cancelled_before_read",
        "gws_calendar_update",
        {"event_id": "meeting", "summary": "New title"},
        setup=_already_cancelled,
    ),
    Scenario(
        "calendar_update_cancelled_between_read_and_write",
        "gws_calendar_update",
        {"event_id": "meeting", "summary": "New title"},
        setup=_cancelled_after_read,
    ),
    Scenario(
        "calendar_update_invalid_calendar",
        "gws_calendar_update",
        {"event_id": "meeting", "summary": "X", "calendar": "primary"},
    ),
    Scenario("calendar_update_missing_event_id", "gws_calendar_update", {"summary": "X"}),
    Scenario(
        "calendar_add_attendees",
        "gws_calendar_add_attendees",
        {"event_id": "meeting", "attendees": ["dave@example.com"]},
    ),
    Scenario(
        "calendar_add_attendees_already_present",
        "gws_calendar_add_attendees",
        {"event_id": "meeting", "attendees": ["bob@example.com"]},
    ),
    Scenario(
        "calendar_add_attendees_412_then_retry_succeeds",
        "gws_calendar_add_attendees",
        {"event_id": "meeting", "attendees": ["dave@example.com"]},
        setup=_patch_failures(CONFLICT, None),
    ),
    Scenario(
        "calendar_add_attendees_bad_list",
        "gws_calendar_add_attendees",
        {"event_id": "meeting", "attendees": ["not-an-address"]},
    ),
    Scenario(
        "calendar_respond_accept",
        "gws_calendar_respond",
        {"event_id": "meeting", "response": "accepted", "comment": "See you there"},
        setup=_guest_view,
    ),
    Scenario(
        "calendar_respond_by_calendar_address",
        "gws_calendar_respond",
        {"event_id": "meeting", "response": "declined"},
    ),
    Scenario(
        "calendar_respond_already_in_state",
        "gws_calendar_respond",
        {"event_id": "meeting", "response": "accepted"},
        setup=_already_accepted,
    ),
    Scenario(
        "calendar_respond_412_then_retry_succeeds",
        "gws_calendar_respond",
        {"event_id": "meeting", "response": "tentative"},
        setup=_patch_failures(CONFLICT, None),
    ),
    Scenario(
        "calendar_respond_cancelled_between_read_and_write",
        "gws_calendar_respond",
        {"event_id": "meeting", "response": "accepted"},
        setup=_cancelled_after_read,
    ),
    Scenario(
        "calendar_respond_not_a_guest",
        "gws_calendar_respond",
        {"event_id": "meeting", "response": "accepted", "calendar_id": "team@example.com"},
    ),
    Scenario(
        "calendar_delete_operator",
        "gws_calendar_delete",
        {"event_id": "evt-existing"},
        {"calendar events delete": {}},
    ),
    Scenario(
        "calendar_delete_own",
        "gws_calendar_delete",
        {"event_id": "evt-existing", "calendar": "own"},
        {"calendar events delete": {}},
    ),
    Scenario(
        "calendar_delete_cli_error",
        "gws_calendar_delete",
        {"event_id": "evt-gone"},
        {"calendar events delete": {"error": "Not Found", "hint": "not_found"}},
    ),
    Scenario("calendar_delete_missing_event_id", "gws_calendar_delete", {}),
]

CHAT_SCENARIOS = [
    Scenario(
        "chat_send",
        "gws_chat_send",
        {"space": "spaces/AAA", "text": "Build is green"},
        {"chat spaces messages create": {"name": "spaces/AAA/messages/1"}},
    ),
    Scenario("chat_send_missing_text", "gws_chat_send", {"space": "spaces/AAA"}),
    Scenario(
        "chat_list_spaces",
        "gws_chat_list_spaces",
        {"page_size": 5000},
        {"chat spaces list": {"spaces": [{"name": "spaces/AAA"}]}},
    ),
    Scenario(
        "chat_list_messages",
        "gws_chat_list_messages",
        {"space": "spaces/AAA", "page_size": 500},
        {"chat spaces messages list": {"messages": []}},
    ),
]

REPLY_ALL_ARGS = {"thread_id": "thread-9", "body": "Thanks.", "cc": "dave@example.com"}
UPDATE_ADD_ARGS = {"event_id": "meeting", "add_attendees": ["dave@example.com"]}
ADD_ARGS = {"event_id": "meeting", "attendees": ["dave@example.com"]}
SEND_ARGS = {"to": "bob@example.com", "cc": "carol@example.com", "subject": "S", "body": "B"}

GUARD_SCENARIOS = [
    # do-not-contact: block / observe (warn and send) / unreadable list, per sender.
    Scenario(
        "dnc_send_block", "gws_gmail_send", SEND_ARGS, SEND_ROUTER, _flag("carol@example.com")
    ),
    Scenario(
        "dnc_send_observe", "gws_gmail_send", SEND_ARGS, SEND_ROUTER, _observe("carol@example.com")
    ),
    Scenario("dnc_send_lookup_failed", "gws_gmail_send", SEND_ARGS, SEND_ROUTER, _lookup_fails),
    Scenario("dnc_send_no_tenant", "gws_gmail_send", SEND_ARGS, SEND_ROUTER, tenant_id=""),
    Scenario(
        "dnc_reply_block",
        "gws_gmail_reply",
        REPLY_ALL_ARGS,
        REPLY_ROUTER,
        _flag("dave@example.com"),
    ),
    Scenario(
        "dnc_reply_block_thread_participant",
        "gws_gmail_reply",
        REPLY_ALL_ARGS,
        REPLY_ROUTER,
        _flag("carol@example.com"),
    ),
    Scenario(
        "dnc_reply_observe",
        "gws_gmail_reply",
        REPLY_ALL_ARGS,
        REPLY_ROUTER,
        _observe("carol@example.com"),
    ),
    Scenario(
        "dnc_reply_lookup_failed", "gws_gmail_reply", REPLY_ALL_ARGS, REPLY_ROUTER, _lookup_fails
    ),
    Scenario(
        "dnc_create_block",
        "gws_calendar_create",
        CREATE_ARGS,
        CREATE_ROUTER,
        _flag("bob@example.com"),
    ),
    Scenario(
        "dnc_create_block_auto_added_operator",
        "gws_calendar_create",
        {**CREATE_ARGS, "calendar": "own"},
        CREATE_ROUTER,
        _flag(OPERATOR),
    ),
    Scenario(
        "dnc_create_observe",
        "gws_calendar_create",
        CREATE_ARGS,
        CREATE_ROUTER,
        _observe("bob@example.com"),
    ),
    Scenario(
        "dnc_create_lookup_failed", "gws_calendar_create", CREATE_ARGS, CREATE_ROUTER, _lookup_fails
    ),
    Scenario(
        "dnc_add_attendees_block",
        "gws_calendar_add_attendees",
        ADD_ARGS,
        setup=_flag("dave@example.com"),
    ),
    Scenario(
        "dnc_add_attendees_observe",
        "gws_calendar_add_attendees",
        ADD_ARGS,
        setup=_observe("dave@example.com"),
    ),
    Scenario(
        "dnc_add_attendees_lookup_failed",
        "gws_calendar_add_attendees",
        ADD_ARGS,
        setup=_lookup_fails,
    ),
    Scenario(
        "dnc_update_block_existing_guest",
        "gws_calendar_update",
        {"event_id": "meeting", "summary": "Moved"},
        setup=_flag("bob@example.com"),
    ),
    Scenario(
        "dnc_respond_block_organizer",
        "gws_calendar_respond",
        {"event_id": "meeting", "response": "accepted"},
        setup=_flag("carol@example.com"),
    ),
    # scheduling_policy = no_auto: only the operator may put this person on a meeting.
    Scenario(
        "no_auto_update_refused",
        "gws_calendar_update",
        UPDATE_ADD_ARGS,
        setup=_no_auto("dave@example.com"),
    ),
    Scenario(
        "no_auto_add_attendees_refused",
        "gws_calendar_add_attendees",
        {"event_id": "meeting", "attendees": ["Dave@Example.com"]},
        setup=_no_auto("dave@example.com"),
    ),
]

BENCHMARK_SCENARIOS = [
    Scenario(f"benchmark_refuses_{tool}", tool, {"anything": "at all"}, is_benchmark=True)
    for tool in (
        "gws_gmail_search",
        "gws_gmail_get",
        "gws_gmail_reply",
        "gws_gmail_send",
        "gws_gmail_modify",
        "gws_calendar_list",
        "gws_calendar_create",
        "gws_calendar_add_attendees",
        "gws_calendar_update",
        "gws_calendar_respond",
        "gws_calendar_delete",
        "gws_chat_send",
        "gws_chat_list_spaces",
        "gws_chat_list_messages",
    )
]

SCENARIOS = (
    GMAIL_SCENARIOS + CALENDAR_SCENARIOS + CHAT_SCENARIOS + GUARD_SCENARIOS + BENCHMARK_SCENARIOS
)


# ── Golden comparison ─────────────────────────────────────────────────

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def _scrub(value: Any) -> Any:
    """Stable placeholders for what legitimately varies between runs.

    Every fixture time is a fixed input, and every time the handlers derive
    (the ±14-day dedup window, a kept meeting length) is a function of it, so
    timestamps are behaviour and are kept. UUIDs are not; none are expected
    today, and any that appears is replaced rather than failing on noise.
    """
    if isinstance(value, dict):
        return {str(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = [_scrub(v) for v in value]
        return sorted(items, key=json.dumps) if isinstance(value, set | frozenset) else items
    if isinstance(value, str):
        return _UUID_RE.sub("<uuid>", value)
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: {value}"
    return value


def _render(record: dict[str, Any]) -> str:
    return json.dumps(_scrub(record), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _compare(name: str, rendered: str) -> None:
    path = GOLDEN_DIR / f"{name}.json"
    if REGEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        return
    if not path.exists():
        pytest.fail(f"no golden at {path}; record it with GWS_GOLDEN_REGEN=1 and review it")
    expected = path.read_text(encoding="utf-8")
    if expected != rendered:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"golden/gws/{name}.json",
                tofile="current behaviour",
            )
        )
        pytest.fail(
            f"gws behaviour drifted from the golden for {name!r}.\n"
            "If the change is intended, regenerate with GWS_GOLDEN_REGEN=1 and review "
            f"the diff.\n\n{diff}",
            pytrace=False,
        )


def test_scenario_names_are_unique() -> None:
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))


def test_every_registered_gws_tool_is_covered() -> None:
    assert {s.tool for s in SCENARIOS} == set(gws_handlers.HANDLERS)


def test_no_orphan_goldens() -> None:
    """A golden with no scenario is a pin nobody checks any more."""
    if REGEN:
        for path in GOLDEN_DIR.glob("*.json"):
            if path.stem not in {s.name for s in SCENARIOS}:
                path.unlink()
        return
    on_disk = {p.stem for p in GOLDEN_DIR.glob("*.json")}
    assert on_disk == {s.name for s in SCENARIOS}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_gws_golden(scenario: Scenario, harness: Harness) -> None:
    harness.gws_router = scenario.router
    if scenario.setup is not None:
        scenario.setup(harness)
    ctx = ToolContext(
        agent_id="main",
        run_id=RUN_ID,
        tenant_id=scenario.tenant_id,
        is_benchmark=scenario.is_benchmark,
    )
    result = await gws_handlers.HANDLERS[scenario.tool](deepcopy(scenario.args), ctx)

    calls = harness.gws_calls
    if scenario.unordered_tail and len(calls) > 1:
        calls = [calls[0], *sorted(calls[1:], key=lambda c: json.dumps(c, sort_keys=True))]

    record = {
        "scenario": scenario.name,
        "tool": scenario.tool,
        "args": scenario.args,
        "context": {
            "tenant_id": scenario.tenant_id,
            "run_id": RUN_ID,
            "is_benchmark": scenario.is_benchmark,
        },
        "gws_calls": calls,
        "calendar_requests": harness.calendar_requests,
        "dnc_lookups": harness.dnc_lookups,
        "guardrail_events": harness.guardrail_events,
        "scheduling_policy_lookups": harness.policy_lookups,
        "crm_write_through": {"calls": harness.crm_calls, "sql": harness.crm_sql},
        "result": result,
    }
    _compare(scenario.name, _render(record))
