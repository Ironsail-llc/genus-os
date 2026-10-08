"""The Microsoft 365 done-definition, end to end, in one simulated working day.

One fake tenant, the real ``gws_*`` handlers, in the order an instance would
meet them:

1. an email arrives in the assistant's inbox;
2. it is triaged the way email-classifier does it -- a search, then a read;
3. a reply is drafted and sent in the SAME conversation, reply-all;
4. a meeting is created, edited (through one 412 from a concurrent change),
   accepted (an invitation from the sender) and cancelled -- each read back;
5. a do-not-contact recipient is blocked with nothing written to any mailbox;
6. a duplicate meeting is caught without a second write.

Inbound ingestion (the delta poll firing ``email.new``, and a forced delta
reset not replaying old mail) is the delta-ingestion PR's suite; this flow
starts from a message already in the inbox.
"""

from __future__ import annotations

from robothor.workspace.tests.contract.conftest import (
    ASSISTANT,
    BLOCKED,
    BOB,
    CAROL,
    OPERATOR,
    WorkspaceEnv,
)


async def test_an_ordinary_day_on_microsoft_365(m365_env: WorkspaceEnv) -> None:
    env = m365_env
    exchange_calendar = env.calendar  # type: ignore[attr-defined]

    # 1. An email arrives.
    arrived = env.deliver(
        sender=BOB,
        to=[ASSISTANT],
        cc=[CAROL],
        subject="Kickoff next week?",
        body="Could we meet Friday at 2pm to kick off?",
    )

    # 2. Triage: find it, then read it.
    found = await env.call("gws_gmail_search", {"query": "is:unread from:bob@example.com"})
    assert [m["id"] for m in found["messages"]] == [arrived["id"]]
    read = await env.call("gws_gmail_get", {"message_id": arrived["id"]})
    assert "Friday at 2pm" in read["body_text"]
    thread = read["thread_id"]
    assert thread == arrived["thread_id"]

    # 3. Reply in the same conversation.
    reply = await env.call(
        "gws_gmail_reply", {"thread_id": thread, "body": "Friday at 2pm works. Invite coming."}
    )
    assert "error" not in reply, reply
    sent = env.sent(reply["id"])
    assert sent["thread_id"] == thread
    assert sent["to"] == [BOB, CAROL]
    conversation = await env.call("gws_gmail_get", {"thread_id": thread})
    assert [m["id"] for m in conversation["messages"]] == [arrived["id"], reply["id"]]

    # 4a. Create the meeting.
    meeting = {
        "summary": "Project kickoff",
        "start": "2026-10-09T14:00:00-04:00",
        "end": "2026-10-09T15:00:00-04:00",
        "attendees": [BOB, CAROL],
        "with_meet": True,
    }
    created = await env.call("gws_calendar_create", dict(meeting))
    assert "error" not in created, created
    assert created["calendar"] == {"kind": "operator", "id": OPERATOR}
    assert created["attendees_notified"] == [BOB, CAROL]
    event_id = created["id"]
    assert (await env.read_event(event_id))["summary"] == "Project kickoff"

    # 4b. Edit it while somebody else is editing it too: one 412, one re-merge.
    env.concurrent_edit_before_next_write(event_id, location="Board room")
    patches = env.event_patches()
    edited = await env.call(
        "gws_calendar_update",
        {"event_id": event_id, "start": "2026-10-09T15:00:00-04:00", "description": "Agenda"},
    )
    assert edited["status"] == "updated" and edited["verification"] == "verified", edited
    assert env.event_patches() == patches + 2
    shown = await env.read_event(event_id)
    assert shown["location"] == "Board room"
    assert shown["start"]["dateTime"] == "2026-10-09T15:00:00-04:00"
    assert shown["end"]["dateTime"] == "2026-10-09T16:00:00-04:00"  # the length is kept

    # 4c. Bob sends his own invitation for a prep call; accept it.
    prep = env.seed_meeting(
        organizer=BOB,
        attendees={OPERATOR: "needsAction"},
        summary="Kickoff prep",
        start="2026-10-08T09:00:00",
        end="2026-10-08T09:15:00",
    )
    accepted = await env.call(
        "gws_calendar_respond", {"event_id": prep, "response": "accepted", "comment": "See you"}
    )
    assert accepted["status"] == "updated" and accepted["verification"] == "verified", accepted
    assert env.responses(await env.read_event(prep))[OPERATOR] == "accepted"
    assert exchange_calendar.notifications[-1]["kind"] == "response"
    assert exchange_calendar.notifications[-1]["to"] == [BOB]

    # 5. A do-not-contact recipient: nothing is written, anywhere.
    before = env.writes()
    blocked_mail = await env.call(
        "gws_gmail_send", {"to": BLOCKED, "subject": "Kickoff", "body": "Join us?"}
    )
    blocked_invite = await env.call(
        "gws_calendar_add_attendees", {"event_id": event_id, "attendees": [BLOCKED]}
    )
    assert blocked_mail["guard"] == blocked_invite["guard"] == "do_not_contact"
    assert env.writes() == before
    assert BLOCKED not in env.tenant.mailboxes  # type: ignore[attr-defined]
    assert BLOCKED not in env.responses(await env.read_event(event_id))

    # 6. Booking the same meeting again is caught.
    duplicate = await env.call(
        "gws_calendar_create",
        {
            **meeting,
            "start": "2026-10-09T15:00:00-04:00",
            "end": "2026-10-09T16:00:00-04:00",
        },  # fmt: skip
    )
    assert duplicate["status"] == "deduped", duplicate
    assert duplicate["existing_event_id"] == event_id
    assert env.writes() == before

    # 4d. Finally the kickoff is cancelled, and the attendees are told.
    cancelled = await env.call("gws_calendar_delete", {"event_id": event_id})
    assert "error" not in cancelled, cancelled
    assert await env.read_event(event_id) is None
    assert exchange_calendar.notifications[-1]["kind"] == "cancel"
    assert sorted(exchange_calendar.notifications[-1]["to"]) == [BOB, CAROL]

    # Every write-through row is filed under Microsoft 365, by Graph id.
    (row,) = env.crm_rows("calendar_event")
    assert row[1:4] == ["microsoft365", event_id, None]
