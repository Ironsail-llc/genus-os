"""Member approval: the one place an operator-only gate widens (stories E3, E4).

A member may list and decide an action ONLY when all of these hold:

- the action records the member as the person it was proposed for;
- the proposing run is an interactive webchat run of that member, on the
  workspace's own session;
- the member can post in that workspace now (shared or assigned, not removed,
  the workspace not archived, the member not a viewer);
- the workspace is a task workspace whose task's human assignee is the member.

Everything else stays with the tenant's operators. E4 is proved as a full
matrix: caller role x proposer x task assignee x workspace access, allowed in
exactly one cell family.
"""

from __future__ import annotations

import itertools
import uuid
from typing import Any

import pytest

from robothor.rooms.tests.conftest import committed_connection, rooms_db  # noqa: F401
from robothor.rooms.tests.staff import staff_db  # noqa: F401

pytestmark = pytest.mark.integration

ROLES = ("member", "viewer", "other_tenant", "owner", "admin")
PROPOSERS = ("mine", "other_user", "scheduled", "non_room")
ASSIGNEES = ("caller", "someone_else", "nobody")
ACCESS = ("assigned", "shared", "removed", "archived")
KINDS = ("allowed", "other")

#: role -> the persona who calls in that role.
CALLER = {
    "member": "alice",
    "viewer": "vera",
    "other_tenant": "olga",
    "owner": "owen",
    "admin": "dana",
}


@pytest.fixture
def db(staff_db, monkeypatch):  # noqa: F811
    from robothor.crm import dal as crm_dal
    from robothor.engine import chat_store
    from robothor.events import bus

    monkeypatch.setattr(chat_store, "get_connection", committed_connection)
    monkeypatch.setattr(crm_dal, "get_connection", committed_connection)
    events: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        bus, "publish", lambda stream, kind, payload, **kw: events.append((stream, kind, payload))
    )
    staff_db.events = events
    try:
        yield staff_db
    finally:
        tenants = [staff_db.tenant, staff_db.other]
        cur = staff_db.cur
        for table in ("operation_audit", "operation_actions", "agent_runs", "crm_task_history"):
            cur.execute(f"DELETE FROM {table} WHERE tenant_id = ANY(%s)", (tenants,))  # noqa: S608
        cur.execute("DELETE FROM room_member_removals WHERE tenant_id = ANY(%s)", (tenants,))


def actor(db, name: str):
    from robothor.rooms.access import Actor
    from robothor.rooms.tests.staff import PEOPLE

    if name == "olga":
        return Actor(user_id=db.olga, role="member", tenant_id=db.other)
    return Actor(user_id=getattr(db, name), role=PEOPLE[name], tenant_id=db.tenant)


def new_task(db, assignee: str | None) -> str:
    task_id = str(uuid.uuid4())
    db.cur.execute(
        "INSERT INTO crm_tasks (id, tenant_id, title, status, assignee_id) "
        "VALUES (%s, %s, 'Send the follow-ups', 'TODO', %s)",
        (task_id, db.tenant, assignee),
    )
    return task_id


def task_room(db, *, caller: str, assignee: str | None, access: str):
    """A task workspace opened by Dana, with ``caller`` given ``access``."""
    from robothor.rooms import dal, tasks

    task_id = new_task(db, assignee)
    room = tasks.open_task_room(actor(db, "dana"), task_id)
    if caller != "dana":
        dal.add_member(db.tenant, room.id, caller, added_by=db.dana, assigned=access != "shared")
        if access == "removed":
            dal.remove_member(db.tenant, room.id, caller, removed_by=db.dana)
    if access == "archived":
        dal.archive_room(db.tenant, room.id)
    return dal.get_room(db.tenant, room.id)


def run(db, *, trigger: str, detail: str, user: str = "") -> str:
    run_id = str(uuid.uuid4())
    db.cur.execute(
        "INSERT INTO agent_runs (id, tenant_id, agent_id, trigger_type, trigger_detail, "
        "user_id, status) VALUES (%s, %s, 'main', %s, %s, %s, 'completed')",
        (run_id, db.tenant, trigger, detail, user),
    )
    return run_id


