"""The provider contract: every guard holds, the same way, on Google and Microsoft 365.

Each scenario runs once per provider (``workspace_env`` is parameterised) and
drives the REAL ``gws_*`` handlers. Where the providers legitimately differ,
the difference is read from ``env.capabilities`` -- the declaration the
handlers themselves consult -- never from the provider's name.
"""

from __future__ import annotations

from typing import Any

from robothor.engine.tools.constants import WORKSPACE_TOOLS
from robothor.workspace.tests.contract.conftest import (
    ASSISTANT,
    BLOCKED,
    BOB,
    CAROL,
    DAVE,
    NO_AUTO,
    OPERATOR,
    TENANT,
    WorkspaceEnv,
)

MEETING = {
    "summary": "Quarterly review",
    "start": "2026-10-09T14:00:00-04:00",
    "end": "2026-10-09T15:00:00-04:00",
    "attendees": [BOB],
    "with_meet": False,
}


# ── mail ──────────────────────────────────────────────────────────────


async def test_search_get_reply_stays_in_the_conversation(workspace_env: WorkspaceEnv) -> None:
    env = workspace_env
    env.deliver(sender=DAVE, to=[ASSISTANT], subject="Unrelated")
    original = env.deliver(
        sender=BOB, to=[ASSISTANT], cc=[CAROL], subject="Budget", body="Can we approve it?"
    )

    found = await env.call("gws_gmail_search", {"query": "from:bob@example.com is:unread"})
    assert [m["id"] for m in found["messages"]] == [original["id"]]
    (hit,) = found["messages"]
    assert hit["thread_id"] == original["thread_id"]

    message = await env.call("gws_gmail_get", {"message_id": hit["id"]})
    assert message["body_text"].strip() == "Can we approve it?"
    assert message["thread_id"] == original["thread_id"]

    reply = await env.call("gws_gmail_reply", {"thread_id": hit["thread_id"], "body": "Approved."})
    assert "error" not in reply, reply
    assert reply["threadId"] == original["thread_id"]
    sent = env.sent(reply["id"])
    assert sent["thread_id"] == original["thread_id"]
    # The handler's reply-all: everyone on the thread except the assistant.
    assert sent["to"] == [BOB, CAROL]
    assert sent["cc"] == []
    assert sent["body"].strip() == "Approved."


async def test_send_returns_an_id_the_verification_reads_back(workspace_env) -> None:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.verification import verify_tool_result

    env = workspace_env
    args = {"to": BOB, "subject": "Agenda", "body": "See attached agenda."}
    sent = await env.call("gws_gmail_send", args)
    assert "error" not in sent, sent
    assert sent["id"]

    readable = await env.call("gws_gmail_get", {"message_id": sent["id"]})
    assert readable["id"] == sent["id"]
    assert readable["subject"] == "Agenda"
    assert env.sent(sent["id"])["to"] == [BOB]

    # The engine's post-condition check reads the same id back through the
    # same provider, and an enforced verification leaves the result clean.
    ctx = ToolContext(agent_id="main", run_id="run-contract", tenant_id=TENANT)
    checked = await verify_tool_result("gws_gmail_send", args, dict(sent), ctx)
    assert "verification_failed" not in checked, checked
    evidence = env.crm_rows("agent_run_evidence")
    assert evidence and evidence[-1][3] == f"gmail:{sent['id']}" and evidence[-1][4] is True


async def test_modify_labels_round_trip(workspace_env) -> None:
    env = workspace_env
    seeded = env.deliver(sender=BOB, to=[ASSISTANT], subject="Flag me")

    marked = await env.call(
        "gws_gmail_modify",
        {"message_id": seeded["id"], "add_labels": ["STARRED"], "remove_labels": ["UNREAD"]},
    )
    assert "error" not in marked, marked
    labels = (await env.call("gws_gmail_get", {"message_id": seeded["id"]}))["labels"]
    assert "STARRED" in labels and "UNREAD" not in labels

    await env.call(
        "gws_gmail_modify",
        {"message_id": seeded["id"], "add_labels": ["UNREAD"], "remove_labels": ["STARRED"]},
    )
    labels = (await env.call("gws_gmail_get", {"message_id": seeded["id"]}))["labels"]
    assert "UNREAD" in labels and "STARRED" not in labels


