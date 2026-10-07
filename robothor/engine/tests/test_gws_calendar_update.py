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


def _google_echo(value: dict[str, Any]) -> dict[str, Any]:
    """What Google stores and echoes: nulls cleared, a naive time localised in
    its ``timeZone`` and returned with an explicit offset. A naive time with no
    zone is a 400 at Google."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    out = {k: v for k, v in value.items() if v is not None}
    if "dateTime" in out:
        moment = datetime.fromisoformat(out["dateTime"])
        if moment.tzinfo is None:
            assert out.get("timeZone"), "Google rejects a naive time with no timeZone"
            moment = moment.replace(tzinfo=ZoneInfo(out["timeZone"]))
            out["dateTime"] = moment.isoformat()
    return out


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
                    self.event[key] = _google_echo(body.pop(key))
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
    monkeypatch.setattr("robothor.engine.guardrails._lookup_scheduling_policies", lambda emails: {})
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


# ── Review fixes ──────────────────────────────────────────────────────


def test_a_naive_reschedule_that_google_applied_is_verified(cal: FakeCalendar) -> None:
    """A naive start ("2026-10-09T14:00:00") is what the schema invites. Google
    localises it in the event's zone and echoes an offset; comparing naive to
    aware called a write that landed `partial`."""
    out = _call(
        "gws_calendar_update",
        {"event_id": "meeting", "start": "2026-10-09T14:00:00", "end": "2026-10-09T15:00:00"},
    )
    assert "error" not in out, out
    assert out["status"] == "updated"
    assert out["verification"] == "verified"
    assert cal.event["start"]["dateTime"] == "2026-10-09T14:00:00-04:00"


def test_a_naive_start_alone_keeps_the_length_and_verifies(cal: FakeCalendar) -> None:
    out = _call("gws_calendar_update", {"event_id": "meeting", "start": "2026-10-09T14:00:00"})
    assert out["verification"] == "verified", out
    assert cal.event["end"]["dateTime"] == "2026-10-09T14:30:00-04:00"


def test_a_naive_time_on_an_event_with_no_zone_gets_the_operator_zone(
    cal: FakeCalendar, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("start", "end"):
        cal.event[key].pop("timeZone")
    monkeypatch.setattr(calendar_attendees, "_default_timezone", lambda: "America/Chicago")
    out = _call("gws_calendar_update", {"event_id": "meeting", "start": "2026-10-09T14:00:00"})
    assert out["verification"] == "verified", out
    [patch] = cal.patches()
    assert patch["body"]["start"]["timeZone"] == "America/Chicago"
    assert patch["body"]["end"]["timeZone"] == "America/Chicago"
    assert cal.event["start"]["dateTime"] == "2026-10-09T14:00:00-05:00"


@pytest.mark.parametrize("tool", ["gws_calendar_update", "gws_calendar_add_attendees"])
def test_a_no_auto_person_cannot_be_added(
    cal: FakeCalendar, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    """`scheduling_policy='no_auto'` is a data policy: agents may not put this
    person on a meeting. It guarded create only, so an edit was the way round."""
    monkeypatch.setattr(
        "robothor.engine.guardrails._lookup_scheduling_policies",
        lambda emails: {"eve@example.com": "no_auto"} if "eve@example.com" in emails else {},
    )
    args = (
        {"event_id": "meeting", "add_attendees": ["Eve@example.com"]}
        if tool == "gws_calendar_update"
        else {"event_id": "meeting", "attendees": ["Eve@example.com"]}
    )
    out = _call(tool, args)
    assert "no_auto" in out["error"]
    assert cal.calls == []


def test_a_no_auto_guest_already_on_the_meeting_does_not_block_a_reschedule(
    cal: FakeCalendar, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "robothor.engine.guardrails._lookup_scheduling_policies",
        lambda emails: {e: "no_auto" for e in emails if e == "bob@example.com"},
    )
    out = _call("gws_calendar_update", {"event_id": "meeting", "summary": "Moved"})
    assert "error" not in out, out


def test_respond_on_a_hidden_guest_list_patches_only_the_self_entry(cal: FakeCalendar) -> None:
    """A guest's copy with a hidden guest list carries only the self entry and
    `attendeesOmitted`. Google's documented RSVP is a PATCH with just that entry."""
    cal.event["attendees"] = [{"email": OPERATOR, "self": True, "responseStatus": "needsAction"}]
    cal.event["attendeesOmitted"] = True
    out = _call("gws_calendar_respond", {"event_id": "meeting", "response": "accepted"})
    assert "error" not in out, out
    [patch] = cal.patches()
    assert patch["body"] == {
        "attendees": [{"email": OPERATOR, "self": True, "responseStatus": "accepted"}]
    }


def test_an_update_still_refuses_to_overwrite_a_hidden_guest_list(cal: FakeCalendar) -> None:
    cal.event["attendeesOmitted"] = True
    out = _call("gws_calendar_update", {"event_id": "meeting", "add_attendees": ["d@example.com"]})
    assert "incomplete guest list" in out["error"]
    assert cal.patches() == []


@pytest.mark.parametrize("who", ["carol@example.com", OPERATOR.upper()])
def test_the_organiser_and_the_calendars_own_entry_cannot_be_removed(
    cal: FakeCalendar, who: str
) -> None:
    cal.event["attendees"].append({"email": "carol@example.com", "organizer": True})
    out = _call("gws_calendar_update", {"event_id": "meeting", "remove_attendees": [who]})
    assert "error" in out
    assert "cannot remove" in out["error"].lower()
    assert cal.patches() == []


