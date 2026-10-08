"""The gws_* handlers reach mail and calendar only through the workspace provider.

The goldens (test_gws_goldens.py) prove the Google path is byte-identical. These
prove the other half of the seam: swap the provider and every transport call
lands on it, with the guards still in front.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from robothor.engine import calendar_attendees
from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import gws
from robothor.workspace import Workspace
from robothor.workspace.errors import Unsupported
from robothor.workspace.types import GOOGLE_CAPABILITIES, CalendarRef


class FakeMail:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.blocking = self

    def _log(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def search(self, query: Any, *, max_results: int) -> dict:
        self._log("search", query.original, max_results=max_results)
        return {"messages": [{"id": "m1", "threadId": "t1"}]}

    def get_message(self, message_id: str, *, fmt: str) -> dict:
        self._log("get_message", message_id, fmt=fmt)
        return {"id": message_id, "threadId": "t1", "payload": {"headers": []}}

    def get_thread(self, thread_id: str, *, fmt: str) -> dict:
        self._log("get_thread", thread_id, fmt=fmt)
        return {
            "id": thread_id,
            "messages": [
                {
                    "id": "m1",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "bob@example.com"},
                            {"name": "Subject", "value": "Hi"},
                        ]
                    },
                }
            ],
        }

    def send(self, raw: str, *, thread_id: str | None = None) -> dict:
        self._log("send", thread_id=thread_id)
        return {"id": "s1", "threadId": thread_id or "s1"}

    def reply(self, raw: str, *, thread_id: str) -> dict:
        self._log("reply", thread_id=thread_id)
        return {"id": "s2", "threadId": thread_id}

    def modify(self, message_id: str, *, add_labels: Any, remove_labels: Any) -> dict:
        self._log("modify", message_id, add_labels=add_labels, remove_labels=remove_labels)
        return {"id": message_id}

    def shape_envelope(self, raw: dict, *, max_header_chars: int | None = None) -> dict:
        return gws._shape_envelope(raw, max_header_chars=max_header_chars)

    def shape_message(self, raw: dict, *, max_chars: int) -> dict:
        return gws._shape_message(raw, max_chars=max_chars)


class FakeCalendar:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.blocking = self
        self.event = {
            "id": "meeting",
            "etag": '"v1"',
            "status": "confirmed",
            "attendees": [{"email": "bob@example.com", "responseStatus": "accepted"}],
            "start": {"dateTime": "2026-10-08T10:00:00-04:00"},
            "end": {"dateTime": "2026-10-08T10:30:00-04:00"},
        }

    def resolve(self, kind: str, *, address: str = "") -> CalendarRef:
        if kind == "own":
            return CalendarRef("me-mailbox", "own", mailbox="agent@example.com")
        return CalendarRef(address, "operator", mailbox=address)

    def list(self, ref: CalendarRef, query: dict) -> dict:
        self.calls.append(("list", ref, dict(query)))
        return {"items": []}

    def create(self, ref: CalendarRef, event: dict, *, conference: bool, send_updates: Any) -> dict:
        self.calls.append(("create", ref, conference, send_updates))
        return {"id": "evt-new", "status": "confirmed", **event}

    def delete(self, ref: CalendarRef, event_id: str, *, send_updates: str) -> dict:
        self.calls.append(("delete", ref, event_id, send_updates))
        return {}

    @contextmanager
    def session(self):
        yield self

    def get(self, ref: CalendarRef, event_id: str) -> dict:
        self.calls.append(("get", ref, event_id))
        return json.loads(json.dumps(self.event))

    def conditional_patch(self, ref, event_id, body, *, etag, send_updates) -> dict:
        self.calls.append(("patch", ref, event_id, body, etag, send_updates))
        self.event.update(body)
        self.event["etag"] = '"v2"'
        return dict(self.event)


@pytest.fixture
def fake_ws(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import yaml

    owner = tmp_path / "owner.yaml"
    owner.write_text(yaml.safe_dump({"first_name": "Alice", "email": "alice@example.com"}))
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(owner))
    monkeypatch.setattr(gws, "ROBOTHOR_EMAIL", "agent@example.com")
    ws = Workspace(
        provider="google",
        mail=FakeMail(),
        calendar=FakeCalendar(),
        capabilities=GOOGLE_CAPABILITIES,
    )
    monkeypatch.setattr("robothor.workspace.get_workspace", lambda tenant_id=None: ws)

    def no_cli(*a: Any, **k: Any) -> Any:
        raise AssertionError("a mail/calendar call bypassed the provider")

    monkeypatch.setattr(gws, "_run_gws", no_cli)
    monkeypatch.setattr(calendar_attendees, "CalendarTransport", no_cli)
    monkeypatch.setattr(gws, "_dnc_refusal", lambda *a, **k: None)
    monkeypatch.setattr(gws, "_record_sent_email", lambda **k: None)
    monkeypatch.setattr(gws, "_record_calendar_event", lambda **k: None)
    monkeypatch.setattr("robothor.engine.feature_flags.calendar_send_updates", lambda: "all")
    monkeypatch.setattr("robothor.engine.guardrails._lookup_scheduling_policies", lambda emails: {})
    return ws


def test_mail_calls_go_through_the_provider(fake_ws: Workspace) -> None:
    mail: FakeMail = fake_ws.mail  # type: ignore[assignment]
    assert gws._handle_gws_tool("gws_gmail_search", {"query": "is:unread"})["count"] == 1
    gws._handle_gws_tool("gws_gmail_get", {"message_id": "m1"})
    gws._handle_gws_tool("gws_gmail_get", {"thread_id": "t1"})
    gws._handle_gws_tool("gws_gmail_send", {"to": "bob@example.com", "subject": "S", "body": "B"})
    gws._handle_gws_tool("gws_gmail_reply", {"thread_id": "t1", "body": "B"})
    gws._handle_gws_tool("gws_gmail_modify", {"message_id": "m1", "add_labels": ["STARRED"]})
    assert [c[0] for c in mail.calls] == [
        "search",
        "get_message",
        "get_message",
        "get_thread",
        "send",
        "get_thread",
        "reply",
        "modify",
    ]
    assert mail.calls[0][1] == ("is:unread",)


def test_calendar_calls_go_through_the_provider(fake_ws: Workspace) -> None:
    cal: FakeCalendar = fake_ws.calendar  # type: ignore[assignment]
    listed = gws._handle_gws_tool(
        "gws_calendar_list", {"time_min": "2026-10-01T00:00:00Z", "calendar": "own"}
    )
    assert listed["calendar"] == {"kind": "own", "id": "me-mailbox"}
    gws._handle_gws_tool(
        "gws_calendar_create",
        {"summary": "S", "start": "2026-10-08T10:00:00", "end": "2026-10-08T11:00:00"},
    )
    gws._handle_gws_tool("gws_calendar_delete", {"event_id": "e1"})
    updated = gws._handle_gws_tool(
        "gws_calendar_update", {"event_id": "meeting", "summary": "Moved"}
    )
    assert updated["status"] == "updated"
    operator = CalendarRef("alice@example.com", "operator", mailbox="alice@example.com")
    assert [c[0] for c in cal.calls] == ["list", "list", "create", "delete", "get", "patch", "get"]
    assert cal.calls[0][1] == CalendarRef("me-mailbox", "own", mailbox="agent@example.com")
    assert all(c[1] == operator for c in cal.calls[1:])


async def test_dark_provider_is_refused_before_any_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def dark(tenant_id: str | None = None) -> Workspace:
        raise Unsupported("microsoft365 mail and calendar are not available yet")

    monkeypatch.setattr("robothor.workspace.get_workspace", dark)
    monkeypatch.setattr(gws, "_dnc_refusal", lambda *a, **k: pytest.fail("guard ran"))
    ctx = ToolContext(agent_id="main", run_id="r", tenant_id="t")
    out = await gws.HANDLERS["gws_gmail_send"]({"to": "bob@example.com"}, ctx)
    assert out["error"].startswith("microsoft365 mail and calendar are not available yet")
    assert out["hint"] == "unsupported"