#: The one kind a member may decide (``approvals.MEMBER_DECIDABLE_KINDS``).
KIND = "room.email"
#: Kinds nobody put on the member allow-list: a plugin's (proposed with or
#: without ``proposed_by``), a core kind of the future, the sales pipeline's.
OTHER_KINDS = ("acme-pay.transfer", "email.follow_up", "sales.email")


def propose(
    db,
    *,
    run_id: str | None,
    for_user: str | None,
    room_id: str | None,
    kind: str = KIND,
    plugin: str | None = None,
    **payload,
):
    from robothor.operations.proposer import Proposer
    from robothor.operations.store import Operations

    body = {
        "recipient": "bob@example.com",
        "subject": "Following up",
        "body": "Hello",
        **payload,
    }
    return Operations(db.tenant).propose(
        kind,
        f"draft-{uuid.uuid4()}",
        body,
        proposed_by=plugin,
        proposer=Proposer(run_id=run_id, user_id=for_user, room_id=room_id),
    )


def action_for(db, *, caller: str, proposer: str, room, kind: str = KIND) -> str:
    """An action proposed in ``room`` the way ``proposer`` names."""
    if proposer == "mine":
        r = run(db, trigger="webchat", detail=f"webchat:{room.session_key}", user=caller)
        return propose(db, run_id=r, for_user=caller, room_id=room.id, kind=kind)
    if proposer == "other_user":
        r = run(db, trigger="webchat", detail=f"webchat:{room.session_key}", user=db.bob)
        return propose(db, run_id=r, for_user=db.bob, room_id=room.id)
    if proposer == "scheduled":
        r = run(db, trigger="cron", detail=f"room:{room.id}")
        return propose(db, run_id=r, for_user=None, room_id=room.id)
    r = run(db, trigger="webchat", detail=f"webchat:agent:main:web:{caller}", user=caller)
    return propose(db, run_id=r, for_user=caller, room_id=None)


def status(db, action_id: str) -> tuple[str, str | None]:
    db.cur.execute("SELECT status, decided_by FROM operation_actions WHERE id = %s", (action_id,))
    return db.cur.fetchone()


# ─── E4: the full matrix ─────────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
def test_member_approval_is_allowed_in_exactly_one_cell_family(db, role):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals

    name = CALLER[role]
    caller_id = db.olga if name == "olga" else getattr(db, name)
    cells: dict[tuple[str, ...], str] = {}
    for proposer, assignee, access in itertools.product(PROPOSERS, ASSIGNEES, ACCESS):
        assigned_to = {"caller": caller_id, "someone_else": db.carol, "nobody": None}[assignee]
        room = task_room(db, caller=caller_id, assignee=assigned_to, access=access)
        cells[(proposer, assignee, access, "allowed")] = action_for(
            db, caller=caller_id, proposer=proposer, room=room
        )
        # One kind off the allow-list per cell; every other kind is refused in
        # the eligible cell by test_only_an_allow_listed_kind_is_member_decidable.
        cells[(proposer, assignee, access, "other")] = action_for(
            db, caller=caller_id, proposer=proposer, room=room, kind=OTHER_KINDS[0]
        )

    def allowed(cell: tuple[str, ...]) -> bool:
        proposer, assignee, access, kind = cell
        return (
            role == "member"
            and kind == "allowed"
            and proposer == "mine"
            and assignee == "caller"
            and access in ("assigned", "shared")
        )

    me = actor(db, name)
    listed = {row["id"] for row in approvals.list_member_actions(me)}
    assert listed == {action for cell, action in cells.items() if allowed(cell)}

    for cell, action in cells.items():
        if allowed(cell):
            assert approvals.decide_member_action(me, action, approved=True) is True, cell
            assert status(db, action) == ("approved", f"member:{caller_id}"), cell
        else:
            with pytest.raises(NotPermitted):
                approvals.decide_member_action(me, action, approved=True)
            assert status(db, action) == ("review", None), cell
    if role == "member":
        assert sum(allowed(c) for c in cells) == 2


# ─── the columns are checked against the run, not trusted ────────────────


@pytest.fixture
def bobs_room(db):
    """A task workspace Bob shares, whose task is assigned to Bob."""
    return task_room(db, caller=db.bob, assignee=db.bob, access="shared")


