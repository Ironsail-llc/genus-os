"""A stateful in-memory Google Workspace behind the two seams the gws tools use.

The characterization goldens script one canned answer per CLI call; a contract
scenario needs STATE instead (a reply must land in the thread a search found, an
edit must read back what it wrote). This fake answers both seams the Google
adapter calls through:

* ``gws._run_gws(argv)`` -- Gmail messages/threads and Calendar list / insert /
  delete / get, keyed by the CLI verb exactly as the adapter issues it;
* ``calendar_attendees.CalendarTransport`` -- the conditional GET / PATCH the
  calendar edits make, with an etag per event and a 412 on a stale one.

Every call is recorded, so a test can count the requests that could change
state (:meth:`FakeGoogleWorkspace.writes`) and assert a refusal made none.
Every address is a generic fixture.
"""

from __future__ import annotations

import base64
import email
import itertools
import json
import threading
from copy import deepcopy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

__all__ = ["FakeGoogleWorkspace"]

#: CLI verbs that change state at Google.
_WRITE_VERBS = frozenset(
    {
        "gmail users messages send",
        "gmail users messages modify",
        "calendar events insert",
        "calendar events delete",
    }
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _header(message: dict[str, Any], name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return str(h["value"])
    return ""


def _google_time(value: dict[str, Any]) -> dict[str, Any]:
    """What Google stores for a time: nulls dropped, a naive time localised."""
    out = {k: v for k, v in value.items() if v is not None}
    if "dateTime" in out:
        moment = datetime.fromisoformat(out["dateTime"])
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ZoneInfo(out.get("timeZone") or "UTC"))
            out["dateTime"] = moment.isoformat()
    return out


class FakeGoogleWorkspace:
    """Gmail plus Google Calendar for one assistant account."""

    def __init__(self, *, assistant: str) -> None:
        self.assistant = assistant
        #: id -> raw Gmail message (``format=full`` shape), in arrival order.
        self.messages: dict[str, dict[str, Any]] = {}
        #: calendarId -> events (Calendar v3 shape, with an etag).
        self.calendars: dict[str, list[dict[str, Any]]] = {}
        #: Every CLI call: ``{"verb", "params", "json"}``.
        self.cli_calls: list[dict[str, Any]] = []
        #: Every conditional calendar request: ``{"method", "calendar_id", ...}``.
        self.transport_calls: list[dict[str, Any]] = []
        #: event id -> fields another writer changes just before our next PATCH.
        self.concurrent_edits: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # ── inspection ───────────────────────────────────────────────────────

    def requests(self) -> int:
        return len(self.cli_calls) + len(self.transport_calls)

    def writes(self) -> int:
        cli = sum(1 for c in self.cli_calls if c["verb"] in _WRITE_VERBS)
        return cli + sum(1 for c in self.transport_calls if c["method"] != "GET")

    def patches(self) -> list[dict[str, Any]]:
        return [c for c in self.transport_calls if c["method"] == "PATCH"]

    def find_event(self, calendar_id: str, event_id: str) -> dict[str, Any] | None:
        return next(
            (e for e in self.calendars.get(calendar_id, []) if e["id"] == event_id),
            None,
        )

    # ── seeding ──────────────────────────────────────────────────────────

    def _new_id(self, kind: str) -> str:
        return f"g-{kind}-{next(self._ids)}"

    def deliver(
        self,
        *,
        sender: str,
        to: list[str],
        cc: list[str] = (),  # type: ignore[assignment]
        subject: str,
        body: str,
        thread_id: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        message_id = self._new_id("msg")
        headers = [
            {"name": "From", "value": sender},
            {"name": "To", "value": ", ".join(to)},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": "Tue, 06 Oct 2026 09:00:00 +0000"},
            {"name": "Message-ID", "value": f"<{message_id}@example.com>"},
        ]
        if cc:
            headers.insert(2, {"name": "Cc", "value": ", ".join(cc)})
        message = {
            "id": message_id,
            "threadId": thread_id or self._new_id("thread"),
            "labelIds": list(labels if labels is not None else ["INBOX", "UNREAD"]),
            "snippet": body[:100],
            "payload": {
                "mimeType": "text/plain",
                "headers": headers,
                "body": {"data": _b64(body)},
            },
        }
        self.messages[message_id] = message
        return message

    def add_event(self, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        stored = deepcopy(event)
        stored.setdefault("id", self._new_id("evt"))
        stored.setdefault("status", "confirmed")
        stored.setdefault("htmlLink", f"https://calendar.example.test/{stored['id']}")
        stored["etag"] = f'"{next(self._ids)}"'
        self.calendars.setdefault(calendar_id, []).append(stored)
        return stored

    # ── the gws CLI ──────────────────────────────────────────────────────

    def run_gws(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        verb = " ".join(a for a in itertools.takewhile(lambda a: not a.startswith("--"), args))
        params = json.loads(args[args.index("--params") + 1]) if "--params" in args else {}
        body = json.loads(args[args.index("--json") + 1]) if "--json" in args else None
        with self._lock:
            self.cli_calls.append({"verb": verb, "params": params, "json": body})
            handler = getattr(self, "_cli_" + verb.replace(" ", "_"), None)
            if handler is None:
                raise AssertionError(f"the fake Google workspace has no {verb!r}")
            return deepcopy(handler(params, body))

    def _cli_gmail_users_messages_list(self, params: dict[str, Any], _body: Any) -> dict[str, Any]:
        terms = str(params.get("q", "")).split()
        found = [
            {"id": message["id"], "threadId": message["threadId"]}
            for message in reversed(list(self.messages.values()))
            if all(self._matches(message, term) for term in terms)
        ]
        found = found[: int(params.get("maxResults", 100))]
        return {"messages": found, "resultSizeEstimate": len(found)} if found else {}

    @staticmethod
    def _matches(message: dict[str, Any], term: str) -> bool:
        field, _, value = term.partition(":")
        if not value:
            return True
        value = value.lower()
        if field == "from":
            return value in _header(message, "From").lower()
        if field == "subject":
            return value in _header(message, "Subject").lower()
        if field == "is" and value == "unread":
            return "UNREAD" in message["labelIds"]
        if field == "in" and value == "inbox":
            return "INBOX" in message["labelIds"]
        return True

    def _cli_gmail_users_messages_get(self, params: dict[str, Any], _body: Any) -> dict[str, Any]:
        message = self.messages.get(params["id"])
        if message is None:
            return {"error": "Requested entity was not found. (404)", "hint": "not_found"}
        return message

    def _cli_gmail_users_threads_get(self, params: dict[str, Any], _body: Any) -> dict[str, Any]:
        messages = [m for m in self.messages.values() if m["threadId"] == params["id"]]
        if not messages:
            return {"error": "Requested entity was not found. (404)", "hint": "not_found"}
        return {"id": params["id"], "messages": messages}

    def _cli_gmail_users_messages_send(self, _params: Any, body: dict[str, Any]) -> dict[str, Any]:
        mime = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
        payload = mime.get_payload(decode=True)
        text = payload.decode(mime.get_content_charset() or "utf-8") if payload else ""
        message_id = self._new_id("sent")
        headers = [{"name": "From", "value": self.assistant}]
        headers += [{"name": k, "value": v} for k, v in mime.items() if k in _KEPT_HEADERS]
        headers.append({"name": "Message-ID", "value": f"<{message_id}@example.com>"})
        thread_id = body.get("threadId") or message_id
        self.messages[message_id] = {
            "id": message_id,
            "threadId": thread_id,
            "labelIds": ["SENT"],
            "snippet": text[:100],
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": _b64(text)}},
        }
        return {"id": message_id, "threadId": thread_id, "labelIds": ["SENT"]}

    def _cli_gmail_users_messages_modify(
        self, params: dict[str, Any], body: dict[str, Any]
    ) -> dict[str, Any]:
        message = self.messages.get(params["id"])
        if message is None:
            return {"error": "Requested entity was not found. (404)", "hint": "not_found"}
        labels = [x for x in message["labelIds"] if x not in body.get("removeLabelIds", [])]
        labels += [x for x in body.get("addLabelIds", []) if x not in labels]
        message["labelIds"] = labels
        return {"id": message["id"], "threadId": message["threadId"], "labelIds": labels}

    def _cli_calendar_events_list(self, params: dict[str, Any], _body: Any) -> dict[str, Any]:
        events = [
            e for e in self.calendars.get(params["calendarId"], []) if e["status"] != "cancelled"
        ]
        return {"kind": "calendar#events", "items": events}

    def _cli_calendar_events_get(self, params: dict[str, Any], _body: Any) -> dict[str, Any]:
        event = self.find_event(params["calendarId"], params["eventId"])
        if event is None:
            return {"error": "Not Found", "hint": "not_found"}
        return event

    def _cli_calendar_events_insert(
        self, params: dict[str, Any], body: dict[str, Any]
    ) -> dict[str, Any]:
        calendar_id = params["calendarId"]
        event = {
            "summary": body["summary"],
            "start": _google_time(body["start"]),
            "end": _google_time(body["end"]),
            "organizer": {"email": calendar_id, "self": True},
        }
        for key in ("description", "location"):
            if key in body:
                event[key] = body[key]
        if body.get("attendees"):
            event["attendees"] = [
                {"email": a["email"], "responseStatus": "needsAction"} for a in body["attendees"]
            ]
        if "conferenceData" in body:
            event["hangoutLink"] = "https://meet.example.test/abc-defg-hij"
        return self.add_event(calendar_id, event)

    def _cli_calendar_events_delete(self, params: dict[str, Any], _body: Any) -> dict[str, Any]:
        event = self.find_event(params["calendarId"], params["eventId"])
        if event is None or event["status"] == "cancelled":
            return {"error": "Not Found", "hint": "not_found"}
        event["status"] = "cancelled"
        return {}

    # ── the conditional calendar transport ───────────────────────────────

    def transport(self) -> _Transport:
        return _Transport(self)


#: The MIME headers a sent Gmail message keeps (the rest are transport detail).
_KEPT_HEADERS = ("To", "Cc", "Subject", "In-Reply-To", "References")


class _Transport:
    """``calendar_attendees.CalendarTransport``, over the fake's calendars."""

    def __init__(self, google: FakeGoogleWorkspace) -> None:
        self.google = google

    def __enter__(self) -> _Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def request(self, method: str, calendar_id: str, event_id: str, **kwargs: Any) -> Any:
        google = self.google
        with google._lock:
            google.transport_calls.append(
                {"method": method, "calendar_id": calendar_id, "event_id": event_id, **kwargs}
            )
            event = google.find_event(calendar_id, event_id)
            if event is None:
                return {"error": "Calendar HTTP 404", "status_code": 404}
            if method == "GET":
                return deepcopy(event)
            assert method == "PATCH", method
            edit = google.concurrent_edits.pop(event_id, None)
            if edit is not None:
                # Somebody else saved the event between our read and our write.
                event.update(edit)
                event["etag"] = f'"{next(google._ids)}"'
            if kwargs.get("etag") != event["etag"]:
                return {"error": "Calendar HTTP 412", "status_code": 412}
            body = deepcopy(kwargs["body"])
            for key in ("start", "end"):
                if key in body:
                    event[key] = _google_time(body.pop(key))
            event.update(body)
            event["etag"] = f'"{next(google._ids)}"'
            return deepcopy(event)
