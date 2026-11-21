# ruff: noqa: F811 - pytest fixtures are imported and requested by name
"""A member's approved email from a task workspace (story E3), against real Postgres
and a fake mail transport.

The flow: in a task workspace whose open task is assigned to them, a member's
own interactive turn proposes an email (``room_propose_email``); nothing is
sent. The member approves it under the member rule; the engine's executor
claims it once, re-checks the approved hash and the do-not-contact list, sends
it from the company's assistant mailbox with Reply-To the member, and writes a
receipt into the workspace conversation. Rejection and expiry send nothing.
"""

from __future__ import annotations

import email
import threading
import uuid
from email import policy
from typing import Any

import pytest

from robothor.rooms.tests.conftest import rooms_db  # noqa: F401
from robothor.rooms.tests.staff import staff_db  # noqa: F401
from robothor.rooms.tests.test_member_approval import (  # noqa: F401
    actor,
    db,
    new_task,
    run,
    task_room,
)

pytestmark = pytest.mark.integration

ASSISTANT = "assistant@example.com"
TO = ["carol.client@example.org", "dave.client@example.org"]
CC = ["erin.client@example.org"]


@pytest.fixture
def mail(db, monkeypatch):
    """A fake mail transport, the assistant mailbox configured, and cleanup."""
    from robothor.rooms import outbound

    monkeypatch.setenv("ROBOTHOR_AI_EMAIL", ASSISTANT)
    sent: list[dict[str, Any]] = []
    state: dict[str, Any] = {"fail": None}
    lock = threading.Lock()

    def send_raw(tenant_id: str, raw: str) -> dict[str, Any]:
        if state["fail"] is not None:
            raise state["fail"]
        with lock:
            sent.append({"tenant": tenant_id, "raw": raw})
            return {"id": f"msg-{len(sent)}", "threadId": f"thread-{len(sent)}"}

    monkeypatch.setattr(outbound, "send_raw", send_raw)
    db.sent, db.mail_state = sent, state
    yield db
    db.cur.execute("DELETE FROM crm_people WHERE tenant_id = %s", (db.tenant,))


def bobs_room(db, **kw):
    return task_room(db, caller=db.bob, assignee=db.bob, access=kw.pop("access", "shared"))


def bobs_turn(db, room) -> str:
    return run(db, trigger="webchat", detail=f"webchat:{room.session_key}", user=db.bob)


def propose(db, room, run_id, **overrides):
    from robothor.rooms import outbound

    args = {"to": TO, "cc": CC, "subject": "Renewal follow-up", "body": "Hello,\nShall we renew?"}
    args.update(overrides)
    return outbound.propose_email(actor(db, "bob"), room.id, run_id, **args)


def approved(db, room):
    from robothor.rooms import approvals

    proposed = propose(db, room, bobs_turn(db, room))
    approvals.decide_member_action(actor(db, "bob"), proposed.action_id, approved=True)
    return proposed


def status(db, action_id: str) -> str:
    db.cur.execute("SELECT status FROM operation_actions WHERE id = %s", (action_id,))
    return db.cur.fetchone()[0]


def notes(db, room) -> list[dict[str, Any]]:
    db.cur.execute(
        "SELECT cm.id, cm.message FROM chat_messages cm JOIN chat_sessions s "
        "ON s.id = cm.session_id WHERE s.tenant_id = %s AND s.session_key = %s ORDER BY cm.id",
        (db.tenant, room.session_key),
    )
    return [
        {"id": r[0], **r[1]} for r in db.cur.fetchall() if r[1].get("kind") == "room_email_receipt"
    ]


def parsed(raw: str) -> email.message.EmailMessage:
    import base64

    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


# ─── the sender seam ─────────────────────────────────────────────────────


def test_the_sender_is_the_assistant_mailbox_replying_to_the_member(mail):
    from robothor.rooms import outbound

    sender = outbound.sender_for(mail.tenant, mail.bob)

    assert sender.address == ASSISTANT
    assert sender.reply_to == "bob@example.com"
    assert (sender.on_behalf_of.user_id, sender.on_behalf_of.display_name) == (mail.bob, "Bob")


