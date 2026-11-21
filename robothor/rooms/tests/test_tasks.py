"""Tasks in workspaces: suggestions become tasks, accept, dismiss, open a task as a
workspace (stories E1, E2, E5, E6), against real Postgres.

Personas: Alice and Bob are staff (members), Vera a viewer, Dana an admin,
Carol a member with no access to the workspace, Olga a user of another company.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from robothor.rooms.tests.conftest import committed_connection, rooms_db  # noqa: F401
from robothor.rooms.tests.staff import staff_db  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def db(staff_db, monkeypatch):  # noqa: F811
    """``staff_db`` plus a captured event bus and the chat store on committed connections."""
    from robothor.crm import dal as crm_dal
    from robothor.engine import chat_store
    from robothor.events import bus

    monkeypatch.setattr(chat_store, "get_connection", committed_connection)
    monkeypatch.setattr(crm_dal, "get_connection", committed_connection)
    events: list[tuple[str, str, dict[str, Any]]] = []

    def publish(stream, event_type, payload, **kw):
        events.append((stream, event_type, dict(payload)))
        return "1-0"

    monkeypatch.setattr(bus, "publish", publish)
    staff_db.events = events
    try:
        yield staff_db
    finally:
        tenants = [staff_db.tenant, staff_db.other]
        cur = staff_db.cur
        cur.execute("DELETE FROM crm_task_history WHERE tenant_id = ANY(%s)", (tenants,))
        cur.execute("DELETE FROM room_member_removals WHERE tenant_id = ANY(%s)", (tenants,))


def actor(db, name: str, *, tenant: str | None = None):
    from robothor.rooms.access import Actor
    from robothor.rooms.tests.staff import PEOPLE

    role = PEOPLE.get(name, "member")
    return Actor(user_id=getattr(db, name), role=role, tenant_id=tenant or db.tenant)


def olga(db):
    from robothor.rooms.access import Actor

    return Actor(user_id=db.olga, role="member", tenant_id=db.other)


def report_room(db, *, shared=("bob", "vera")):
    from robothor.rooms import dal

    room = dal.create_room(
        db.tenant, kind="report", title="Weekly sales", agent_id="main", created_by=db.alice
    )
    for name in shared:
        dal.add_member(db.tenant, room.id, getattr(db, name), added_by=db.alice)
    return room


def publish(db, room, suggestions, *, preview=False):
    from robothor.rooms import dal

    edition, _ = dal.create_edition(
        db.tenant, room.id, preview=preview, requested_by=db.alice if preview else None
    )
    return dal.publish_edition(
        db.tenant,
        edition.id,
        title="Week 47",
        summary="",
        blocks=[{"component": "render_markdown", "props": {"content": "hi"}}],
        suggestions=suggestions,
    )


def suggestion(key: str, title: str | None = None) -> dict[str, str]:
    return {
        "key": key,
        "title": title or f"Follow up {key}",
        "detail": "Ask about the renewal",
        "action_hint": "email",
    }


def crm_task(db, *, tenant=None, title="Renew the contract", **cols: Any) -> str:
    task_id = str(uuid.uuid4())
    cols = {
        "status": "TODO",
        "tags": [],
        "objective": "Get the renewal signed",
        **cols,
    }
    names = ", ".join(["id", "tenant_id", "title", *cols])
    marks = ", ".join(["%s"] * (3 + len(cols)))
    db.cur.execute(
        f"INSERT INTO crm_tasks ({names}) VALUES ({marks})",  # noqa: S608
        (task_id, tenant or db.tenant, title, *cols.values()),
    )
    return task_id


def task_row(db, task_id: str) -> dict[str, Any]:
    db.cur.execute(
        "SELECT title, status, tags, assignee_id, assigned_to_agent, requires_human, "
        "resolved_at, resolution, next_action, updated_at FROM crm_tasks WHERE id = %s",
        (task_id,),
    )
    names = [d[0] for d in db.cur.description]
    return dict(zip(names, db.cur.fetchone(), strict=True))


def room_task_events(db) -> list[dict[str, Any]]:
    return [p for s, t, p in db.events if s == "agent" and t == "room.task"]


# ─── E1: suggestions become suggested tasks ──────────────────────────────


def test_each_suggestion_of_a_published_edition_becomes_a_suggested_task(db):
    from robothor.rooms import tasks

    room = report_room(db)
    edition = publish(db, room, [suggestion("acme"), suggestion("globex")])

    tasks.on_edition_published(room, edition)

    views = tasks.list_tasks(actor(db, "bob"), room.id)
    assert sorted(v.title for v in views) == ["Follow up acme", "Follow up globex"]
    for view in views:
        assert view.origin == "suggestion"
        assert view.edition_id == edition.id
        assert view.assignee is None and view.accepted_by is None and not view.dismissed
        row = task_row(db, view.task_id)
        assert row["requires_human"] is True
        assert row["assigned_to_agent"] is None
        assert "thread" not in (row["tags"] or [])
        assert row["assignee_id"] is None
    assert sorted(e["task_id"] for e in room_task_events(db)) == sorted(v.task_id for v in views)
    for event in room_task_events(db):
        assert set(event) == {"room_id", "tenant_id", "task_id"}
        assert event["room_id"] == room.id and event["tenant_id"] == db.tenant


def test_the_publish_extension_point_runs_the_suggestion_hook(db):
    import robothor.engine.tools.handlers.room_tasks  # noqa: F401 - registers the hook
    from robothor.rooms import editions, tasks

    room = report_room(db)
    edition = publish(db, room, [suggestion("acme")])

    editions.on_edition_published(room, edition)

    assert [v.title for v in tasks.list_tasks(actor(db, "alice"), room.id)] == ["Follow up acme"]


def test_a_preview_edition_creates_no_tasks(db):
    from robothor.rooms import tasks

    room = report_room(db)
    edition = publish(db, room, [suggestion("acme")], preview=True)

    tasks.on_edition_published(room, edition)

    assert tasks.list_tasks(actor(db, "alice"), room.id) == []
    assert room_task_events(db) == []


def test_a_suggestion_key_is_created_once_per_workspace(db):
    from robothor.rooms import tasks

    room = report_room(db)
    first = publish(db, room, [suggestion("acme"), suggestion("acme", "Duplicate in one edition")])
    tasks.on_edition_published(room, first)
    second = publish(db, room, [suggestion("acme", "Same key next week")])
    tasks.on_edition_published(room, second)

    views = tasks.list_tasks(actor(db, "alice"), room.id)
    assert [(v.title, v.edition_id) for v in views] == [("Follow up acme", first.id)]


def test_an_accepted_suggestion_is_not_created_again(db):
    from robothor.rooms import tasks

    room = report_room(db)
    tasks.on_edition_published(room, publish(db, room, [suggestion("acme")]))
    [view] = tasks.list_tasks(actor(db, "bob"), room.id)
    tasks.accept_task(actor(db, "bob"), room.id, view.task_id)

    tasks.on_edition_published(room, publish(db, room, [suggestion("acme")]))

    assert [v.task_id for v in tasks.list_tasks(actor(db, "bob"), room.id)] == [view.task_id]


def test_suggestions_without_a_key_are_skipped(db):
    from robothor.rooms import tasks

    room = report_room(db)
    edition = publish(db, room, [{"title": "No key"}, suggestion("acme")])

    tasks.on_edition_published(room, edition)

    assert [v.title for v in tasks.list_tasks(actor(db, "bob"), room.id)] == ["Follow up acme"]


# ─── E2: accept ──────────────────────────────────────────────────────────


def _one_suggested(db):
    from robothor.rooms import tasks

    room = report_room(db)
    tasks.on_edition_published(room, publish(db, room, [suggestion("acme")]))
    [view] = tasks.list_tasks(actor(db, "alice"), room.id)
    db.events.clear()
    return room, view.task_id


def test_accepting_assigns_the_task_to_the_person(db):
    from robothor.rooms import tasks

    room, task_id = _one_suggested(db)

    view = tasks.accept_task(actor(db, "bob"), room.id, task_id)

    assert view.assignee is not None and view.assignee.user_id == db.bob
    assert view.assignee.display_name == "Bob"
    assert view.accepted_by is not None and view.accepted_by.user_id == db.bob
    assert str(task_row(db, task_id)["assignee_id"]) == db.bob
    assert room_task_events(db) == [
        {"room_id": room.id, "tenant_id": db.tenant, "task_id": task_id}
    ]


def test_a_second_accept_by_someone_else_is_refused_naming_who_has_it(db):
    from robothor.rooms import tasks
    from robothor.rooms.errors import TaskAlreadyAccepted

    room, task_id = _one_suggested(db)
    tasks.accept_task(actor(db, "alice"), room.id, task_id)

    with pytest.raises(TaskAlreadyAccepted) as refused:
        tasks.accept_task(actor(db, "bob"), room.id, task_id)

    assert refused.value.code == "already_accepted"
    assert refused.value.by.user_id == db.alice
    assert refused.value.by.display_name == "Alice"
    assert str(task_row(db, task_id)["assignee_id"]) == db.alice


def test_accepting_again_is_harmless_for_the_same_person(db):
    from robothor.rooms import tasks

    room, task_id = _one_suggested(db)
    tasks.accept_task(actor(db, "bob"), room.id, task_id)

    view = tasks.accept_task(actor(db, "bob"), room.id, task_id)

    assert view.assignee is not None and view.assignee.user_id == db.bob


def test_a_task_already_assigned_to_someone_else_cannot_be_accepted(db):
    from robothor.rooms import tasks
    from robothor.rooms.errors import TaskAlreadyAccepted

    room, task_id = _one_suggested(db)
    db.cur.execute("UPDATE crm_tasks SET assignee_id = %s WHERE id = %s", (db.dana, task_id))

    with pytest.raises(TaskAlreadyAccepted) as refused:
        tasks.accept_task(actor(db, "bob"), room.id, task_id)

    assert refused.value.by.user_id == db.dana
    assert str(task_row(db, task_id)["assignee_id"]) == db.dana


def test_a_viewer_cannot_accept_or_dismiss(db):
    from robothor.rooms import tasks
    from robothor.rooms.errors import RoomForbidden

    room, task_id = _one_suggested(db)

    with pytest.raises(RoomForbidden):
        tasks.accept_task(actor(db, "vera"), room.id, task_id)
    with pytest.raises(RoomForbidden):
        tasks.dismiss_task(actor(db, "vera"), room.id, task_id)
    assert task_row(db, task_id)["assignee_id"] is None


@pytest.mark.parametrize("who", ["carol", "olga"])
def test_someone_without_access_gets_not_found(db, who):
    from robothor.rooms import tasks
    from robothor.rooms.errors import RoomNotFound

    room, task_id = _one_suggested(db)
    caller = olga(db) if who == "olga" else actor(db, who)

    for call in (tasks.accept_task, tasks.dismiss_task):
        with pytest.raises(RoomNotFound) as refused:
            call(caller, room.id, task_id)
        assert refused.value.code == "not_found"
    with pytest.raises(RoomNotFound):
        tasks.list_tasks(caller, room.id)


def test_an_archived_workspace_refuses_accept(db):
    from robothor.rooms import dal, tasks
    from robothor.rooms.errors import RoomForbidden

    room, task_id = _one_suggested(db)
    dal.archive_room(db.tenant, room.id)

    with pytest.raises(RoomForbidden) as refused:
        tasks.accept_task(actor(db, "bob"), room.id, task_id)
    assert refused.value.code == "forbidden"


def test_a_task_not_linked_to_the_workspace_is_not_found(db):
    from robothor.rooms import tasks
    from robothor.rooms.errors import TaskNotFound

    room, _ = _one_suggested(db)
    elsewhere = crm_task(db)

    with pytest.raises(TaskNotFound) as refused:
        tasks.accept_task(actor(db, "bob"), room.id, elsewhere)
    assert refused.value.code == "not_found"
    assert task_row(db, elsewhere)["assignee_id"] is None


def test_a_task_workspaces_own_task_cannot_be_taken_from_inside_it(db):
    """Opening a task must never be a way to become its assignee."""
    from robothor.rooms import tasks
    from robothor.rooms.errors import RoomForbidden

    task_id = crm_task(db)
    room = tasks.open_task_room(actor(db, "bob"), task_id)

    with pytest.raises(RoomForbidden):
        tasks.accept_task(actor(db, "bob"), room.id, task_id)
    assert task_row(db, task_id)["assignee_id"] is None


# ─── E5: dismiss ─────────────────────────────────────────────────────────


def test_a_dismissed_suggestion_leaves_the_list_and_never_returns(db):
    from robothor.rooms import tasks

    room, task_id = _one_suggested(db)

    view = tasks.dismiss_task(actor(db, "bob"), room.id, task_id)

    assert view.dismissed is True
    assert tasks.list_tasks(actor(db, "bob"), room.id) == []
    db.cur.execute(
        "SELECT dismissed_by FROM room_tasks WHERE room_id = %s AND task_id = %s",
        (room.id, task_id),
    )
    assert str(db.cur.fetchone()[0]) == db.bob
    row = task_row(db, task_id)
    assert row["resolved_at"] is not None and row["resolution"] == "dismissed"
    assert room_task_events(db) == [
        {"room_id": room.id, "tenant_id": db.tenant, "task_id": task_id}
    ]

    tasks.on_edition_published(room, publish(db, room, [suggestion("acme")]))
    assert tasks.list_tasks(actor(db, "bob"), room.id) == []


def test_a_task_is_decided_once(db):
    from robothor.rooms import tasks
    from robothor.rooms.errors import TaskAlreadyAccepted, TaskAlreadyDismissed

    room, task_id = _one_suggested(db)
    tasks.dismiss_task(actor(db, "bob"), room.id, task_id)
    with pytest.raises(TaskAlreadyDismissed) as again:
        tasks.dismiss_task(actor(db, "alice"), room.id, task_id)
    assert again.value.code == "already_dismissed"
    with pytest.raises(TaskAlreadyDismissed):
        tasks.accept_task(actor(db, "alice"), room.id, task_id)

    room2, task2 = _one_suggested(db)
    tasks.accept_task(actor(db, "alice"), room2.id, task2)
    with pytest.raises(TaskAlreadyAccepted):
        tasks.dismiss_task(actor(db, "bob"), room2.id, task2)


def test_only_a_suggestion_can_be_dismissed(db):
    from robothor.rooms import tasks
    from robothor.rooms.errors import RoomForbidden

    task_id = crm_task(db)
    room = tasks.open_task_room(actor(db, "bob"), task_id)

    with pytest.raises(RoomForbidden):
        tasks.dismiss_task(actor(db, "bob"), room.id, task_id)


# ─── E6: open any task as a workspace ────────────────────────────────────


def _messages(db, room) -> list[dict[str, Any]]:
    db.cur.execute(
        "SELECT cm.message FROM chat_messages cm JOIN chat_sessions s ON s.id = cm.session_id "
        "WHERE s.tenant_id = %s AND s.session_key = %s ORDER BY cm.id",
        (db.tenant, room.session_key),
    )
    return [r[0] for r in db.cur.fetchall()]


def test_opening_a_task_creates_its_workspace_seeded_with_the_task(db):
    from robothor.rooms import dal, tasks

    task_id = crm_task(db, assigned_to_agent="analyst", status="IN_PROGRESS")
    db.cur.execute(
        "INSERT INTO crm_task_history (task_id, from_status, to_status, changed_by, reason, "
        "tenant_id) VALUES (%s, 'TODO', 'IN_PROGRESS', 'analyst', 'Started the draft', %s)",
        (task_id, db.tenant),
    )

    room = tasks.open_task_room(actor(db, "bob"), task_id)

    assert room.kind == "task" and room.task_id == task_id
    assert room.agent_id == "analyst"
    assert room.title == "Renew the contract"
    member = dal.get_member(db.tenant, room.id, db.bob)
    assert member is not None and member.assigned
    [seed] = _messages(db, room)
    text = seed["content"]
    for part in (
        "Renew the contract",
        "Get the renewal signed",
        "IN_PROGRESS",
        "Started the draft",
    ):
        assert part in text
    assert "data" in text.lower()
    [link] = dal.list_room_tasks(db.tenant, room.id)
    assert link.origin == "subject"


def test_opening_again_returns_the_same_workspace_seeded_once(db):
    from robothor.rooms import tasks

    task_id = crm_task(db)
    first = tasks.open_task_room(actor(db, "bob"), task_id)

    again = tasks.open_task_room(actor(db, "bob"), task_id)

    assert again.id == first.id
    assert len(_messages(db, first)) == 1


def test_the_default_chat_agent_runs_a_task_with_no_known_agent(db):
    from robothor.rooms import tasks

    unassigned = tasks.open_task_room(actor(db, "bob"), crm_task(db))
    unknown = tasks.open_task_room(actor(db, "bob"), crm_task(db, assigned_to_agent="ghost"))

    assert unassigned.agent_id == "main"
    assert unknown.agent_id == "main"


def test_someone_else_cannot_join_an_existing_task_workspace_by_opening_it(db):
    from robothor.rooms import dal, tasks
    from robothor.rooms.errors import RoomNotFound

    task_id = crm_task(db)
    room = tasks.open_task_room(actor(db, "bob"), task_id)

    with pytest.raises(RoomNotFound):
        tasks.open_task_room(actor(db, "alice"), task_id)
    assert dal.get_member(db.tenant, room.id, db.alice) is None


def test_the_tasks_assignee_joins_an_existing_task_workspace_assigned(db):
    from robothor.rooms import dal, tasks

    task_id = crm_task(db)
    room = tasks.open_task_room(actor(db, "bob"), task_id)
    db.cur.execute("UPDATE crm_tasks SET assignee_id = %s WHERE id = %s", (db.alice, task_id))

    joined = tasks.open_task_room(actor(db, "alice"), task_id)

    assert joined.id == room.id
    member = dal.get_member(db.tenant, room.id, db.alice)
    assert member is not None and member.assigned


def test_a_removed_member_cannot_add_themselves_back_by_opening_the_task(db):
    from robothor.rooms import dal, definition, tasks
    from robothor.rooms.errors import RoomNotFound

    task_id = crm_task(db)
    room = tasks.open_task_room(actor(db, "bob"), task_id)
    db.cur.execute("UPDATE crm_tasks SET assignee_id = %s WHERE id = %s", (db.alice, task_id))
    tasks.open_task_room(actor(db, "alice"), task_id)
    definition.unshare_room(actor(db, "bob"), room.id, db.alice)

    with pytest.raises(RoomNotFound):
        tasks.open_task_room(actor(db, "alice"), task_id)
    assert dal.get_member(db.tenant, room.id, db.alice) is None

    definition.share_room(actor(db, "bob"), room.id, db.alice)
    assert tasks.open_task_room(actor(db, "alice"), task_id).id == room.id


def test_a_removal_through_the_data_layer_is_remembered_too(db):
    from robothor.rooms import dal, tasks
    from robothor.rooms.errors import RoomNotFound

    task_id = crm_task(db)
    room = tasks.open_task_room(actor(db, "bob"), task_id)
    db.cur.execute("UPDATE crm_tasks SET assignee_id = %s WHERE id = %s", (db.alice, task_id))
    tasks.open_task_room(actor(db, "alice"), task_id)
    dal.remove_member(db.tenant, room.id, db.alice, removed_by=db.bob)

    with pytest.raises(RoomNotFound):
        tasks.open_task_room(actor(db, "alice"), task_id)


def test_an_assignee_cannot_join_an_archived_task_workspace(db):
    from robothor.rooms import dal, tasks
    from robothor.rooms.errors import RoomNotFound

    task_id = crm_task(db)
    room = tasks.open_task_room(actor(db, "bob"), task_id)
    dal.archive_room(db.tenant, room.id)
    db.cur.execute("UPDATE crm_tasks SET assignee_id = %s WHERE id = %s", (db.alice, task_id))

    with pytest.raises(RoomNotFound):
        tasks.open_task_room(actor(db, "alice"), task_id)


def test_an_admin_opens_any_existing_task_workspace(db):
    from robothor.rooms import tasks

    task_id = crm_task(db)
    room = tasks.open_task_room(actor(db, "bob"), task_id)

    assert tasks.open_task_room(actor(db, "dana"), task_id).id == room.id


def test_two_openers_at_once_get_one_workspace(db):
    from robothor.rooms import tasks

    task_id = crm_task(db)
    results: list[Any] = []
    barrier = threading.Barrier(2)

    def open_as(name: str) -> None:
        barrier.wait()
        results.append(tasks.open_task_room(actor(db, name), task_id))

    threads = [threading.Thread(target=open_as, args=(n,)) for n in ("bob", "dana")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({r.id for r in results}) == 1
    db.cur.execute(
        "SELECT COUNT(*) FROM rooms WHERE tenant_id = %s AND task_id = %s", (db.tenant, task_id)
    )
    assert db.cur.fetchone()[0] == 1


def test_opening_a_planner_thread_task_leaves_the_planner_alone(db):
    from robothor.rooms import tasks

    task_id = crm_task(
        db,
        tags=["thread"],
        status="IN_PROGRESS",
        assigned_to_agent="analyst",
        next_action="Draft the proposal",
    )
    before = task_row(db, task_id)

    tasks.open_task_room(actor(db, "bob"), task_id)

    assert task_row(db, task_id) == before


def test_a_viewer_cannot_create_a_task_workspace(db):
    from robothor.rooms import tasks
    from robothor.rooms.errors import RoomForbidden

    with pytest.raises(RoomForbidden):
        tasks.open_task_room(actor(db, "vera"), crm_task(db))


def test_a_service_identity_cannot_open_a_task(db):
    from robothor.rooms import tasks
    from robothor.rooms.access import Actor
    from robothor.rooms.errors import RoomForbidden

    service = Actor(user_id=db.bob, role="member", tenant_id=db.tenant, is_service=True)
    with pytest.raises(RoomForbidden):
        tasks.open_task_room(service, crm_task(db))


@pytest.mark.parametrize("where", ["other_tenant", "missing", "malformed"])
def test_a_task_outside_the_company_is_not_found(db, where):
    from robothor.rooms import tasks
    from robothor.rooms.errors import TaskNotFound

    task_id = {
        "other_tenant": lambda: crm_task(db, tenant=db.other),
        "missing": lambda: str(uuid.uuid4()),
        "malformed": lambda: "not-a-uuid",
    }[where]()

    with pytest.raises(TaskNotFound):
        tasks.open_task_room(actor(db, "bob"), task_id)


# ─── status changes reach linked workspaces ──────────────────────────────


def test_a_status_change_is_published_to_every_linked_workspace(db):
    from robothor.crm import dal as crm_dal
    from robothor.rooms import dal, tasks

    room, task_id = _one_suggested(db)
    task_room = tasks.open_task_room(actor(db, "bob"), task_id)
    other = report_room(db)
    dal.link_task(db.tenant, other.id, task_id)
    db.events.clear()

    assert crm_dal.update_task(task_id, status="IN_PROGRESS", tenant_id=db.tenant)

    assert sorted(e["room_id"] for e in room_task_events(db)) == sorted(
        [room.id, task_room.id, other.id]
    )
    for event in room_task_events(db):
        assert event == {"room_id": event["room_id"], "tenant_id": db.tenant, "task_id": task_id}


def test_a_rejected_review_is_published_to_its_workspace(db):
    """The Tasks view's reject moves a task back to in progress: the
    workspace showing it hears of it, as for every other status change."""
    from robothor.crm import dal as crm_dal

    room, task_id = _one_suggested(db)
    db.cur.execute("UPDATE crm_tasks SET status = 'REVIEW' WHERE id = %s", (task_id,))
    db.events.clear()

    assert crm_dal.reject_task(task_id, "needs a source", "u-reviewer", tenant_id=db.tenant) is True

    assert room_task_events(db) == [
        {"room_id": room.id, "tenant_id": db.tenant, "task_id": task_id}
    ]


def test_a_deleted_task_is_published_to_its_workspace(db):
    from robothor.crm import dal as crm_dal

    room, task_id = _one_suggested(db)
    db.events.clear()

    assert crm_dal.delete_task(task_id, tenant_id=db.tenant) is True

    assert room_task_events(db) == [
        {"room_id": room.id, "tenant_id": db.tenant, "task_id": task_id}
    ]


def test_an_update_that_keeps_the_status_publishes_nothing(db):
    from robothor.crm import dal as crm_dal

    _, task_id = _one_suggested(db)

    assert crm_dal.update_task(task_id, title="Renamed", tenant_id=db.tenant)

    assert room_task_events(db) == []


# ─── only the person's own run acts for them ─────────────────────────────


def _run(db, *, trigger: str = "webchat", parent: str | None = None) -> str:
    run_id = str(uuid.uuid4())
    db.cur.execute(
        "INSERT INTO agent_runs (id, tenant_id, agent_id, trigger_type, parent_run_id, status) "
        "VALUES (%s, %s, 'main', %s, %s, 'running')",
        (run_id, db.tenant, trigger, parent),
    )
    return run_id


def test_a_top_level_run_may_act_and_a_sub_agent_may_not(db):
    from robothor.rooms import tasks

    try:
        top = _run(db)
        child = _run(db, trigger="sub_agent", parent=top)
        orphan_child = _run(db, trigger="sub_agent")
        nested = _run(db, trigger="webchat", parent=top)

        assert tasks.run_is_top_level(db.tenant, top) is True
        assert tasks.run_is_top_level(db.tenant, child) is False
        assert tasks.run_is_top_level(db.tenant, orphan_child) is False
        assert tasks.run_is_top_level(db.tenant, nested) is False
        assert tasks.run_is_top_level(db.other, top) is False
        assert tasks.run_is_top_level(db.tenant, str(uuid.uuid4())) is False
        assert tasks.run_is_top_level(db.tenant, "run-1") is False
    finally:
        db.cur.execute("DELETE FROM agent_runs WHERE tenant_id = %s AND parent_run_id IS NOT NULL",
                       (db.tenant,))  # fmt: skip
        db.cur.execute("DELETE FROM agent_runs WHERE tenant_id = %s", (db.tenant,))