async def test_duplicate_reply_is_skipped_with_zero_writes(workspace_env) -> None:
    env = workspace_env
    original = env.deliver(sender=BOB, to=[ASSISTANT], subject="Question")
    env.deliver(
        sender=ASSISTANT,
        to=[BOB],
        subject="RE: Question",
        thread_id=original["thread_id"],
        from_assistant=True,
    )
    before = env.writes()

    out = await env.call("gws_gmail_reply", {"thread_id": original["thread_id"], "body": "again"})
    assert out["status"] == "skipped"

    out = await env.call(
        "gws_gmail_send",
        {"to": BOB, "subject": "RE: Question", "body": "again", "thread_id": original["thread_id"]},
    )
    assert out["status"] == "skipped"
    assert env.writes() == before


# ── do-not-contact ────────────────────────────────────────────────────


async def test_do_not_contact_send_and_reply_write_nothing(workspace_env) -> None:
    env = workspace_env
    thread = env.deliver(sender=BOB, to=[ASSISTANT], cc=[BLOCKED], subject="Offer")
    before = env.writes()

    send = await env.call("gws_gmail_send", {"to": BLOCKED, "subject": "Hi", "body": "x"})
    assert send["guard"] == "do_not_contact"
    reply = await env.call("gws_gmail_reply", {"thread_id": thread["thread_id"], "body": "x"})
    assert reply["guard"] == "do_not_contact"
    assert env.writes() == before
    assert env.crm_rows("message") == []


async def test_do_not_contact_calendar_writes_nothing(workspace_env) -> None:
    env = workspace_env
    meeting = env.seed_meeting(organizer=OPERATOR, attendees={BOB: "accepted"})
    invitation = env.seed_meeting(
        organizer=BLOCKED, attendees={OPERATOR: "needsAction"}, summary="Their invite"
    )
    before_requests = env.requests()

    created = await env.call("gws_calendar_create", {**MEETING, "attendees": [BLOCKED]})
    assert created["guard"] == "do_not_contact"
    # Refused before even the dedup read: nothing reached the provider.
    assert env.requests() == before_requests

    before = env.writes()
    added = await env.call(
        "gws_calendar_add_attendees", {"event_id": meeting, "attendees": [BLOCKED]}
    )
    assert added["guard"] == "do_not_contact"
    updated = await env.call(
        "gws_calendar_update", {"event_id": meeting, "add_attendees": [BLOCKED]}
    )
    assert updated["guard"] == "do_not_contact"
    rsvp = await env.call("gws_calendar_respond", {"event_id": invitation, "response": "declined"})
    assert rsvp["guard"] == "do_not_contact"
    assert env.writes() == before
    assert env.crm_rows("calendar_event") == []


# ── scheduling policy ─────────────────────────────────────────────────


async def test_no_auto_scheduling_policy_refuses_with_zero_requests(workspace_env) -> None:
    env = workspace_env
    meeting = env.seed_meeting(organizer=OPERATOR, attendees={BOB: "accepted"})
    before = env.requests()

    for tool, args in (
        ("gws_calendar_update", {"event_id": meeting, "add_attendees": [NO_AUTO]}),
        ("gws_calendar_add_attendees", {"event_id": meeting, "attendees": [NO_AUTO]}),
    ):
        out = await env.call(tool, args)
        assert out["guardrail"] == "scheduling_policy", (tool, out)
        assert out["invitations_requested"] is False
    assert env.requests() == before


# ── dedup ─────────────────────────────────────────────────────────────