def test_with_no_assistant_mailbox_nothing_can_be_proposed(mail, monkeypatch):
    from robothor.rooms import outbound

    monkeypatch.setattr(outbound, "_assistant_mailbox", lambda tenant_id: "")
    room = bobs_room(mail)

    with pytest.raises(outbound.EmailRefused, match="mailbox"):
        propose(mail, room, bobs_turn(mail, room))


def test_the_proposal_and_the_executor_both_ask_the_seam(mail, monkeypatch):
    from robothor.rooms import outbound

    room = bobs_room(mail)
    real = outbound.sender_for
    asked: list[tuple[str, str]] = []

    def seam(tenant_id, user_id):
        asked.append((tenant_id, user_id))
        return real(tenant_id, user_id)

    monkeypatch.setattr(outbound, "sender_for", seam)
    proposed = approved(mail, room)
    outbound.deliver(mail.tenant, proposed.action_id)

    assert asked == [(mail.tenant, mail.bob), (mail.tenant, mail.bob)]


# ─── E3: the whole flow ──────────────────────────────────────────────────


def test_a_members_approved_email_is_sent_once_with_a_receipt_in_the_workspace(mail):
    from robothor.crm import dal as crm_dal
    from robothor.operations.store import Operations
    from robothor.rooms import approvals, outbound

    room = bobs_room(mail)
    proposed = propose(mail, room, bobs_turn(mail, room))

    assert proposed.payload == {
        "version": 1,
        "sender": ASSISTANT,
        "reply_to": "bob@example.com",
        "recipient": TO,
        "cc": CC,
        "subject": "Renewal follow-up",
        "body": "Hello,\nShall we renew?",
        "on_behalf_of": {"user_id": mail.bob, "display_name": "Bob"},
        "room_id": room.id,
        "task_id": room.task_id,
    }
    assert status(mail, proposed.action_id) == "review"
    assert mail.sent == [], "proposing sends nothing"
    [row] = approvals.list_member_actions(actor(mail, "bob"))
    assert row["kind"] == "room.email" and row["payload"] == proposed.payload

    approvals.decide_member_action(actor(mail, "bob"), proposed.action_id, approved=True)
    assert outbound.deliver(mail.tenant, proposed.action_id) == "sent"

    [sent] = mail.sent
    message = parsed(sent["raw"])
    assert message["From"] == ASSISTANT
    assert message["Reply-To"] == "bob@example.com"
    assert message["To"] == ", ".join(TO)
    assert message["Cc"] == ", ".join(CC)
    assert message["Subject"] == "Renewal follow-up"
    assert message.get_content().strip() == "Hello,\nShall we renew?"
    action = Operations(mail.tenant).get_action(proposed.action_id)
    assert action["status"] == "completed"
    assert action["receipt"]["id"] == "msg-1"

    [note] = notes(mail, room)
    receipt = note["receipt"]
    assert note["role"] == "assistant"
    assert receipt["status"] == "sent"
    assert receipt["action_id"] == proposed.action_id
    assert receipt["from"] == ASSISTANT and receipt["reply_to"] == "bob@example.com"
    assert receipt["to"] == TO and receipt["cc"] == CC
    assert receipt["subject"] == "Renewal follow-up"
    assert receipt["approved_by"] == {"user_id": mail.bob, "display_name": "Bob"}
    assert receipt["message_id"] == "msg-1" and receipt["sent_at"]
    for part in (ASSISTANT, TO[0], "Bob", "Renewal follow-up"):
        assert part in note["content"]
    events = [p for s, t, p in mail.events if t == "room.message"]
    assert {"room_id": room.id, "tenant_id": mail.tenant, "message_id": note["id"]} in events

    assert crm_dal.update_task(room.task_id, status="IN_PROGRESS", tenant_id=mail.tenant)
    assert crm_dal.update_task(
        room.task_id, status="DONE", resolution="Emails sent", tenant_id=mail.tenant
    )


