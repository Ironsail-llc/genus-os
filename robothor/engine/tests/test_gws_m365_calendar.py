"""The REAL gws calendar handlers on Microsoft 365, against an in-memory Exchange.

``workspace_provider=microsoft365`` routes every ``gws_calendar_*`` call to
:class:`~robothor.workspace.microsoft.calendar.GraphCalendar`; the guards in
front of it (do-not-contact, no-auto scheduling, dedup, calendar identity,
cancellation) are the same code the Google path runs. These tests drive the
tool handlers end to end and read what reached Graph.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import gws
from robothor.workspace.bridge import bind_engine_loop
from robothor.workspace.microsoft.graph import GraphClient
from robothor.workspace.tests.fake_graph import FakeGraphTenant
from robothor.workspace.tests.fake_graph_calendar import FakeExchangeCalendar, install_calendar

ASSISTANT = "assistant@example.com"
OWNER = "owner@example.com"
GUEST = "guest@example.com"
BLOCKED = "optout@example.com"
NO_AUTO = "noauto@example.com"
WRITES = ("POST", "PATCH", "DELETE")


class StaticToken:
    def __init__(self, tenant: FakeGraphTenant) -> None:
        tenant.issued_tokens.append("static-token")

    async def token(self) -> str:
        return "static-token"


class Env:
    def __init__(self, tenant: FakeGraphTenant, exchange: FakeExchangeCalendar) -> None:
        self.tenant = tenant
        self.exchange = exchange
        self.crm: list[dict[str, Any]] = []
        self.send_updates = "all"

    def writes(self) -> list[Any]:
        return [r for r in self.tenant.requests if r.method in WRITES]

    def requests(self, method: str, pattern: str) -> list[Any]:
        return self.tenant.requests_matching(method, pattern)

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        ctx = ToolContext(agent_id="main", run_id="run-1", tenant_id="tenant-a")
        return await gws.HANDLERS[tool](args, ctx)

    def meeting(self, mailbox: str = OWNER, **extra: Any) -> dict[str, Any]:
        event = {
            "subject": "Planning",
            "start": {"dateTime": "2026-10-08T10:00:00", "timeZone": "Eastern Standard Time"},
            "end": {"dateTime": "2026-10-08T10:30:00", "timeZone": "Eastern Standard Time"},
            "organizer": {"emailAddress": {"address": mailbox}},
            "attendees": [
                {
                    "type": "required",
                    "status": {"response": "accepted"},
                    "emailAddress": {"address": GUEST},
                }
            ],
        }
        event.update(extra)
        return self.exchange.add(mailbox, event)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Env:
    import yaml

    from robothor import workspace
    from robothor.settings import reset_settings

    owner = tmp_path / "owner.yaml"
    owner.write_text(yaml.safe_dump({"first_name": "Alice", "email": OWNER}))
    monkeypatch.setenv("ROBOTHOR_OWNER_CONFIG", str(owner))
    monkeypatch.setenv("ROBOTHOR_WORKSPACE_PROVIDER", "microsoft365")
    monkeypatch.setenv("ROBOTHOR_M365_ASSISTANT_MAILBOX", ASSISTANT)
    monkeypatch.setenv("ROBOTHOR_M365_OWNER_MAILBOX", OWNER)
    monkeypatch.setenv("ROBOTHOR_TIMEZONE", "America/New_York")
    reset_settings()
    workspace.reset_workspace_cache()

    tenant = FakeGraphTenant()
    state = Env(tenant, install_calendar(tenant))

    async def fake_client(tenant_id: str = "default", **_: Any) -> GraphClient:
        return GraphClient(StaticToken(tenant), transport=tenant.transport())

    monkeypatch.setattr("robothor.workspace.microsoft.graph_client_from_vault", fake_client)
    monkeypatch.setattr(gws, "ROBOTHOR_EMAIL", ASSISTANT)

    def no_cli(*a: Any, **k: Any) -> Any:
        raise AssertionError("a Microsoft 365 calendar call reached the gws CLI")

    monkeypatch.setattr(gws, "_run_gws", no_cli)
    monkeypatch.setattr(
        "robothor.crm.dal.do_not_contact_emails",
        lambda emails, tenant_id="default": {e.lower() for e in emails if e.lower() == BLOCKED},
    )
    monkeypatch.setattr("robothor.engine.tracking.log_guardrail_event", lambda *a, **k: None)
    monkeypatch.setattr(gws, "_dnc_mode", lambda: "enforce")
    monkeypatch.setattr(
        "robothor.engine.guardrails._lookup_scheduling_policies",
        lambda emails: {e: "no_auto" for e in emails if e == NO_AUTO},
    )
    monkeypatch.setattr(
        "robothor.engine.feature_flags.calendar_send_updates", lambda: state.send_updates
    )
    monkeypatch.setattr(gws, "_record_calendar_event", lambda **kw: state.crm.append(kw))
    yield state
    reset_settings()
    workspace.reset_workspace_cache()


# ── list ──────────────────────────────────────────────────────────────


async def test_list_own_reads_the_assistant_mailbox(env: Env) -> None:
    env.meeting(ASSISTANT, subject="Mine")
    env.meeting(OWNER, subject="Theirs")
    listed = await env.call(
        "gws_calendar_list",
        {"time_min": "2026-10-01T00:00:00Z", "time_max": "2026-10-31T00:00:00Z", "calendar": "own"},
    )
    assert listed["calendar"] == {"kind": "own", "id": ASSISTANT}
    assert [e["summary"] for e in listed["items"]] == ["Mine"]
    assert env.requests("GET", f"/users/{ASSISTANT}/calendar/calendarView")


async def test_list_defaults_to_the_operator_mailbox(env: Env) -> None:
    env.meeting(OWNER, subject="Theirs")
    listed = await env.call("gws_calendar_list", {"time_min": "2026-10-01T00:00:00Z"})
    assert listed["calendar"] == {"kind": "operator", "id": OWNER}
    (event,) = listed["items"]
    assert event["start"] == {
        "dateTime": "2026-10-08T10:00:00-04:00",
        "timeZone": "America/New_York",
    }
    assert event["attendees"] == [{"email": GUEST, "responseStatus": "accepted"}]
    assert event["organizer"] == {"email": OWNER, "self": True}


# ── create ────────────────────────────────────────────────────────────


async def test_create_invites_and_writes_through_with_the_immutable_id(env: Env) -> None:
    out = await env.call(
        "gws_calendar_create",
        {
            "summary": "Kickoff",
            "start": "2026-10-09T14:00:00-04:00",
            "end": "2026-10-09T15:00:00-04:00",
            "attendees": [GUEST],
            "with_meet": False,
        },
    )
    assert "error" not in out, out
    assert out["calendar"] == {"kind": "operator", "id": OWNER}
    assert out["id"].startswith("AAkALg-imm-")
    assert out["invitations_requested"] is True
    assert out["send_updates"] == "all"
    assert out["attendees_notified"] == [GUEST]
    (post,) = env.requests("POST", f"/users/{OWNER}/calendar/events")
    sent = json.loads(post.content)
    assert sent["start"] == {"dateTime": "2026-10-09T14:00:00", "timeZone": "America/New_York"}
    assert "isOnlineMeeting" not in sent
    assert env.exchange.notifications == [{"kind": "invite", "to": [GUEST], "event": out["id"]}]
    (record,) = env.crm
    assert record["provider"] == "microsoft365"
    assert record["result"]["id"] == out["id"]


async def test_create_with_meet_makes_a_teams_meeting(env: Env) -> None:
    out = await env.call(
        "gws_calendar_create",
        {
            "summary": "Sync",
            "start": "2026-10-09T14:00:00-04:00",
            "end": "2026-10-09T14:30:00-04:00",
            "calendar": "own",
        },
    )
    assert "error" not in out, out
    (post,) = env.requests("POST", f"/users/{ASSISTANT}/calendar/events")
    sent = json.loads(post.content)
    assert sent["isOnlineMeeting"] is True
    assert sent["onlineMeetingProvider"] == "teamsForBusiness"
    # The operator is invited to an event on the assistant's calendar.
    assert sent["attendees"] == [{"emailAddress": {"address": OWNER}, "type": "required"}]
    assert out["conferenceData"]["entryPoints"][0]["uri"].startswith(
        "https://teams.microsoft.example/"
    )


async def test_duplicate_is_deduped_without_a_write(env: Env) -> None:
    seeded = env.meeting(OWNER, subject="Planning")
    out = await env.call(
        "gws_calendar_create",
        {
            "summary": "Planning",
            "start": "2026-10-08T10:00:00-04:00",
            "end": "2026-10-08T10:30:00-04:00",
            "attendees": [GUEST],
        },
    )
    assert out["status"] == "deduped"
    assert out["existing_event_id"] == seeded["id"]
    assert env.writes() == []
    assert env.crm == []


async def test_force_bypasses_dedup(env: Env) -> None:
    env.meeting(OWNER, subject="Planning")
    out = await env.call(
        "gws_calendar_create",
        {
            "summary": "Planning",
            "start": "2026-10-08T10:00:00-04:00",
            "end": "2026-10-08T10:30:00-04:00",
            "attendees": [GUEST],
            "force": True,
            "with_meet": False,
        },
    )
    assert "error" not in out, out
    assert len(env.requests("POST", f"/users/{OWNER}/calendar/events")) == 1
    # force skips the dedup read too.
    assert env.requests("GET", ".*calendarView") == []


async def test_do_not_contact_attendee_means_zero_graph_writes(env: Env) -> None:
    out = await env.call(
        "gws_calendar_create",
        {
            "summary": "Pitch",
            "start": "2026-10-09T14:00:00-04:00",
            "end": "2026-10-09T15:00:00-04:00",
            "attendees": [BLOCKED],
        },
    )
    assert out["guard"] == "do_not_contact"
    assert env.tenant.requests == []

    seeded = env.meeting(OWNER)
    out = await env.call(
        "gws_calendar_update", {"event_id": seeded["id"], "add_attendees": [BLOCKED]}
    )
    assert out["guard"] == "do_not_contact"
    assert env.writes() == []


async def test_quiet_send_updates_with_attendees_is_refused_before_writing(env: Env) -> None:
    env.send_updates = "none"
    out = await env.call(
        "gws_calendar_create",
        {
            "summary": "Kickoff",
            "start": "2026-10-09T14:00:00-04:00",
            "end": "2026-10-09T15:00:00-04:00",
            "attendees": [GUEST],
        },
    )
    assert out["hint"] == "unsupported"
    assert "always notifies" in out["error"]
    assert env.writes() == []
    # The write-through sees the refusal and records nothing (it skips errors).
    assert all("error" in record["result"] for record in env.crm)


# ── edits ─────────────────────────────────────────────────────────────


async def test_update_retries_once_after_a_412(env: Env) -> None:
    seeded = env.meeting(OWNER)
    env.tenant.fail("PATCH", f"/users/{OWNER}/events/.*", 412, times=1)
    out = await env.call(
        "gws_calendar_update",
        {"event_id": seeded["id"], "summary": "Moved", "start": "2026-10-08T15:00:00-04:00"},
    )
    assert out["status"] == "updated", out
    assert out["verification"] == "verified"
    patches = env.requests("PATCH", f"/users/{OWNER}/events/.*")
    assert len(patches) == 2
    assert patches[0].headers["if-match"] == patches[1].headers["if-match"]
    sent = json.loads(patches[1].content)
    assert sent["subject"] == "Moved"
    # Moving the start keeps the meeting's length.
    assert sent["start"] == {"dateTime": "2026-10-08T15:00:00", "timeZone": "America/New_York"}
    assert sent["end"] == {"dateTime": "2026-10-08T15:30:00", "timeZone": "America/New_York"}
    assert out["attendees_notified"] == [GUEST]


async def test_add_attendees_keeps_existing_guests(env: Env) -> None:
    seeded = env.meeting(OWNER)
    out = await env.call(
        "gws_calendar_add_attendees",
        {"event_id": seeded["id"], "attendees": ["new@example.com"]},
    )
    assert out["status"] == "updated", out
    assert out["added"] == ["new@example.com"]
    (patch,) = env.requests("PATCH", f"/users/{OWNER}/events/.*")
    assert patch.headers["if-match"].startswith('W/"')
    assert [a["emailAddress"]["address"] for a in json.loads(patch.content)["attendees"]] == [
        GUEST,
        "new@example.com",
    ]
    assert {a["email"]: a["responseStatus"] for a in out["attendees"]} == {
        GUEST: "accepted",
        "new@example.com": "needsAction",
    }


async def test_no_auto_policy_refuses_before_any_request(env: Env) -> None:
    seeded = env.meeting(OWNER)
    env.tenant.requests.clear()
    out = await env.call(
        "gws_calendar_update", {"event_id": seeded["id"], "add_attendees": [NO_AUTO]}
    )
    assert out["guardrail"] == "scheduling_policy"
    assert env.tenant.requests == []


async def test_cancellation_before_patch_sends_no_patch(env: Env) -> None:
    seeded = env.meeting(OWNER)

    class CancelAfterRead:
        """Not cancelled when the edit starts; cancelled by the time it would write."""

        def __init__(self) -> None:
            self.checks = 0

        def is_set(self) -> bool:
            self.checks += 1
            return self.checks > 1

    cancelled = CancelAfterRead()
    with bind_engine_loop(asyncio.get_running_loop()):
        out = await asyncio.to_thread(
            gws._calendar_update,
            {"event_id": seeded["id"], "summary": "Moved"},
            run_id="run-1",
            tenant_id="tenant-a",
            cancelled=cancelled,
        )
    assert out["error"] == "Calendar operation cancelled before write"
    assert env.requests("GET", f"/users/{OWNER}/events/.*")
    assert env.writes() == []


async def test_respond_accept_uses_the_graph_action(env: Env) -> None:
    seeded = env.meeting(
        OWNER,
        organizer={"emailAddress": {"address": GUEST}},
        attendees=[
            {
                "type": "required",
                "status": {"response": "none"},
                "emailAddress": {"address": OWNER},
            }
        ],
    )
    out = await env.call(
        "gws_calendar_respond",
        {"event_id": seeded["id"], "response": "accepted", "comment": "See you there"},
    )
    assert out["status"] == "updated", out
    assert out["verification"] == "verified"
    (post,) = env.requests("POST", f"/users/{OWNER}/events/.*/accept")
    assert json.loads(post.content) == {"sendResponse": True, "comment": "See you there"}
    assert env.requests("PATCH", ".*") == []
    assert env.exchange.notifications[-1]["to"] == [GUEST]


async def test_respond_to_a_do_not_contact_organiser_sends_nothing(env: Env) -> None:
    seeded = env.meeting(
        OWNER,
        organizer={"emailAddress": {"address": BLOCKED}},
        attendees=[
            {
                "type": "required",
                "status": {"response": "none"},
                "emailAddress": {"address": OWNER},
            }
        ],
    )
    out = await env.call("gws_calendar_respond", {"event_id": seeded["id"], "response": "declined"})
    assert out["guard"] == "do_not_contact"
    assert env.writes() == []


async def test_delete_of_a_meeting_cancels_it(env: Env) -> None:
    seeded = env.meeting(OWNER)
    out = await env.call("gws_calendar_delete", {"event_id": seeded["id"]})
    assert "error" not in out, out
    assert out["method"] == "cancel"
    assert out["send_updates"] == "all"
    assert env.requests("POST", f"/users/{OWNER}/events/.*/cancel")
    assert env.requests("DELETE", ".*") == []
    assert env.exchange.notifications[-1]["kind"] == "cancel"


# ── mail is still dark on this branch ─────────────────────────────────


async def test_mail_tools_still_refuse_on_microsoft365(env: Env) -> None:
    out = await env.call("gws_gmail_send", {"to": GUEST, "subject": "S", "body": "B"})
    assert out["hint"] == "unsupported"
    assert env.tenant.requests == []