def test_a_forged_proposer_column_is_refused(db, bobs_room):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals

    alices_run = run(
        db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.alice
    )
    action = propose(db, run_id=alices_run, for_user=db.bob, room_id=bobs_room.id)

    assert approvals.list_member_actions(actor(db, "bob")) == []
    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)


def test_a_run_in_another_workspace_is_refused(db, bobs_room):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals

    elsewhere = task_room(db, caller=db.bob, assignee=db.bob, access="shared")
    r = run(db, trigger="webchat", detail=f"webchat:{elsewhere.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)

    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)


@pytest.mark.parametrize("detail", ["plan-exec", "plan", "resume"])
def test_only_an_interactive_webchat_turn_counts(db, bobs_room, detail):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals

    r = run(db, trigger="webchat", detail=f"{detail}:{bobs_room.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)

    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)


def test_an_action_with_no_recorded_run_is_refused(db, bobs_room):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals

    action = propose(db, run_id=None, for_user=db.bob, room_id=bobs_room.id)

    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)


def test_a_report_workspace_action_stays_with_operators(db):
    """A report workspace has no task of its own: nothing links the action to a task."""
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals, dal

    room = dal.create_room(
        db.tenant, kind="report", title="Weekly", agent_id="main", created_by=db.bob
    )
    r = run(db, trigger="webchat", detail=f"webchat:{room.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=room.id)

    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)


@pytest.mark.parametrize("action_id", [str(uuid.uuid4()), "not-a-uuid"])
def test_an_unknown_action_is_refused(db, action_id):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals

    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action_id, approved=True)


def test_a_service_identity_is_refused(db, bobs_room):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals
    from robothor.rooms.access import Actor

    r = run(db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)
    service = Actor(user_id=db.bob, role="member", tenant_id=db.tenant, is_service=True)

    assert approvals.list_member_actions(service) == []
    with pytest.raises(NotPermitted):
        approvals.decide_member_action(service, action, approved=True)


def _mine(db, room, **kw):
    r = run(db, trigger="webchat", detail=f"webchat:{room.session_key}", user=db.bob)
    return propose(db, run_id=r, for_user=db.bob, room_id=room.id, **kw)


@pytest.mark.parametrize(
    "overrides",
    [
        {"plugin": "sales"},
        {"kind": "sales.email"},
        {"kind": "sales.follow_up"},
        {"kind": "acme-pay.transfer"},
        {"kind": "acme-pay.transfer", "plugin": "acme-pay"},
        {"kind": "room.email.v2"},
        {"kind": "ROOM.EMAIL"},
    ],
    ids=[
        "plugin-proposed",
        "sales-email",
        "sales-other",
        "plugin-kind-without-proposer",
        "plugin-kind-with-proposer",
        "lookalike-kind",
        "case-variant",
    ],
)
def test_only_an_allow_listed_kind_is_member_decidable(db, bobs_room, overrides):
    """A member decides only kinds on the allow-list; a plugin's draft (with or
    without ``proposed_by``), a sales draft or any other kind stays with
    operators, in an otherwise eligible cell."""
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals

    action = _mine(db, bobs_room, **overrides)

    assert approvals.list_member_actions(actor(db, "bob")) == []
    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE user_accounts SET role = 'viewer' WHERE id = %(bob)s",
        "UPDATE user_accounts SET status = 'disabled' WHERE id = %(bob)s",
        "DELETE FROM user_accounts WHERE id = %(bob)s",
    ],
    ids=["demoted-to-viewer", "deactivated", "account-gone"],
)
def test_the_account_is_read_from_the_database_not_the_token(db, bobs_room, change):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals

    action = _mine(db, bobs_room)
    db.cur.execute(change, {"bob": db.bob})

    assert approvals.list_member_actions(actor(db, "bob")) == []
    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE crm_tasks SET status = 'DONE' WHERE id = %(task)s",
        "UPDATE crm_tasks SET resolved_at = now(), resolution = 'done' WHERE id = %(task)s",
        "UPDATE crm_tasks SET deleted_at = now() WHERE id = %(task)s",
    ],
    ids=["done", "resolved", "deleted"],
)
def test_a_finished_task_grants_nothing(db, bobs_room, change):
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals

    action = _mine(db, bobs_room)
    db.cur.execute(change, {"task": bobs_room.task_id})

    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)