def test_the_self_entry_cannot_be_removed_on_own_calendar(cal: FakeCalendar) -> None:
    cal.event["attendees"].append({"email": "bot@example.com", "self": True})
    out = _call(
        "gws_calendar_update",
        {"event_id": "meeting", "remove_attendees": ["bot@example.com"], "calendar": "own"},
    )
    assert "cannot remove" in out["error"].lower()
    assert cal.patches() == []


@pytest.fixture
def all_day(cal: FakeCalendar) -> FakeCalendar:
    cal.event["start"] = {"date": "2026-10-08"}
    cal.event["end"] = {"date": "2026-10-10"}
    return cal


def test_moving_an_all_day_event_keeps_it_all_day_and_its_length(all_day: FakeCalendar) -> None:
    out = _call("gws_calendar_update", {"event_id": "meeting", "start": "2026-10-20"})
    assert out["verification"] == "verified", out
    [patch] = all_day.patches()
    assert patch["body"]["start"] == {"date": "2026-10-20"}
    assert patch["body"]["end"] == {"date": "2026-10-22"}
    assert all_day.event["start"] == {"date": "2026-10-20"}


def test_bare_dates_turn_a_timed_event_all_day(cal: FakeCalendar) -> None:
    out = _call(
        "gws_calendar_update", {"event_id": "meeting", "start": "2026-10-20", "end": "2026-10-21"}
    )
    assert out["verification"] == "verified", out
    [patch] = cal.patches()
    assert patch["body"]["start"]["date"] == "2026-10-20"
    assert patch["body"]["start"]["dateTime"] is None
    assert cal.event["start"] == {"date": "2026-10-20"}


@pytest.mark.parametrize(
    "args",
    [
        {"start": "2026-10-20", "end": "2026-10-20T10:00:00"},
        {"start": "2026-10-21", "end": "2026-10-20"},
        {"start": "2026-13-40"},
    ],
)
def test_mixed_or_backwards_dates_are_refused(cal: FakeCalendar, args: dict[str, Any]) -> None:
    out = _call("gws_calendar_update", {"event_id": "meeting", **args})
    assert "error" in out
    assert cal.patches() == []


def test_a_timed_start_on_an_all_day_event_needs_an_end(all_day: FakeCalendar) -> None:
    out = _call("gws_calendar_update", {"event_id": "meeting", "start": "2026-10-20T10:00:00"})
    assert "pass end" in out["error"]
    assert all_day.patches() == []


# ── Effect ledger + evidence ──────────────────────────────────────────


@pytest.mark.parametrize(
    "tool", ["gws_calendar_update", "gws_calendar_add_attendees", "gws_calendar_respond"]
)
def test_calendar_edits_go_through_the_effect_ledger(tool: str) -> None:
    """add_attendees bypassed the ledger because the retired draft flow kept its
    own. That ledger no longer receives writes, so the bypass left edits with no
    durable record at all."""
    from types import SimpleNamespace

    from robothor.engine.runtime.effect_dispatch import _bypass

    assert _bypass(tool, {"event_id": "meeting"}, SimpleNamespace(is_benchmark=False)) is False


@pytest.mark.asyncio
async def test_a_verified_edit_names_the_evidence_reference_for_its_effect(
    cal: FakeCalendar,
) -> None:
    from uuid import uuid4

    from robothor.engine.runtime import effects
    from robothor.engine.tools.dispatch import ToolContext

    effect_id = uuid4()
    token = effects.active_effect.set({"id": effect_id})
    try:
        out = await gws_handlers.HANDLERS["gws_calendar_update"](
            {"event_id": "meeting", "summary": "Moved"}, ToolContext(tenant_id="fixture")
        )
    finally:
        effects.active_effect.reset(token)
    assert out["evidence_reference"] == f"calendar-effect:{effect_id}"


@pytest.mark.asyncio
async def test_an_unverified_edit_offers_no_evidence(cal: FakeCalendar) -> None:
    from uuid import uuid4

    from robothor.engine.runtime import effects
    from robothor.engine.tools.dispatch import ToolContext

    cal.failure = {"error": "conflict", "status_code": 412}
    token = effects.active_effect.set({"id": uuid4()})
    try:
        out = await gws_handlers.HANDLERS["gws_calendar_update"](
            {"event_id": "meeting", "summary": "Moved"}, ToolContext(tenant_id="fixture")
        )
    finally:
        effects.active_effect.reset(token)
    assert "evidence_reference" not in out


def test_a_new_style_step_is_not_reported_as_an_unmatched_legacy_operation() -> None:
    """New edits carry no operation_id; their receipt is the runtime effect.
    The legacy reader must not invent an `unmatched` calendar receipt for them."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from robothor.engine.chat_receipts import calendar_receipts

    cur = MagicMock()
    cur.fetchall.return_value = [
        {
            "tool_name": "gws_calendar_add_attendees",
            "tool_input": {"event_id": "meeting", "attendees": ["d@example.com"]},
            "tool_output": {"status": "updated", "verification": "verified"},
        }
    ]
    auth = SimpleNamespace(tenant_id="fixture", user_id="owner")
    assert calendar_receipts(cur, {"id": "run", "agent_id": "main"}, auth) == []