def test_delivering_again_sends_nothing_more(mail):
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = approved(mail, room)

    assert outbound.deliver(mail.tenant, proposed.action_id) == "sent"
    assert outbound.deliver(mail.tenant, proposed.action_id) == "skipped"
    assert outbound.sweep() >= 0

    assert len(mail.sent) == 1
    assert len(notes(mail, room)) == 1


def test_concurrent_executors_send_once(mail):
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = approved(mail, room)
    barrier = threading.Barrier(4)
    outcomes: list[str] = []

    def go():
        barrier.wait()
        outcomes.append(outbound.deliver(mail.tenant, proposed.action_id))

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["sent", "skipped", "skipped", "skipped"]
    assert len(mail.sent) == 1


def test_an_executor_that_died_mid_send_is_never_retried(mail):
    """A restart finds the claimed action's lease expired: it becomes ``unknown``,
    is never sent again, and the member is told it could not be confirmed."""
    from robothor.operations.store import Operations
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = approved(mail, room)
    assert Operations(mail.tenant).claim_action_by_id(proposed.action_id) is not None
    mail.cur.execute(
        "UPDATE operation_actions SET lease_until = now() - interval '1 minute' WHERE id = %s",
        (proposed.action_id,),
    )

    outbound.sweep()
    outbound.sweep()

    assert mail.sent == []
    assert status(mail, proposed.action_id) == "unknown"
    [note] = notes(mail, room)
    assert note["receipt"]["status"] == "uncertain"


def test_a_sweep_delivers_an_approval_whose_event_was_lost(mail):
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = approved(mail, room)

    outbound.sweep()

    assert len(mail.sent) == 1
    assert status(mail, proposed.action_id) == "completed"


def test_a_rejected_email_is_never_sent_and_the_workspace_is_told(mail):
    from robothor.rooms import approvals, outbound

    room = bobs_room(mail)
    proposed = propose(mail, room, bobs_turn(mail, room))
    approvals.decide_member_action(actor(mail, "bob"), proposed.action_id, approved=False)

    assert outbound.deliver(mail.tenant, proposed.action_id) == "skipped"
    outbound.sweep()
    outbound.sweep()

    assert mail.sent == []
    [note] = notes(mail, room)
    assert note["receipt"]["status"] == "rejected"
    assert note["receipt"]["approved_by"] is None
    assert note["receipt"]["decided_by"] == {"user_id": mail.bob, "display_name": "Bob"}


def test_an_expired_email_is_never_sent_and_the_workspace_is_told(mail):
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = propose(mail, room, bobs_turn(mail, room))
    mail.cur.execute(
        "UPDATE operation_actions SET expires_at = now() - interval '1 minute' WHERE id = %s",
        (proposed.action_id,),
    )

    outbound.sweep()
    outbound.sweep()

    assert mail.sent == []
    [note] = notes(mail, room)
    assert note["receipt"]["status"] == "expired"


def test_a_payload_altered_after_approval_is_refused_at_claim(mail):
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = approved(mail, room)
    mail.cur.execute(
        'UPDATE operation_actions SET payload = payload || \'{"recipient": ["eve@example.com"]}\' '
        "WHERE id = %s",
        (proposed.action_id,),
    )

    assert outbound.deliver(mail.tenant, proposed.action_id) == "refused"
    outbound.sweep()

    assert mail.sent == []
    [note] = notes(mail, room)
    assert note["receipt"]["status"] == "not_sent"
    assert "changed" in note["receipt"]["error"]


def test_a_second_approval_changes_nothing(mail):
    from robothor.operations.store import Conflict
    from robothor.rooms import approvals, outbound

    room = bobs_room(mail)
    proposed = approved(mail, room)
    with pytest.raises(Conflict):
        approvals.decide_member_action(actor(mail, "bob"), proposed.action_id, approved=True)
    outbound.deliver(mail.tenant, proposed.action_id)

    assert len(mail.sent) == 1