async def test_second_identical_create_is_deduped_and_force_bypasses(workspace_env) -> None:
    env = workspace_env
    first = await env.call("gws_calendar_create", dict(MEETING))
    assert "error" not in first, first
    before = env.writes()

    again = await env.call("gws_calendar_create", dict(MEETING))
    assert again["status"] == "deduped"
    assert again["existing_event_id"] == first["id"]
    assert again["invitations_requested"] is False
    assert env.writes() == before

    forced = await env.call("gws_calendar_create", {**MEETING, "force": True})
    assert "error" not in forced, forced
    assert forced["id"] != first["id"]
    assert env.writes() == before + 1


async def test_created_event_is_verified_on_the_calendar_it_landed_on(workspace_env) -> None:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.verification import verify_tool_result

    env = workspace_env
    created = await env.call("gws_calendar_create", dict(MEETING))
    assert created["calendar"]["kind"] == "operator"
    ctx = ToolContext(agent_id="main", run_id="run-contract", tenant_id=TENANT)
    checked = await verify_tool_result("gws_calendar_create", dict(MEETING), dict(created), ctx)
    assert "verification_failed" not in checked, checked
    evidence = env.crm_rows("agent_run_evidence")
    assert evidence[-1][3] == f"calendar:{created['id']}" and evidence[-1][4] is True


# ── calendar lifecycle ────────────────────────────────────────────────


async def test_calendar_lifecycle_each_step_read_back(workspace_env) -> None:
    env = workspace_env

    created = await env.call("gws_calendar_create", dict(MEETING))
    assert "error" not in created, created
    assert created["calendar"]["kind"] == "operator"
    assert created["attendees_notified"] == [BOB]
    event_id = created["id"]
    shown = await env.read_event(event_id)
    assert shown is not None and shown["summary"] == "Quarterly review"
    assert set(env.responses(shown)) == {BOB}

    # Somebody else edits the event between our read and our write: the write
    # gets a 412 (precondition failed), is re-merged once, and keeps their change.
    env.concurrent_edit_before_next_write(event_id, location="Room 9")
    patches = env.event_patches()
    moved = await env.call(
        "gws_calendar_update",
        {
            "event_id": event_id,
            "summary": "Quarterly review (moved)",
            "start": "2026-10-09T16:00:00-04:00",
        },  # fmt: skip
    )
    assert moved["status"] == "updated", moved
    assert moved["verification"] == "verified"
    assert env.event_patches() == patches + 2
    shown = await env.read_event(event_id)
    assert shown["summary"] == "Quarterly review (moved)"
    assert shown["location"] == "Room 9"

    added = await env.call(
        "gws_calendar_add_attendees", {"event_id": event_id, "attendees": [CAROL]}
    )
    assert added["status"] == "updated", added
    assert added["added"] == [CAROL]
    shown = await env.read_event(event_id)
    assert set(env.responses(shown)) == {BOB, CAROL}

    deleted = await env.call("gws_calendar_delete", {"event_id": event_id})
    assert "error" not in deleted, deleted
    assert deleted["event_id"] == event_id
    assert await env.read_event(event_id) is None


async def test_respond_accept_reads_back_accepted(workspace_env) -> None:
    env = workspace_env
    invitation = env.seed_meeting(
        organizer=DAVE, attendees={OPERATOR: "needsAction", BOB: "accepted"}, summary="Dave's sync"
    )
    out = await env.call("gws_calendar_respond", {"event_id": invitation, "response": "accepted"})
    assert out["status"] == "updated", out
    assert out["verification"] == "verified"
    shown = await env.read_event(invitation)
    assert env.responses(shown)[OPERATOR] == "accepted"
    assert env.responses(shown)[BOB] == "accepted"


async def test_cancellation_before_the_write_writes_nothing(workspace_env) -> None:
    env = workspace_env
    meeting = env.seed_meeting(organizer=OPERATOR, attendees={BOB: "accepted"})
    before = env.writes()
    out = await env.call_cancelled_after_read(
        "gws_calendar_update", {"event_id": meeting, "summary": "Moved"}
    )
    assert out["error"] == "Calendar operation cancelled before write"
    assert out["invitations_requested"] is False
    assert env.writes() == before
    assert (await env.read_event(meeting))["summary"] == "Planning"


