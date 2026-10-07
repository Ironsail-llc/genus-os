"""Full calendar control: edit, reschedule, add/remove guests, RSVP — one call each.

The main agent could not add a guest to an existing meeting (the tool was gated
off behind a setting and a draft→"yes" confirmation that a bystander in a group
chat could fire), could not move a meeting at all, and could not RSVP. Editing a
meeting is not a payment or an irreversible external action, so it gets no gate.

Nothing here reaches Google: every test swaps the conditional-request transport
for an in-memory event. No address in this file belongs to anyone.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from robothor.engine import calendar_attendees
from robothor.engine.tools.handlers import gws as gws_handlers

OPERATOR = "alice@example.com"


class FakeCalendar:
    def __init__(self) -> None:
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
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failure: dict[str, Any] | None = None

    def __enter__(self) -> FakeCalendar:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def request(self, method: str, calendar_id: str, event_id: str, **kwargs: Any) -> Any:
        self.calls.append((method, {"calendar_id": calendar_id, **kwargs}))
        if method == "PATCH":
            assert kwargs["etag"] == self.event["etag"], "every write is conditional"
            if self.failure:
                return self.failure
            body = deepcopy(kwargs["body"])
            for key in ("start", "end"):
                if key in body:
                    self.event[key] = body.pop(key)
            self.event.update(body)
            self.event["etag"] = '"v2"'
        return deepcopy(self.event)

    def patches(self) -> list[dict[str, Any]]:
        return [kw for method, kw in self.calls if method == "PATCH"]


@pytest.fixture
def cal(monkeypatch: pytest.MonkeyPatch, tmp_path) -> FakeCalendar:
    import yaml

    owner = tmp_path / "owner.yaml"
    owner.write_text(
        yaml.safe_dump({"tenant_id": "fixture", "first_name": "Alice", "email": OPERATOR})
    )
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(owner))
    fake = FakeCalendar()
    monkeypatch.setattr(calendar_attendees, "CalendarTransport", lambda: fake)
    monkeypatch.setattr(gws_handlers, "_dnc_refusal", lambda *a, **k: None)
    monkeypatch.setattr(gws_handlers, "_send_updates", lambda: "all")
    return fake


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return gws_handlers._handle_gws_tool(name, args, run_id="", tenant_id="fixture")


# ── gws_calendar_update ───────────────────────────────────────────────


def test_reschedule_moves_the_meeting_in_one_conditional_write(cal: FakeCalendar) -> None:
    out = _call(
        "gws_calendar_update",
        {
            "event_id": "meeting",
            "start": "2026-10-09T14:00:00-04:00",
            "end": "2026-10-09T15:00:00-04:00",
        },
    )
    assert "error" not in out, out
    assert out["status"] == "updated"
    assert out["verification"] == "verified"
    [patch] = cal.patches()
    assert patch["calendar_id"] == OPERATOR, "the operator's calendar by default"
    assert patch["send_updates"] == "all"
    assert patch["body"]["start"]["dateTime"] == "2026-10-09T14:00:00-04:00"
    # Same timezone handling as create (dateTime), keeping the event's own zone.
    assert patch["body"]["start"]["timeZone"] == "America/New_York"
    assert out["start"]["dateTime"] == "2026-10-09T14:00:00-04:00"
    assert out["end"]["dateTime"] == "2026-10-09T15:00:00-04:00"
    assert out["htmlLink"] == "https://calendar.example.test/meeting"
    assert out["send_updates"] == "all"
    assert out["calendar"] == {"kind": "operator", "id": OPERATOR}
    # Guests and RSVPs survive a reschedule untouched.
    assert {a["email"]: a["responseStatus"] for a in out["attendees"]} == {
        OPERATOR: "needsAction",
        "Bob@Example.com": "accepted",
    }
    assert sorted(out["attendees_notified"]) == sorted([OPERATOR, "Bob@Example.com"])


def test_start_alone_keeps_the_meeting_length(cal: FakeCalendar) -> None:
    out = _call("gws_calendar_update", {"event_id": "meeting", "start": "2026-10-09T14:00:00Z"})
    assert "error" not in out, out
    [patch] = cal.patches()
    assert patch["body"]["end"]["dateTime"] == "2026-10-09T14:30:00+00:00"


def test_end_before_start_is_refused_before_any_write(cal: FakeCalendar) -> None:
    out = _call(
        "gws_calendar_update",
        {
            "event_id": "meeting",
            "start": "2026-10-09T15:00:00Z",
            "end": "2026-10-09T14:00:00Z",
        },
    )
    assert "error" in out
    assert cal.patches() == []


def test_add_and_remove_guests_merge_case_insensitively(cal: FakeCalendar) -> None:
    out = _call(
        "gws_calendar_update",
        {
            "event_id": "meeting",
            "add_attendees": ["dave@example.com", "DAVE@example.com"],
            "remove_attendees": ["BOB@EXAMPLE.COM"],
            "summary": "Planning (moved)",
        },
    )
    assert "error" not in out, out
    [patch] = cal.patches()
    emails = [a["email"].casefold() for a in patch["body"]["attendees"]]
    assert emails == [OPERATOR, "dave@example.com"]
    # The operator's existing entry is carried over whole, RSVP included.
    assert patch["body"]["attendees"][0] == {"email": OPERATOR, "responseStatus": "needsAction"}
    assert patch["body"]["summary"] == "Planning (moved)"
    assert out["added"] == ["dave@example.com"]
    assert out["removed"] == ["bob@example.com"]


def test_adding_someone_already_invited_does_not_duplicate_them(cal: FakeCalendar) -> None:
    out = _call(
        "gws_calendar_update",
        {"event_id": "meeting", "add_attendees": ["bob@example.com"], "location": "Room 4"},
    )
    assert "error" not in out, out
    [patch] = cal.patches()
    # The guest list did not change, so it is not rewritten at all.
    assert "attendees" not in patch["body"]
    assert patch["body"]["location"] == "Room 4"
    emails = [a["email"].casefold() for a in out["attendees"]]
    assert emails.count("bob@example.com") == 1
    assert out["attendees"][1]["responseStatus"] == "accepted"
    assert out["already_present"] == ["bob@example.com"]
    assert out["added"] == []


def test_adding_and_removing_the_same_guest_is_refused(cal: FakeCalendar) -> None:
    out = _call(
        "gws_calendar_update",
        {
            "event_id": "meeting",
            "add_attendees": ["bob@example.com"],
            "remove_attendees": ["BOB@example.com"],
        },
    )
    assert "error" in out
    assert cal.calls == []


def test_nothing_to_change_writes_nothing(cal: FakeCalendar) -> None:
    out = _call("gws_calendar_update", {"event_id": "meeting", "summary": "Planning"})
    assert "error" not in out, out
    assert out["status"] == "unchanged"
    assert out["invitations_requested"] is False
    assert cal.patches() == []


def test_an_update_with_no_change_requested_is_an_error(cal: FakeCalendar) -> None:
    out = _call("gws_calendar_update", {"event_id": "meeting"})
    assert "error" in out
    assert cal.calls == []


def test_added_invitees_go_through_the_do_not_contact_screen(
    cal: FakeCalendar, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def refuse(tool: str, *recipients: str, **kwargs: Any) -> dict[str, Any]:
        seen.extend(recipients)
        return {"error": "do not contact", "tool": tool}

    monkeypatch.setattr(gws_handlers, "_dnc_refusal", refuse)
    out = _call(
        "gws_calendar_update", {"event_id": "meeting", "add_attendees": ["eve@example.com"]}
    )
    assert out["error"] == "do not contact"
    assert "eve@example.com" in seen
    assert cal.patches() == []


def test_a_conflicting_edit_is_re_read_and_merged_once(cal: FakeCalendar) -> None:
    cal.failure = {"error": "conflict", "status_code": 412}
    out = _call("gws_calendar_update", {"event_id": "meeting", "summary": "New"})
    assert "error" in out
    assert [c[0] for c in cal.calls] == ["GET", "PATCH", "GET", "PATCH"]


def test_own_calendar_and_explicit_id_are_honoured(cal: FakeCalendar) -> None:
    _call("gws_calendar_update", {"event_id": "meeting", "summary": "X", "calendar": "own"})
    _call(
        "gws_calendar_update",
        {"event_id": "meeting", "summary": "Y", "calendar_id": "team@example.com"},
    )
    assert [p["calendar_id"] for p in cal.patches()] == ["primary", "team@example.com"]


def test_an_invalid_calendar_is_refused_before_google(cal: FakeCalendar) -> None:
    out = _call(
        "gws_calendar_update", {"event_id": "meeting", "summary": "X", "calendar": "primary"}
    )
    assert out["hint"] == "invalid_params"
    assert cal.calls == []


# ── gws_calendar_add_attendees: a thin alias, direct, ungated ─────────


@pytest.mark.asyncio
async def test_add_attendees_writes_directly_with_no_draft_and_no_gate(
    cal: FakeCalendar,
) -> None:
    from robothor.engine.tools.dispatch import ToolContext

    handler = gws_handlers.HANDLERS["gws_calendar_add_attendees"]
    out = await handler(
        {"event_id": "meeting", "attendees": ["dave@example.com"]},
        ToolContext(tenant_id="fixture", agent_id="main"),
    )
    assert "error" not in out, out
    assert "operation_id" not in out
    assert out["status"] == "updated"
    assert out["added"] == ["dave@example.com"]
    [patch] = cal.patches()
    assert [a["email"] for a in patch["body"]["attendees"]][-1] == "dave@example.com"


def test_the_add_attendees_schema_offers_no_draft_or_operation_id() -> None:
    from robothor.engine.tools.schemas import get_engine_schemas

    props = get_engine_schemas()["gws_calendar_add_attendees"]["function"]["parameters"][
        "properties"
    ]
    assert "draft" not in props
    assert "operation_id" not in props


# ── gws_calendar_respond ──────────────────────────────────────────────


def test_respond_sets_the_calendar_owners_rsvp(cal: FakeCalendar) -> None:
    out = _call(
        "gws_calendar_respond",
        {"event_id": "meeting", "response": "accepted", "comment": "See you there"},
    )
    assert "error" not in out, out
    [patch] = cal.patches()
    by_email = {a["email"]: a for a in patch["body"]["attendees"]}
    assert by_email[OPERATOR]["responseStatus"] == "accepted"
    assert by_email[OPERATOR]["comment"] == "See you there"
    # Nobody else's RSVP is touched.
    assert by_email["Bob@Example.com"] == {
        "email": "Bob@Example.com",
        "responseStatus": "accepted",
        "optional": True,
    }
    assert patch["send_updates"] == "all"
    assert out["response"] == "accepted"
    assert out["calendar"] == {"kind": "operator", "id": OPERATOR}


def test_respond_on_own_calendar_uses_the_self_entry(cal: FakeCalendar) -> None:
    cal.event["attendees"].append(
        {"email": "bot@example.com", "self": True, "responseStatus": "needsAction"}
    )
    out = _call(
        "gws_calendar_respond", {"event_id": "meeting", "response": "declined", "calendar": "own"}
    )
    assert "error" not in out, out
    [patch] = cal.patches()
    me = [a for a in patch["body"]["attendees"] if a.get("self")]
    assert me[0]["responseStatus"] == "declined"


@pytest.mark.parametrize("bad", ["", "yes", "needsAction", None])
def test_respond_refuses_an_unknown_response(cal: FakeCalendar, bad: Any) -> None:
    out = _call("gws_calendar_respond", {"event_id": "meeting", "response": bad})
    assert "error" in out
    assert cal.calls == []


def test_respond_when_not_invited_says_so(cal: FakeCalendar) -> None:
    cal.event["attendees"] = [{"email": "bob@example.com", "responseStatus": "accepted"}]
    out = _call("gws_calendar_respond", {"event_id": "meeting", "response": "tentative"})
    assert "error" in out
    assert cal.patches() == []


# ── Registration ──────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", ["gws_calendar_update", "gws_calendar_respond"])
def test_new_tools_are_registered_everywhere(tool: str) -> None:
    from robothor.engine.benchmark_sandbox import EXTERNAL_SIDE_EFFECT_TOOLS
    from robothor.engine.tools.constants import CORE_TOOLS, GWS_TOOLS, READONLY_TOOLS
    from robothor.engine.tools.schemas import get_engine_schemas

    assert tool in gws_handlers.HANDLERS
    assert tool in get_engine_schemas()
    assert tool in GWS_TOOLS
    assert tool in CORE_TOOLS
    assert tool in EXTERNAL_SIDE_EFFECT_TOOLS
    assert tool not in READONLY_TOOLS
    assert tool in gws_handlers._GWS_MUTATING_TOOLS


def test_every_calendar_tool_is_core() -> None:
    from robothor.engine.tools.constants import CORE_TOOLS, GWS_TOOLS

    calendar = {t for t in GWS_TOOLS if t.startswith("gws_calendar_")}
    assert calendar <= CORE_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["gws_calendar_update", "gws_calendar_respond"])
async def test_benchmark_runs_are_refused(cal: FakeCalendar, tool: str) -> None:
    from robothor.engine.tools.dispatch import ToolContext

    out = await gws_handlers.HANDLERS[tool]({"event_id": "meeting"}, ToolContext(is_benchmark=True))
    assert out["guard"] == "is_benchmark"
    assert cal.calls == []


# ── The confirmation gate is gone ─────────────────────────────────────


def test_there_is_no_calendar_operations_setting() -> None:
    from robothor.settings import get_settings

    assert not hasattr(get_settings().engine, "calendar_operations_enabled")


def test_a_bare_yes_binds_nothing() -> None:
    import importlib.util

    from robothor.engine import calendar_operations

    assert not hasattr(calendar_operations, "confirmation_id")
    assert importlib.util.find_spec("robothor.engine.routine_request") is None