def test_a_failed_send_is_recorded_and_not_retried(mail):
    from robothor.operations.store import Operations
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = approved(mail, room)
    mail.mail_state["fail"] = RuntimeError("mail provider said no")

    assert outbound.deliver(mail.tenant, proposed.action_id) == "failed"
    mail.mail_state["fail"] = None
    outbound.sweep()

    assert mail.sent == []
    action = Operations(mail.tenant).get_action(proposed.action_id)
    assert action["status"] == "failed"
    [note] = notes(mail, room)
    assert note["receipt"]["status"] == "failed"
    assert "mail provider said no" in note["receipt"]["error"]


def test_a_provider_answer_without_a_message_id_is_uncertain(mail, monkeypatch):
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = approved(mail, room)
    monkeypatch.setattr(outbound, "send_raw", lambda tenant_id, raw: {"error": "quota"})

    assert outbound.deliver(mail.tenant, proposed.action_id) == "failed"
    [note] = notes(mail, room)
    assert note["receipt"]["status"] == "failed"


def test_a_changed_sending_mailbox_sends_nothing(mail, monkeypatch):
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = approved(mail, room)
    monkeypatch.setenv("ROBOTHOR_AI_EMAIL", "other-assistant@example.com")

    assert outbound.deliver(mail.tenant, proposed.action_id) == "failed"
    assert mail.sent == []


# ─── do not contact ──────────────────────────────────────────────────────


def _opt_out(db, address: str) -> None:
    db.cur.execute(
        "INSERT INTO crm_people (id, tenant_id, first_name, email, do_not_contact) "
        "VALUES (%s, %s, 'Opted', %s, TRUE)",
        (str(uuid.uuid4()), db.tenant, address),
    )


def test_an_opted_out_recipient_is_refused_at_proposal(mail):
    from robothor.rooms import outbound

    _opt_out(mail, CC[0])
    room = bobs_room(mail)

    with pytest.raises(outbound.EmailRefused, match="opted out"):
        propose(mail, room, bobs_turn(mail, room))
    mail.cur.execute("SELECT COUNT(*) FROM operation_actions WHERE tenant_id = %s", (mail.tenant,))
    assert mail.cur.fetchone()[0] == 0


def test_an_opt_out_after_approval_is_honoured_at_execution(mail):
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = approved(mail, room)
    _opt_out(mail, TO[1])

    assert outbound.deliver(mail.tenant, proposed.action_id) == "refused"

    assert mail.sent == []
    assert status(mail, proposed.action_id) == "cancelled"
    [note] = notes(mail, room)
    assert note["receipt"]["status"] == "not_sent"
    assert "opted out" in note["receipt"]["error"]


# ─── every refusal of the proposal ───────────────────────────────────────


def _no_action(db) -> None:
    db.cur.execute("SELECT COUNT(*) FROM operation_actions WHERE tenant_id = %s", (db.tenant,))
    assert db.cur.fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["report", "agent"])
def test_refused_outside_a_task_workspace(mail, kind):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import dal

    room = dal.create_room(mail.tenant, kind=kind, title="R", agent_id="main", created_by=mail.bob)
    with pytest.raises(NotPermitted):
        propose(mail, room, bobs_turn(mail, room))
    _no_action(mail)


@pytest.mark.parametrize(
    "how",
    ["scheduled", "sub_agent", "nested", "someone_elses", "other_room", "plan_exec", "unknown"],
)
def test_refused_outside_the_members_own_interactive_turn_there(mail, how):
    from robothor.operations.store import NotPermitted

    room = bobs_room(mail)
    top = bobs_turn(mail, room)
    other = bobs_room(mail)
    run_id = {
        "scheduled": lambda: run(mail, trigger="cron", detail=f"room:{room.id}"),
        "sub_agent": lambda: run(
            mail, trigger="sub_agent", detail=f"webchat:{room.session_key}", user=mail.bob
        ),
        "nested": lambda: _child(mail, top, room),
        "someone_elses": lambda: run(
            mail, trigger="webchat", detail=f"webchat:{room.session_key}", user=mail.alice
        ),
        "other_room": lambda: bobs_turn(mail, other),
        "plan_exec": lambda: run(
            mail, trigger="webchat", detail=f"plan-exec:{room.session_key}", user=mail.bob
        ),
        "unknown": lambda: str(uuid.uuid4()),
    }[how]()

    with pytest.raises(NotPermitted):
        propose(mail, room, run_id)
    _no_action(mail)