def test_opening_a_task_never_makes_the_opener_able_to_approve(db):
    """Open an unassigned task as a workspace, try to take it from inside, then
    approve an action from your own turn there: refused at every step."""
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals, tasks
    from robothor.rooms.errors import RoomForbidden

    task_id = new_task(db, None)
    room = tasks.open_task_room(actor(db, "bob"), task_id)
    with pytest.raises(RoomForbidden):
        tasks.accept_task(actor(db, "bob"), room.id, task_id)
    action = _mine(db, room)

    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)
    db.cur.execute("SELECT assignee_id FROM crm_tasks WHERE id = %s", (task_id,))
    assert db.cur.fetchone()[0] is None


def test_an_accepted_suggestion_opened_as_a_workspace_is_the_members_to_approve(db):
    """E3 end to end: Bob accepts a suggested task, opens it, his own turn there
    proposes, and he may approve it."""
    from robothor.rooms import approvals, dal, tasks

    report = dal.create_room(
        db.tenant, kind="report", title="Weekly", agent_id="main", created_by=db.alice
    )
    dal.add_member(db.tenant, report.id, db.bob, added_by=db.alice)
    edition, _ = dal.create_edition(db.tenant, report.id)
    edition = dal.publish_edition(
        db.tenant,
        edition.id,
        title="W47",
        summary="",
        blocks=[],
        suggestions=[{"key": "acme", "title": "Email Acme", "detail": "", "action_hint": ""}],
    )
    tasks.on_edition_published(report, edition)
    [view] = tasks.list_tasks(actor(db, "bob"), report.id)
    tasks.accept_task(actor(db, "bob"), report.id, view.task_id)
    room = tasks.open_task_room(actor(db, "bob"), view.task_id)
    action = _mine(db, room)

    assert [r["id"] for r in approvals.list_member_actions(actor(db, "bob"))] == [action]
    assert approvals.decide_member_action(actor(db, "bob"), action, approved=True) is True


# ─── E4.5: Bob lists only his own ────────────────────────────────────────


def test_the_member_list_holds_only_the_callers_pending_eligible_rows(db, bobs_room):
    from robothor.rooms import approvals

    r = run(db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.bob)
    pending = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)
    decided = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id, subject="Second")
    approvals.decide_member_action(actor(db, "bob"), decided, approved=False)
    expired = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id, subject="Third")
    db.cur.execute("UPDATE operation_actions SET expires_at = now() - interval '1 minute' "
                   "WHERE id = %s", (expired,))  # fmt: skip
    alices_room = task_room(db, caller=db.alice, assignee=db.alice, access="shared")
    ra = run(db, trigger="webchat", detail=f"webchat:{alices_room.session_key}", user=db.alice)
    propose(db, run_id=ra, for_user=db.alice, room_id=alices_room.id)

    rows = approvals.list_member_actions(actor(db, "bob"))

    assert [row["id"] for row in rows] == [pending]
    [row] = rows
    assert row["room_id"] == bobs_room.id
    assert row["task_id"] == bobs_room.task_id
    assert row["kind"] == KIND
    assert row["payload"]["recipient"] == "bob@example.com"


def test_the_member_list_has_no_cap(db, bobs_room):
    """Workspaces cap nothing: every one of a member's pending actions is listed."""
    from robothor.rooms import approvals

    r = run(db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.bob)
    made = {
        propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id, subject=f"n{i}")
        for i in range(120)
    }

    assert {row["id"] for row in approvals.list_member_actions(actor(db, "bob"))} == made


# ─── E3: approve, reject, expire; the effect is exactly what was approved ─


def test_an_approved_member_action_executes_once_exactly_as_approved(db, bobs_room):
    from robothor.operations.store import Operations
    from robothor.rooms import approvals

    r = run(db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)
    shown = {row["id"]: row["payload"] for row in approvals.list_member_actions(actor(db, "bob"))}

    assert approvals.decide_member_action(actor(db, "bob"), action, approved=True) is True

    ops = Operations(db.tenant)
    claimed = ops.claim_action_by_id(action)
    assert claimed is not None and claimed["payload"] == shown[action]
    assert ops.claim_action_by_id(action) is None
    db.cur.execute(
        "SELECT event, actor, detail FROM operation_audit WHERE tenant_id = %s AND entity_id = %s "
        "ORDER BY created_at",
        (db.tenant, action),
    )
    audit = db.cur.fetchall()
    assert ("action.approved", f"member:{db.bob}") in [(e, a) for e, a, _ in audit]
    [(_, _, detail)] = [row for row in audit if row[0] == "action.approved"]
    assert detail["room_id"] == bobs_room.id and detail["task_id"] == bobs_room.task_id