# ── benchmark ─────────────────────────────────────────────────────────

_PLAUSIBLE_ARGS: dict[str, dict[str, Any]] = {
    "gws_gmail_search": {"query": "from:bob@example.com"},
    "gws_gmail_get": {"message_id": "m1"},
    "gws_gmail_send": {"to": BOB, "subject": "s", "body": "b"},
    "gws_gmail_reply": {"thread_id": "t1", "body": "b"},
    "gws_gmail_modify": {"message_id": "m1", "add_labels": ["STARRED"]},
    "gws_calendar_list": {"time_min": "2026-10-01T00:00:00Z"},
    "gws_calendar_create": dict(MEETING),
    "gws_calendar_update": {"event_id": "e1", "summary": "x"},
    "gws_calendar_add_attendees": {"event_id": "e1", "attendees": [BOB]},
    "gws_calendar_respond": {"event_id": "e1", "response": "accepted"},
    "gws_calendar_delete": {"event_id": "e1"},
    "gws_chat_send": {"space": "spaces/a", "text": "hi"},
    "gws_chat_list_spaces": {},
    "gws_chat_list_messages": {"space": "spaces/a"},
}


async def test_benchmark_context_refuses_every_tool_with_zero_requests(workspace_env) -> None:
    env = workspace_env
    assert set(_PLAUSIBLE_ARGS) == set(WORKSPACE_TOOLS)
    for tool in sorted(WORKSPACE_TOOLS):
        out = await env.call(tool, _PLAUSIBLE_ARGS[tool], benchmark=True)
        assert out["guard"] == "is_benchmark", (tool, out)
    assert env.requests() == 0
    assert env.crm_sql == []


# ── chat ──────────────────────────────────────────────────────────────


async def test_chat_tools_follow_the_chat_capability(workspace_env, monkeypatch) -> None:
    env = workspace_env
    if env.capabilities["chat"] == "google_chat":
        cli: list[list[str]] = []
        monkeypatch.setattr(
            "robothor.engine.tools.handlers.gws._run_gws",
            lambda args, timeout=30: cli.append(args) or {"spaces": []},
        )
        out = await env.call("gws_chat_list_spaces", {})
        assert out == {"spaces": []}
        assert cli and cli[0][:2] == ["chat", "spaces"]
        return
    assert env.capabilities["chat"] == "none"
    for tool in ("gws_chat_send", "gws_chat_list_spaces", "gws_chat_list_messages"):
        out = await env.call(tool, _PLAUSIBLE_ARGS[tool])
        assert out["hint"] == "unsupported", (tool, out)
        assert "Google Chat is not available" in out["error"]
    assert env.requests() == 0


# ── CRM write-through ─────────────────────────────────────────────────


async def test_crm_rows_carry_the_provider_and_its_ids(workspace_env) -> None:
    env = workspace_env
    sent = await env.call("gws_gmail_send", {"to": BOB, "subject": "Rows", "body": "b"})
    (thread_row,) = env.crm_rows("message_thread")
    assert thread_row[1] == sent["threadId"]
    (message_row,) = env.crm_rows("message")
    assert message_row[2] == sent["id"]

    created = await env.call("gws_calendar_create", dict(MEETING))
    (event_row,) = env.crm_rows("calendar_event")
    provider, external_id, google_id = event_row[1], event_row[2], event_row[3]
    assert provider == env.capabilities["provider"]
    assert external_id == created["id"]
    # The legacy `google_event_id` column is dual-written for Google ids only.
    legacy = created["id"] if env.capabilities["provider"] == "google" else None
    assert google_id == legacy
    participants = env.crm_rows("calendar_event_participant")
    assert [p[4] for p in participants] == [BOB]