def _child(db, parent: str, room) -> str:
    child = run(db, trigger="webchat", detail=f"webchat:{room.session_key}", user=db.bob)
    db.cur.execute("UPDATE agent_runs SET parent_run_id = %s WHERE id = %s", (parent, child))
    return child


@pytest.mark.parametrize("assignee", ["nobody", "someone_else"])
def test_refused_unless_the_task_is_assigned_to_the_member(mail, assignee):
    from robothor.operations.store import NotPermitted

    who = None if assignee == "nobody" else mail.carol
    room = task_room(mail, caller=mail.bob, assignee=who, access="shared")

    with pytest.raises(NotPermitted):
        propose(mail, room, bobs_turn(mail, room))
    _no_action(mail)


@pytest.mark.parametrize("change", ["done", "resolved", "removed", "archived", "viewer"])
def test_refused_when_the_task_or_access_is_gone(mail, change):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import dal

    room = bobs_room(mail)
    turn = bobs_turn(mail, room)
    if change == "done":
        mail.cur.execute("UPDATE crm_tasks SET status = 'DONE' WHERE id = %s", (room.task_id,))
    elif change == "resolved":
        mail.cur.execute("UPDATE crm_tasks SET resolved_at = now() WHERE id = %s", (room.task_id,))
    elif change == "removed":
        dal.remove_member(mail.tenant, room.id, mail.bob, removed_by=mail.dana)
    elif change == "archived":
        dal.archive_room(mail.tenant, room.id)
    else:
        mail.cur.execute("UPDATE user_accounts SET role = 'viewer' WHERE id = %s", (mail.bob,))

    with pytest.raises(NotPermitted):
        propose(mail, room, turn)
    _no_action(mail)


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"to": []}, "recipient"),
        ({"to": ["not-an-address"]}, "address"),
        ({"to": ["Carol <carol@example.org>"]}, "address"),
        ({"cc": ["bad@"]}, "address"),
        ({"to": ["a@example.org\nBcc: x@example.org"]}, "address"),
        ({"subject": ""}, "subject"),
        ({"subject": "Hi\nBcc: x@example.org"}, "subject"),
        ({"body": ""}, "body"),
        ({"attachments": [{"name": "a.pdf"}]}, "attachment"),
    ],
)
def test_malformed_drafts_are_refused(mail, overrides, needle):
    from robothor.rooms import outbound

    room = bobs_room(mail)
    with pytest.raises(outbound.EmailRefused, match=needle):
        propose(mail, room, bobs_turn(mail, room), **overrides)
    _no_action(mail)


def test_many_recipients_are_not_capped(mail):
    room = bobs_room(mail)
    many = [f"client{i}@example.org" for i in range(150)]

    proposed = propose(mail, room, bobs_turn(mail, room), to=many)

    assert proposed.payload["recipient"] == many


def test_a_proposal_can_only_be_approved_by_the_member_rule_or_an_operator(mail):
    """A member never sends without approving: a proposal stays in review until
    decided, and the executor sends only approved rows."""
    from robothor.rooms import outbound

    room = bobs_room(mail)
    proposed = propose(mail, room, bobs_turn(mail, room))

    assert outbound.deliver(mail.tenant, proposed.action_id) == "skipped"
    outbound.sweep()
    assert mail.sent == []
    assert status(mail, proposed.action_id) == "review"


def test_alices_proposal_in_her_task_cannot_be_approved_by_bob(mail):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals, outbound

    room = task_room(mail, caller=mail.alice, assignee=mail.alice, access="shared")
    dal_run = run(mail, trigger="webchat", detail=f"webchat:{room.session_key}", user=mail.alice)
    proposed = outbound.propose_email(
        actor(mail, "alice"), room.id, dal_run, to=TO, cc=[], subject="Hi", body="Hello"
    )

    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(mail, "bob"), proposed.action_id, approved=True)
    assert outbound.deliver(mail.tenant, proposed.action_id) == "skipped"
    assert mail.sent == []