def test_a_payload_changed_after_member_approval_never_executes(db, bobs_room):
    from robothor.operations.store import Conflict, Operations
    from robothor.rooms import approvals

    r = run(db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)
    approvals.decide_member_action(actor(db, "bob"), action, approved=True)
    db.cur.execute(
        'UPDATE operation_actions SET payload = payload || \'{"recipient": "eve@example.com"}\' '
        "WHERE id = %s",
        (action,),
    )

    with pytest.raises(Conflict):
        Operations(db.tenant).claim_action_by_id(action)


def test_a_rejected_member_action_never_executes(db, bobs_room):
    from robothor.operations.store import Operations
    from robothor.rooms import approvals

    r = run(db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)

    assert approvals.decide_member_action(actor(db, "bob"), action, approved=False) is True

    assert status(db, action) == ("rejected", f"member:{db.bob}")
    assert Operations(db.tenant).claim_action_by_id(action) is None
    db.cur.execute("SELECT status FROM crm_tasks WHERE id = %s", (bobs_room.task_id,))
    assert db.cur.fetchone()[0] == "TODO"


def test_an_expired_member_action_settles_nothing(db, bobs_room):
    from robothor.operations.store import Conflict
    from robothor.rooms import approvals

    r = run(db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)
    db.cur.execute(
        "UPDATE operation_actions SET expires_at = now() - interval '1 minute' WHERE id = %s",
        (action,),
    )

    with pytest.raises(Conflict) as refused:
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)
    assert type(refused.value).__name__ == "Conflict"
    assert status(db, action) == ("review", None)


def test_a_second_member_decision_settles_nothing(db, bobs_room):
    from robothor.operations.store import Conflict, NotPermitted
    from robothor.rooms import approvals

    r = run(db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)
    approvals.decide_member_action(actor(db, "bob"), action, approved=False)

    with pytest.raises(Conflict) as again:
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)
    assert not isinstance(again.value, NotPermitted)
    assert status(db, action) == ("rejected", f"member:{db.bob}")


# ─── the ledger itself refuses a member verdict it cannot re-check ───────


def test_the_ledger_refuses_a_member_verdict_without_a_check(db, bobs_room):
    from robothor.operations.store import NotPermitted, Operations

    r = run(db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)

    with pytest.raises(NotPermitted):
        Operations(db.tenant).decide(action, True, f"member:{db.bob}")
    with pytest.raises(NotPermitted):
        Operations(db.tenant).decide(
            action, True, f"member:{db.bob}", member_check=lambda cur, row: False
        )
    with pytest.raises(NotPermitted):
        Operations(db.tenant).decide(
            action, True, f"member:{db.alice}", member_check=lambda cur, row: True
        )
    assert status(db, action) == ("review", None)


def test_a_check_that_fails_inside_the_decision_settles_nothing(db, bobs_room, monkeypatch):
    """Access removed between the first check and the locked decision."""
    from robothor.operations.store import NotPermitted
    from robothor.rooms import approvals, dal

    r = run(db, trigger="webchat", detail=f"webchat:{bobs_room.session_key}", user=db.bob)
    action = propose(db, run_id=r, for_user=db.bob, room_id=bobs_room.id)
    real = approvals._eligible_in

    calls = {"n": 0}

    def flaky(cur, me, action_id, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            dal.remove_member(db.tenant, bobs_room.id, db.bob, removed_by=db.dana)
        return real(cur, me, action_id, **kw)

    monkeypatch.setattr(approvals, "_eligible_in", flaky)

    with pytest.raises(NotPermitted):
        approvals.decide_member_action(actor(db, "bob"), action, approved=True)
    assert status(db, action) == ("review", None)


def test_the_allow_list_is_exactly_the_kinds_this_feature_creates():
    from robothor.rooms import approvals, outbound

    assert frozenset({outbound.KIND}) == approvals.MEMBER_DECIDABLE_KINDS
