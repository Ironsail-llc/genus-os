"""The workspaces ("rooms") bridge API against real Postgres.

Personas, all in one company except Eve: Alice (member, creates and is
assigned), Bob (member, shared), Vera (viewer, shared), Dana (company admin,
no membership), Carol (member, no access), Eve (admin of another company) and
Ivan (a disabled account).

Stories: A3 (each settings field round-trips, a bad schedule saves nothing,
a stale form gets 409), A6 (archive, admin restore returns paused), A7
(history readable by a shared user, restore only by the assigned), C6
(per-user unread, a read marks only the caller), G1 (share, other company and
inactive refused, no onward share), G2 (an unshared member's next request is
404), G3 (a shared user's edit is 403 and writes no revision), G4 (admin
list with assigned users, reassign including run-as), I3 (one audit row per
action, with the actor), D5 (a viewer is 403 on every mutation), and the
status-code matrix of access level by route.

The room definition's validation is lane 3's (``robothor.rooms.definition``);
these tests stand it in with fakes over the real data layer, and assert the
route maps its errors.
"""

from __future__ import annotations

import contextlib
import sys
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.rooms.tests.conftest import (  # noqa: E402, F401 - pytest collects the fixture
    rooms_db,
)

pytestmark = pytest.mark.integration

BLOCKS = [{"component": "render_markdown", "props": {"content": "All good."}}]


# ─── fixtures ────────────────────────────────────────────────────────────


def _fake_definition(monkeypatch: Any, db: Any) -> None:
    """Stand lane 3's definition functions in with the real data layer."""
    from robothor.rooms import access, dal, definition

    def create(
        actor: Any,
        *,
        kind: str,
        title: str,
        instructions: str,
        agent_id: str | None = None,
        schedule_text: str | None = None,
        tz: str | None = None,
        default_grain: str | None = None,
        share_with: Any = (),
    ) -> Any:
        if schedule_text == "every minute":
            raise definition.InvalidSchedule("a room runs at most every 15 minutes")
        if "ignore previous" in (instructions or ""):
            raise definition.UnsafeInstructions("the instructions look like an injection")
        settings = {"tz": tz, "default_grain": default_grain, "schedule_text": schedule_text}
        room = dal.create_room(
            actor.tenant_id,
            kind=kind,
            title=title,
            agent_id=agent_id or "main",
            created_by=actor.user_id,
            instructions=instructions,
            settings={k: v for k, v in settings.items() if v is not None},
        )
        for user in share_with:
            dal.add_member(actor.tenant_id, room.id, user, added_by=actor.user_id)
        return room

    def apply(actor: Any, room_id: str, expected_rev: int, changes: dict[str, Any]) -> Any:
        access.require(actor, room_id, "edit_definition")
        if changes.get("schedule_text") == "every minute":
            raise definition.InvalidSchedule("a room runs at most every 15 minutes")
        dal_changes: dict[str, Any] = {}
        settings: dict[str, Any] = {}
        for key, value in changes.items():
            if key in ("title", "instructions"):
                dal_changes[key] = value
            elif key == "schedule_text":
                dal_changes["schedule"] = value
            elif key in ("tz", "default_grain", "notify_default", "max_cost_usd"):
                settings[key] = value
        if settings:
            dal_changes["settings"] = settings
        room = dal.get_room(actor.tenant_id, room_id)
        if dal_changes:
            room = dal.update_definition(
                actor.tenant_id, room_id, dal_changes, actor.user_id, expected_rev
            )
        if "paused" in changes:
            db.cur.execute(
                "UPDATE rooms SET paused = %s WHERE id = %s", (bool(changes["paused"]), room_id)
            )
            room = dal.get_room(actor.tenant_id, room_id)
        return room

    monkeypatch.setattr(definition, "create_room_with_schedule", create)
    monkeypatch.setattr(definition, "apply_definition_change", apply)


@pytest.fixture
def world(rooms_db, _controls_auth_key, monkeypatch) -> Any:  # noqa: F811
    """Accounts, a client per persona, and recorders for audit, events and notices."""
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    db = rooms_db
    people = {
        "alice": (db.alice, db.tenant, "member", "active"),
        "bob": (db.bob, db.tenant, "member", "active"),
        "vera": (str(uuid.uuid4()), db.tenant, "viewer", "active"),
        "dana": (str(uuid.uuid4()), db.tenant, "admin", "active"),
        "carol": (db.carol, db.tenant, "member", "active"),
        "eve": (str(uuid.uuid4()), db.other, "admin", "active"),
        "ivan": (str(uuid.uuid4()), db.tenant, "member", "disabled"),
    }
    for name, (uid, tenant, role, status) in people.items():
        db.cur.execute(
            "INSERT INTO user_accounts (id, tenant_id, email, display_name, role, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (uid, tenant, f"{name}@example.com", name.capitalize(), role, status),
        )
    ids = SimpleNamespace(**{name: row[0] for name, row in people.items()})
    try:
        yield _wire(db, ids, people, tokens, TestClient, monkeypatch)
    finally:
        db.cur.execute(
            "DELETE FROM user_cronjobs WHERE tenant_id = ANY(%s)", ([db.tenant, db.other],)
        )
        with contextlib.suppress(Exception):
            db.cur.execute(
                "DELETE FROM user_accounts WHERE tenant_id = ANY(%s)", ([db.tenant, db.other],)
            )


def _wire(db: Any, ids: Any, people: Any, tokens: Any, TestClient: Any, monkeypatch: Any) -> Any:  # noqa: N803

    def client(name: str) -> Any:
        uid, tenant, role, _ = people[name]
        c = TestClient(app_())
        c.headers.update(
            {"Authorization": f"Bearer {tokens.issue_access_token(uid, tenant, role)}"}
        )
        return c

    audit: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    notices: list[dict[str, Any]] = []

    def record_audit(event_type: str, **kwargs: Any) -> None:
        audit.append({"event_type": event_type, **kwargs})

    monkeypatch.setattr("routers._audit.log_event", record_audit)
    monkeypatch.setattr(
        "routers.rooms.publish",
        lambda stream, event_type, payload, **kw: events.append((event_type, payload)),
    )
    monkeypatch.setattr("routers.rooms.send_notification", lambda **kw: notices.append(kw) or "n-1")
    engine = AsyncMock(return_value=(202, {"editionId": "e-1", "status": "building"}))
    monkeypatch.setattr("routers.rooms.engine_request", engine)
    _fake_definition(monkeypatch, db)

    return SimpleNamespace(
        db=db,
        ids=ids,
        client=client,
        audit=audit,
        events=events,
        notices=notices,
        engine=engine,
    )


def app_() -> Any:
    from bridge_service import app

    return app


def _room(w: Any, **overrides: Any) -> Any:
    """Alice's report room, shared with Bob and Vera."""
    from robothor.rooms import dal

    fields: dict[str, Any] = {
        "kind": "report",
        "title": "Weekly numbers",
        "agent_id": "main",
        "created_by": w.ids.alice,
        "instructions": "Summarise the week.",
        "settings": {"tz": "UTC", "default_grain": "week"},
    }
    fields.update(overrides)
    room = dal.create_room(w.db.tenant, **fields)
    dal.add_member(w.db.tenant, room.id, w.ids.bob, added_by=w.ids.alice)
    dal.add_member(w.db.tenant, room.id, w.ids.vera, added_by=w.ids.alice)
    return room


def _messages(w: Any, room: Any, n: int) -> None:
    w.db.cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel) VALUES (%s, %s, 'webchat') "
        "ON CONFLICT (tenant_id, session_key) DO UPDATE SET channel = EXCLUDED.channel "
        "RETURNING id",
        (room.tenant_id, room.session_key),
    )
    session_id = w.db.cur.fetchone()[0]
    for i in range(n):
        w.db.cur.execute(
            "INSERT INTO chat_messages (session_id, message) "
            "VALUES (%s, jsonb_build_object('role', 'user', 'content', %s::text))",
            (session_id, f"message {i}"),
        )


def _edition(w: Any, room: Any, *, preview: bool = False, requested_by: str | None = None) -> Any:
    from robothor.rooms import dal

    edition, _ = dal.create_edition(
        w.db.tenant,
        room.id,
        preview=preview,
        grain="week",
        period_start=date(2026, 11, 9),
        period_end=date(2026, 11, 15),
        requested_by=requested_by,
    )
    return dal.publish_edition(
        w.db.tenant, edition.id, title="Week 46", summary="Up 4%.", blocks=BLOCKS
    )


def _task(w: Any, title: str = "Call Bob back") -> str:
    w.db.cur.execute(
        "INSERT INTO crm_tasks (tenant_id, title) VALUES (%s, %s) RETURNING id",
        (w.db.tenant, title),
    )
    return str(w.db.cur.fetchone()[0])


def _audits(w: Any, event_type: str) -> list[dict[str, Any]]:
    return [a for a in w.audit if a["event_type"] == event_type]


# ─── list ────────────────────────────────────────────────────────────────


def test_list_mine_is_camel_case_with_the_latest_edition(world) -> None:
    w = world
    room = _room(w)
    edition = _edition(w, room)

    response = w.client("bob").get("/api/rooms")

    assert response.status_code == 200
    rooms = response.json()["rooms"]
    assert [r["id"] for r in rooms] == [room.id]
    summary = rooms[0]
    assert set(summary) >= {
        "id",
        "kind",
        "title",
        "agentId",
        "paused",
        "archived",
        "unread",
        "lastActivityAt",
        "latestEdition",
    }
    assert summary["agentId"] == "main"
    assert summary["latestEdition"] == {
        "id": edition.id,
        "seq": 1,
        "publishedSeq": 1,
        "title": "Week 46",
        "periodStart": "2026-11-09",
        "periodEnd": "2026-11-15",
    }


def test_list_mine_leaves_out_rooms_the_caller_is_not_in(world) -> None:
    w = world
    _room(w)
    assert w.client("carol").get("/api/rooms").json() == {"rooms": []}
    assert w.client("eve").get("/api/rooms").json() == {"rooms": []}


def test_list_all_needs_an_admin(world) -> None:
    w = world
    room = _room(w)
    for name in ("alice", "bob", "vera", "carol"):
        assert w.client(name).get("/api/rooms?scope=all").status_code == 403, name

    response = w.client("dana").get("/api/rooms?scope=all")

    assert response.status_code == 200
    assert [r["id"] for r in response.json()["rooms"]] == [room.id]


def test_admin_list_names_the_assigned_users_and_includes_archived(world) -> None:
    """G4.1: every thread in the company, with its assigned users."""
    from robothor.rooms import dal

    w = world
    room = _room(w)
    archived = dal.create_room(
        w.db.tenant, kind="report", title="Old report", agent_id="main", created_by=w.ids.bob
    )
    dal.archive_room(w.db.tenant, archived.id)
    _room(w, title="Elsewhere")  # still the same company
    dal.create_room(
        w.db.other, kind="agent", title="Not ours", agent_id="main", created_by=w.ids.eve
    )

    rooms = {r["id"]: r for r in w.client("dana").get("/api/rooms?scope=all").json()["rooms"]}

    assert room.id in rooms and archived.id in rooms and len(rooms) == 3
    assert rooms[room.id]["assigned"] == [{"userId": w.ids.alice, "displayName": "Alice"}]
    assert rooms[archived.id]["archived"] is True
    assert rooms[archived.id]["assigned"] == [{"userId": w.ids.bob, "displayName": "Bob"}]


def test_list_scope_must_be_mine_or_all(world) -> None:
    assert world.client("alice").get("/api/rooms?scope=everyone").status_code == 422


# ─── unread (C6) ─────────────────────────────────────────────────────────


def test_unread_is_per_user_and_read_marks_only_the_caller(world) -> None:
    w = world
    room = _room(w)
    _messages(w, room, 3)
    _edition(w, room)

    def unread(name: str) -> int:
        return w.client(name).get("/api/rooms").json()["rooms"][0]["unread"]

    assert (unread("alice"), unread("bob")) == (4, 4)

    response = w.client("bob").post(f"/api/rooms/{room.id}/read")

    assert response.status_code == 200
    assert unread("bob") == 0
    assert unread("alice") == 4
    assert w.client("bob").get(f"/api/rooms/{room.id}").json()["unread"] == 0
    assert w.client("alice").get(f"/api/rooms/{room.id}").json()["unread"] == 4


def test_a_viewer_can_mark_a_room_read(world) -> None:
    """Reading is not changing anything: Vera's own marker moves."""
    w = world
    room = _room(w)
    _messages(w, room, 2)

    assert w.client("vera").post(f"/api/rooms/{room.id}/read").status_code == 200
    assert w.client("vera").get("/api/rooms").json()["rooms"][0]["unread"] == 0


def test_read_of_a_room_without_access_is_404(world) -> None:
    w = world
    room = _room(w)
    assert w.client("carol").post(f"/api/rooms/{room.id}/read").status_code == 404


# ─── get ─────────────────────────────────────────────────────────────────


def test_get_room_carries_access_members_and_runs_as(world) -> None:
    w = world
    room = _room(w)

    body = w.client("bob").get(f"/api/rooms/{room.id}").json()

    assert body["id"] == room.id
    assert body["access"] == "shared"
    assert body["instructions"] == "Summarise the week."
    assert body["settings"] == {"tz": "UTC", "default_grain": "week"}
    assert body["rev"] == 1
    assert body["runsAs"] == {"userId": w.ids.alice, "displayName": "Alice"}
    assert body["members"] == [
        {"userId": w.ids.alice, "displayName": "Alice", "assigned": True},
        {"userId": w.ids.bob, "displayName": "Bob", "assigned": False},
        {"userId": w.ids.vera, "displayName": "Vera", "assigned": False},
    ]
    assert body["nextRunAt"] is None
    assert "sessionKey" not in body and "session_key" not in body


@pytest.mark.parametrize(
    ("name", "level"),
    [("alice", "assigned"), ("bob", "shared"), ("vera", "viewer"), ("dana", "admin")],
)
def test_get_room_reports_the_callers_access(world, name, level) -> None:
    room = _room(world)
    assert world.client(name).get(f"/api/rooms/{room.id}").json()["access"] == level


def test_next_run_at_comes_from_the_rooms_schedule(world) -> None:
    w = world
    room = _room(w)
    w.db.cur.execute(
        "INSERT INTO user_cronjobs (job_id, tenant_id, agent_id, schedule_kind, schedule_payload, "
        "prompt, next_run_at, room_id, run_as_user_id) VALUES (%s, %s, 'main', 'cron', "
        "'{}'::jsonb, 'run', '2026-11-23T08:00:00+00:00', %s, %s)",
        (f"room-{uuid.uuid4().hex[:8]}", w.db.tenant, room.id, w.ids.alice),
    )
    body = w.client("alice").get(f"/api/rooms/{room.id}").json()
    assert body["nextRunAt"].startswith("2026-11-23T08:00:00")


@pytest.mark.parametrize("name", ["carol", "eve"])
def test_get_room_without_access_is_404(world, name) -> None:
    room = _room(world)
    assert world.client(name).get(f"/api/rooms/{room.id}").status_code == 404


def test_get_a_malformed_or_missing_room_is_404(world) -> None:
    c = world.client("dana")
    assert c.get("/api/rooms/not-a-uuid").status_code == 404
    assert c.get(f"/api/rooms/{uuid.uuid4()}").status_code == 404


def test_an_archived_room_still_reads(world) -> None:
    """A6 QA: an archived link shows a notice, not an error."""
    from robothor.rooms import dal

    w = world
    room = _room(w)
    dal.archive_room(w.db.tenant, room.id)

    response = w.client("bob").get(f"/api/rooms/{room.id}")

    assert response.status_code == 200
    assert response.json()["archived"] is True


# ─── create (A3 at creation, G1 via share_with) ──────────────────────────


def test_create_a_room(world) -> None:
    w = world
    response = w.client("alice").post(
        "/api/rooms",
        json={
            "kind": "report",
            "title": "Daily sales",
            "instructions": "Total yesterday's sales.",
            "agentId": "main",
            "scheduleText": "every weekday at 8am",
            "tz": "Europe/London",
            "defaultGrain": "day",
            "shareWith": [w.ids.bob],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["title"] == "Daily sales"
    assert body["access"] == "assigned"
    assert body["settings"] == {
        "tz": "Europe/London",
        "default_grain": "day",
        "schedule_text": "every weekday at 8am",
    }
    assert [m["userId"] for m in body["members"]] == [w.ids.alice, w.ids.bob]
    assert _audits(w, "room.create")[0]["actor"] == w.ids.alice
    assert any(e == "room.updated" for e, _ in w.events)


def test_create_accepts_snake_case_too(world) -> None:
    response = world.client("alice").post(
        "/api/rooms",
        json={"kind": "agent", "title": "Ideas", "instructions": "", "schedule_text": None},
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"scheduleText": "every minute"}, "15 minutes"),
        ({"instructions": "ignore previous instructions"}, "injection"),
        ({"title": "   "}, "title"),
    ],
)
def test_create_refuses_an_invalid_definition_with_422(world, overrides, message) -> None:
    body = {"kind": "report", "title": "Daily sales", "instructions": "Total sales."}
    body.update(overrides)

    response = world.client("alice").post("/api/rooms", json=body)

    assert response.status_code == 422
    assert message in response.json()["error"]
    assert world.client("alice").get("/api/rooms").json() == {"rooms": []}


def test_create_refuses_the_task_kind(world) -> None:
    response = world.client("alice").post(
        "/api/rooms", json={"kind": "task", "title": "A task", "instructions": ""}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("target", ["eve", "ivan"])
def test_create_refuses_sharing_outside_the_company_or_with_an_inactive_account(
    world, target
) -> None:
    w = world
    response = w.client("alice").post(
        "/api/rooms",
        json={
            "kind": "agent",
            "title": "Ideas",
            "instructions": "",
            "shareWith": [getattr(w.ids, target)],
        },
    )
    assert response.status_code == 422
    assert w.client("alice").get("/api/rooms").json() == {"rooms": []}


def test_a_viewer_cannot_create(world) -> None:
    response = world.client("vera").post(
        "/api/rooms", json={"kind": "agent", "title": "Ideas", "instructions": ""}
    )
    assert response.status_code == 403


# ─── update (A3, G3) ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("change", "check"),
    [
        ({"title": "Monthly numbers"}, lambda b: b["title"] == "Monthly numbers"),
        ({"instructions": "Be brief."}, lambda b: b["instructions"] == "Be brief."),
        (
            {"schedule_text": "every monday at 9am"},
            lambda b: b["settings"]["schedule_text"] == "every monday at 9am",
        ),
        ({"tz": "America/New_York"}, lambda b: b["settings"]["tz"] == "America/New_York"),
        ({"default_grain": "month"}, lambda b: b["settings"]["default_grain"] == "month"),
        (
            {"notify_default": "summary"},
            lambda b: b["settings"]["notify_default"] == "summary",
        ),
        ({"max_cost_usd": 2.5}, lambda b: b["settings"]["max_cost_usd"] == 2.5),
        ({"paused": True}, lambda b: b["paused"] is True),
    ],
)
def test_each_settings_field_round_trips(world, change, check) -> None:
    w = world
    room = _room(w)

    response = w.client("alice").patch(
        f"/api/rooms/{room.id}", json={"expected_rev": 1, "changes": change}
    )

    assert response.status_code == 200, response.text
    assert check(response.json())
    assert check(w.client("bob").get(f"/api/rooms/{room.id}").json())
    assert _audits(w, "room.update")[0]["actor"] == w.ids.alice


def test_update_accepts_camel_case_keys(world) -> None:
    w = world
    room = _room(w)
    response = w.client("alice").patch(
        f"/api/rooms/{room.id}",
        json={"expectedRev": 1, "changes": {"scheduleText": "daily at 7am", "defaultGrain": "day"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["settings"]["schedule_text"] == "daily at 7am"
    assert response.json()["settings"]["default_grain"] == "day"


def test_a_bad_schedule_is_422_and_saves_nothing(world) -> None:
    w = world
    room = _room(w)

    response = w.client("alice").patch(
        f"/api/rooms/{room.id}",
        json={"expected_rev": 1, "changes": {"schedule_text": "every minute"}},
    )

    assert response.status_code == 422
    assert "15 minutes" in response.json()["error"]
    assert w.client("alice").get(f"/api/rooms/{room.id}").json()["rev"] == 1


def test_a_stale_form_is_409_with_the_current_rev(world) -> None:
    w = world
    room = _room(w)
    first = w.client("alice").patch(
        f"/api/rooms/{room.id}", json={"expected_rev": 1, "changes": {"title": "First"}}
    )
    assert first.status_code == 200

    second = w.client("dana").patch(
        f"/api/rooms/{room.id}", json={"expected_rev": 1, "changes": {"title": "Second"}}
    )

    assert second.status_code == 409
    assert second.json()["currentRev"] == 2
    assert w.client("alice").get(f"/api/rooms/{room.id}").json()["title"] == "First"


def test_update_needs_expected_rev_and_changes(world) -> None:
    w = world
    room = _room(w)
    c = w.client("alice")
    assert c.patch(f"/api/rooms/{room.id}", json={"changes": {"title": "X"}}).status_code == 422
    assert c.patch(f"/api/rooms/{room.id}", json={"expected_rev": 1}).status_code == 422


def test_a_shared_users_edit_is_403_and_writes_no_revision(world) -> None:
    """G3 (API path): refused, and the refusal offers the duplicate."""
    from robothor.rooms import dal

    w = world
    room = _room(w)

    response = w.client("bob").patch(
        f"/api/rooms/{room.id}", json={"expected_rev": 1, "changes": {"title": "Mine now"}}
    )

    assert response.status_code == 403
    assert "duplicate" in response.json()["error"].lower()
    assert [r.rev for r in dal.list_revisions(w.db.tenant, room.id)] == [1]


# ─── archive (A6) ────────────────────────────────────────────────────────


def test_archive_then_admin_restore_returns_paused(world) -> None:
    w = world
    room = _room(w)

    archived = w.client("alice").post(f"/api/rooms/{room.id}/archive")

    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert w.client("alice").get("/api/rooms").json() == {"rooms": []}
    assert w.client("bob").get("/api/rooms").json() == {"rooms": []}

    assert w.client("alice").post(f"/api/rooms/{room.id}/unarchive").status_code == 403
    restored = w.client("dana").post(f"/api/rooms/{room.id}/unarchive")

    assert restored.status_code == 200
    assert restored.json()["archived"] is False
    assert restored.json()["paused"] is True
    assert [r["id"] for r in w.client("bob").get("/api/rooms").json()["rooms"]] == [room.id]
    assert _audits(w, "room.archive")[0]["actor"] == w.ids.alice
    assert _audits(w, "room.unarchive")[0]["actor"] == w.ids.dana


def test_archive_keeps_history_editions_and_tasks(world) -> None:
    from robothor.rooms import dal

    w = world
    room = _room(w)
    _messages(w, room, 2)
    edition = _edition(w, room)
    task = _task(w)
    dal.link_task(w.db.tenant, room.id, task)

    w.client("alice").post(f"/api/rooms/{room.id}/archive")

    bob = w.client("bob")
    assert [e["id"] for e in bob.get(f"/api/rooms/{room.id}/editions").json()["editions"]] == [
        edition.id
    ]
    assert [t["taskId"] for t in bob.get(f"/api/rooms/{room.id}/tasks").json()["tasks"]] == [task]


def test_a_shared_user_cannot_archive(world) -> None:
    room = _room(world)
    assert world.client("bob").post(f"/api/rooms/{room.id}/archive").status_code == 403


# ─── duplicate ───────────────────────────────────────────────────────────


def test_a_shared_user_duplicates_as_their_own(world) -> None:
    w = world
    room = _room(w)

    response = w.client("bob").post(f"/api/rooms/{room.id}/duplicate")

    assert response.status_code == 201
    body = response.json()
    assert body["id"] != room.id
    assert body["access"] == "assigned"
    assert body["instructions"] == "Summarise the week."
    assert [m["userId"] for m in body["members"]] == [w.ids.bob]
    assert _audits(w, "room.duplicate")[0]["actor"] == w.ids.bob


# ─── revisions (A7) ──────────────────────────────────────────────────────


def test_history_is_readable_by_a_shared_user_and_names_who_changed_it(world) -> None:
    w = world
    room = _room(w)
    w.client("alice").patch(
        f"/api/rooms/{room.id}", json={"expected_rev": 1, "changes": {"title": "Renamed"}}
    )

    response = w.client("bob").get(f"/api/rooms/{room.id}/revisions")

    assert response.status_code == 200
    revisions = response.json()["revisions"]
    assert [r["rev"] for r in revisions] == [2, 1]
    assert revisions[0]["changedBy"] == w.ids.alice
    assert revisions[0]["changedByName"] == "Alice"
    assert revisions[0]["changedAt"]
    assert revisions[0]["definition"]["title"] == "Renamed"


def test_restore_writes_a_new_revision_equal_to_the_old_one(world) -> None:
    w = world
    room = _room(w)
    w.client("alice").patch(
        f"/api/rooms/{room.id}", json={"expected_rev": 1, "changes": {"title": "Renamed"}}
    )

    response = w.client("alice").post(f"/api/rooms/{room.id}/revisions/1/restore")

    assert response.status_code == 200
    assert response.json()["rev"] == 3
    assert response.json()["title"] == "Weekly numbers"
    assert _audits(w, "room.restore")[0]["actor"] == w.ids.alice


def test_restore_with_a_stale_expected_rev_is_409(world) -> None:
    w = world
    room = _room(w)
    w.client("alice").patch(
        f"/api/rooms/{room.id}", json={"expected_rev": 1, "changes": {"title": "Renamed"}}
    )
    response = w.client("alice").post(
        f"/api/rooms/{room.id}/revisions/1/restore", json={"expected_rev": 1}
    )
    assert response.status_code == 409
    assert response.json()["currentRev"] == 2


def test_restore_of_a_missing_revision_is_404(world) -> None:
    room = _room(world)
    response = world.client("alice").post(f"/api/rooms/{room.id}/revisions/9/restore")
    assert response.status_code == 404


def test_a_shared_user_cannot_restore(world) -> None:
    room = _room(world)
    assert world.client("bob").post(f"/api/rooms/{room.id}/revisions/1/restore").status_code == 403


# ─── members (G1, G2) ────────────────────────────────────────────────────


def test_share_with_a_colleague_notifies_them_and_puts_it_in_their_rail(world) -> None:
    w = world
    room = _room(w)

    response = w.client("alice").put(f"/api/rooms/{room.id}/members/{w.ids.carol}", json={})

    assert response.status_code == 200
    assert response.json() == {"userId": w.ids.carol, "displayName": "Carol", "assigned": False}
    assert [r["id"] for r in w.client("carol").get("/api/rooms").json()["rooms"]] == [room.id]
    notice = w.notices[0]
    assert notice["to_agent"] == w.ids.carol
    assert notice["tenant_id"] == w.db.tenant
    assert notice["metadata"]["room_id"] == room.id
    assert notice["metadata"]["link"] == f"/?v=chat&room={room.id}"
    assert room.session_key not in str(notice)
    share = _audits(w, "room.share")[0]
    assert share["actor"] == w.ids.alice
    assert share["details"]["user_id"] == w.ids.carol
    assert ("room.updated", {"room_id": room.id, "tenant_id": w.db.tenant}) in w.events


def test_room_events_carry_ids_only(world) -> None:
    """Listeners filter by tenant, then by the rooms the user can see."""
    from robothor.rooms import dal

    w = world
    room = _room(w)
    task = _task(w)
    dal.link_task(w.db.tenant, room.id, task)
    alice = w.client("alice")
    alice.patch(f"/api/rooms/{room.id}", json={"expected_rev": 1, "changes": {"title": "X"}})
    alice.put(f"/api/rooms/{room.id}/members/{w.ids.carol}", json={})
    alice.post(f"/api/rooms/{room.id}/tasks/{task}/accept")
    alice.post(f"/api/rooms/{room.id}/archive")

    assert len(w.events) == 4
    for event_type, payload in w.events:
        assert event_type == "room.updated"
        assert set(payload) <= {"room_id", "tenant_id", "message_id", "edition_id", "task_id"}
        assert payload["room_id"] == room.id and payload["tenant_id"] == w.db.tenant


def test_share_as_assigned(world) -> None:
    w = world
    room = _room(w)
    response = w.client("alice").put(
        f"/api/rooms/{room.id}/members/{w.ids.bob}", json={"assigned": True}
    )
    assert response.status_code == 200
    assert response.json()["assigned"] is True
    assert w.client("bob").get(f"/api/rooms/{room.id}").json()["access"] == "assigned"


@pytest.mark.parametrize("target", ["eve", "ivan"])
def test_share_outside_the_company_or_with_an_inactive_account_is_refused(world, target) -> None:
    w = world
    room = _room(w)
    response = w.client("alice").put(
        f"/api/rooms/{room.id}/members/{getattr(w.ids, target)}", json={}
    )
    assert response.status_code == 422
    assert w.notices == []


def test_share_with_a_stranger_id_is_refused(world) -> None:
    room = _room(world)
    response = world.client("alice").put(f"/api/rooms/{room.id}/members/{uuid.uuid4()}", json={})
    assert response.status_code == 422


def test_a_shared_user_cannot_share_onward(world) -> None:
    w = world
    room = _room(w)
    response = w.client("bob").put(f"/api/rooms/{room.id}/members/{w.ids.carol}", json={})
    assert response.status_code == 403
    assert w.client("carol").get("/api/rooms").json() == {"rooms": []}


def test_members_list(world) -> None:
    w = world
    room = _room(w)
    response = w.client("vera").get(f"/api/rooms/{room.id}/members")
    assert response.status_code == 200
    assert [m["displayName"] for m in response.json()["members"]] == ["Alice", "Bob", "Vera"]


def test_an_unshared_member_gets_404_on_their_next_request(world) -> None:
    """G2: including an open tab — access is re-checked on every request."""
    w = world
    room = _room(w)
    _messages(w, room, 2)
    bob = w.client("bob")
    assert bob.get(f"/api/rooms/{room.id}").status_code == 200

    response = w.client("alice").delete(f"/api/rooms/{room.id}/members/{w.ids.bob}")

    assert response.status_code == 200
    assert bob.get(f"/api/rooms/{room.id}").status_code == 404
    assert bob.get(f"/api/rooms/{room.id}/editions").status_code == 404
    assert bob.post(f"/api/rooms/{room.id}/read").status_code == 404
    assert bob.get("/api/rooms").json() == {"rooms": []}
    # history unchanged
    w.db.cur.execute(
        "SELECT COUNT(*) FROM chat_messages cm JOIN chat_sessions s ON s.id = cm.session_id "
        "WHERE s.session_key = %s",
        (room.session_key,),
    )
    assert w.db.cur.fetchone()[0] == 2
    unshare = _audits(w, "room.unshare")[0]
    assert unshare["actor"] == w.ids.alice and unshare["details"]["user_id"] == w.ids.bob


def test_unshare_of_a_non_member_is_404(world) -> None:
    w = world
    room = _room(w)
    response = w.client("alice").delete(f"/api/rooms/{room.id}/members/{w.ids.carol}")
    assert response.status_code == 404


# ─── reassign (G4) ───────────────────────────────────────────────────────


def test_admin_reassigns_a_deactivated_creators_room(world) -> None:
    w = world
    room = _room(w)
    w.db.cur.execute("UPDATE user_accounts SET status = 'disabled' WHERE id = %s", (w.ids.alice,))

    response = w.client("dana").post(f"/api/rooms/{room.id}/reassign", json={"userId": w.ids.bob})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runsAs"] == {"userId": w.ids.bob, "displayName": "Bob"}
    assigned = {m["userId"]: m["assigned"] for m in body["members"]}
    assert assigned[w.ids.bob] is True and assigned[w.ids.alice] is False
    assert w.client("bob").get(f"/api/rooms/{room.id}").json()["access"] == "assigned"
    reassign = _audits(w, "room.reassign")[0]
    assert reassign["actor"] == w.ids.dana and reassign["details"]["user_id"] == w.ids.bob


@pytest.mark.parametrize("target", ["eve", "ivan", "vera"])
def test_reassign_to_an_ineligible_account_is_refused(world, target) -> None:
    """Another company, a disabled account, or a viewer cannot run a room."""
    w = world
    room = _room(w)
    response = w.client("dana").post(
        f"/api/rooms/{room.id}/reassign", json={"userId": getattr(w.ids, target)}
    )
    assert response.status_code == 422


def test_reassign_needs_an_admin(world) -> None:
    w = world
    room = _room(w)
    response = w.client("alice").post(f"/api/rooms/{room.id}/reassign", json={"userId": w.ids.bob})
    assert response.status_code == 403


# ─── editions ────────────────────────────────────────────────────────────


def test_editions_are_camel_case_and_hide_other_users_previews(world) -> None:
    w = world
    room = _room(w)
    published = _edition(w, room)
    preview = _edition(w, room, preview=True, requested_by=w.ids.alice)

    bob = w.client("bob").get(f"/api/rooms/{room.id}/editions").json()["editions"]
    alice = w.client("alice").get(f"/api/rooms/{room.id}/editions").json()["editions"]

    assert [e["id"] for e in bob] == [published.id]
    assert [e["id"] for e in alice] == [preview.id, published.id]
    edition = bob[0]
    assert set(edition) == {
        "id",
        "seq",
        "kind",
        "status",
        "preview",
        "periodStart",
        "periodEnd",
        "grain",
        "revision",
        "publishedSeq",
        "title",
        "summary",
        "blocks",
        "suggestions",
        "provenance",
        "createdAt",
    }
    assert edition["periodStart"] == "2026-11-09"
    assert edition["publishedSeq"] == 1
    assert alice[0]["publishedSeq"] is None  # a preview never gets one
    assert edition["blocks"] == BLOCKS
    assert w.client("bob").get(f"/api/rooms/{room.id}/editions/{preview.id}").status_code == 404
    one = w.client("alice").get(f"/api/rooms/{room.id}/editions/{preview.id}")
    assert one.status_code == 200 and one.json()["preview"] is True


def test_an_edition_of_another_room_is_404(world) -> None:
    w = world
    room, other = _room(w), _room(w, title="Other")
    edition = _edition(w, other)
    assert w.client("alice").get(f"/api/rooms/{room.id}/editions/{edition.id}").status_code == 404


# ─── engine-backed: run and periods ──────────────────────────────────────


def test_run_proxies_to_the_engine_with_the_requester(world) -> None:
    w = world
    room = _room(w)

    response = w.client("bob").post(f"/api/rooms/{room.id}/run", json={"preview": True})

    assert response.status_code == 202
    assert response.json() == {"editionId": "e-1", "status": "building"}
    method, path = w.engine.call_args.args
    kwargs = w.engine.call_args.kwargs
    assert (method, path) == ("POST", f"/api/rooms/{room.id}/run")
    assert kwargs["tenant"] == w.db.tenant
    assert kwargs["json"]["preview"] is True
    assert kwargs["json"]["requested_by"] == w.ids.bob
    assert _audits(w, "room.run")[0]["actor"] == w.ids.bob


def test_periods_proxies_to_the_engine(world) -> None:
    w = world
    room = _room(w)

    response = w.client("bob").post(
        f"/api/rooms/{room.id}/periods", json={"grain": "week", "anchor": "2026-11-10"}
    )

    assert response.status_code == 202
    method, path = w.engine.call_args.args
    assert (method, path) == ("POST", f"/api/rooms/{room.id}/periods")
    assert w.engine.call_args.kwargs["json"] == {
        "grain": "week",
        "anchor": "2026-11-10",
        "requested_by": w.ids.bob,
    }


def test_periods_needs_a_grain_and_an_anchor(world) -> None:
    w = world
    room = _room(w)
    response = w.client("bob").post(f"/api/rooms/{room.id}/periods", json={"grain": "week"})
    assert response.status_code == 422
    w.engine.assert_not_called()


def test_engine_errors_pass_through(world) -> None:
    w = world
    room = _room(w)
    w.engine.return_value = (502, {"error": "engine unavailable"})
    response = w.client("bob").post(f"/api/rooms/{room.id}/run", json={})
    assert response.status_code == 502
    assert response.json() == {"error": "engine unavailable"}


@pytest.mark.parametrize("route", ["run", "periods"])
def test_engine_routes_check_access_before_calling_the_engine(world, route) -> None:
    w = world
    room = _room(w)
    body = {"grain": "week", "anchor": "2026-11-10"}
    assert w.client("carol").post(f"/api/rooms/{room.id}/{route}", json=body).status_code == 404
    assert w.client("vera").post(f"/api/rooms/{room.id}/{route}", json=body).status_code == 403
    w.engine.assert_not_called()


# ─── tasks ───────────────────────────────────────────────────────────────


def test_tasks_list_accept_and_dismiss(world) -> None:
    from robothor.rooms import dal

    w = world
    room = _room(w)
    first, second = _task(w, "Chase invoice"), _task(w, "Book review")
    dal.link_task(w.db.tenant, room.id, first, origin="suggestion", suggestion_key="chase")
    dal.link_task(w.db.tenant, room.id, second)

    tasks = w.client("vera").get(f"/api/rooms/{room.id}/tasks").json()["tasks"]
    assert {t["taskId"] for t in tasks} == {first, second}
    row = next(t for t in tasks if t["taskId"] == first)
    assert row["origin"] == "suggestion" and row["suggestionKey"] == "chase"
    assert row["pending"] is True

    accepted = w.client("bob").post(f"/api/rooms/{room.id}/tasks/{first}/accept")
    dismissed = w.client("bob").post(f"/api/rooms/{room.id}/tasks/{second}/dismiss")

    assert accepted.status_code == 200 and accepted.json()["acceptedBy"] == w.ids.bob
    assert dismissed.status_code == 200 and dismissed.json()["dismissedBy"] == w.ids.bob
    assert _audits(w, "room.task_accept")[0]["actor"] == w.ids.bob
    assert _audits(w, "room.task_dismiss")[0]["actor"] == w.ids.bob
    again = w.client("alice").post(f"/api/rooms/{room.id}/tasks/{first}/accept")
    assert again.status_code == 409


def test_accepting_an_unlinked_task_is_404(world) -> None:
    w = world
    room = _room(w)
    task = _task(w)
    assert w.client("bob").post(f"/api/rooms/{room.id}/tasks/{task}/accept").status_code == 404


def test_open_a_task_as_a_room(world) -> None:
    w = world
    task = _task(w, "Call Bob back")

    first = w.client("alice").post(f"/api/rooms/from-task/{task}")
    again = w.client("alice").post(f"/api/rooms/from-task/{task}")

    assert first.status_code == 200, first.text
    assert first.json()["kind"] == "task"
    assert first.json()["title"] == "Call Bob back"
    assert again.json()["id"] == first.json()["id"]
    assert _audits(w, "room.open_task")[0]["actor"] == w.ids.alice


def test_a_colleague_opening_the_same_task_joins_its_room(world) -> None:
    w = world
    task = _task(w)
    room_id = w.client("alice").post(f"/api/rooms/from-task/{task}").json()["id"]

    response = w.client("carol").post(f"/api/rooms/from-task/{task}")

    assert response.status_code == 200
    assert response.json()["id"] == room_id
    assert response.json()["access"] == "shared"


def test_open_a_task_of_another_company_is_404(world) -> None:
    w = world
    w.db.cur.execute(
        "INSERT INTO crm_tasks (tenant_id, title) VALUES (%s, 'Theirs') RETURNING id",
        (w.db.other,),
    )
    task = str(w.db.cur.fetchone()[0])
    assert w.client("alice").post(f"/api/rooms/from-task/{task}").status_code == 404


def test_a_viewer_cannot_open_a_task_as_a_room(world) -> None:
    w = world
    task = _task(w)
    assert w.client("vera").post(f"/api/rooms/from-task/{task}").status_code == 403


# ─── the status-code matrix ──────────────────────────────────────────────

#: (method, path template, body, the action it needs). ``{room}`` is the room,
#: ``{task}`` a linked task, ``{edition}`` a published edition, ``{carol}`` an
#: account to share with, ``{bob}`` the shared member.
ROUTES: list[tuple[str, str, dict[str, Any] | None, str]] = [
    ("GET", "/api/rooms/{room}", None, "read"),
    ("PATCH", "/api/rooms/{room}", {"expected_rev": 1, "changes": {"title": "X"}}, "edit"),
    ("POST", "/api/rooms/{room}/archive", None, "edit"),
    ("POST", "/api/rooms/{room}/unarchive", None, "admin"),
    ("POST", "/api/rooms/{room}/duplicate", None, "work"),
    ("POST", "/api/rooms/{room}/read", None, "read"),
    ("GET", "/api/rooms/{room}/revisions", None, "read"),
    ("POST", "/api/rooms/{room}/revisions/1/restore", None, "edit"),
    ("GET", "/api/rooms/{room}/members", None, "read"),
    ("PUT", "/api/rooms/{room}/members/{carol}", {}, "edit"),
    ("DELETE", "/api/rooms/{room}/members/{bob}", None, "edit"),
    ("POST", "/api/rooms/{room}/reassign", {"userId": "{bob}"}, "admin"),
    ("GET", "/api/rooms/{room}/editions", None, "read"),
    ("GET", "/api/rooms/{room}/editions/{edition}", None, "read"),
    ("POST", "/api/rooms/{room}/run", {}, "work"),
    ("POST", "/api/rooms/{room}/periods", {"grain": "week", "anchor": "2026-11-10"}, "work"),
    ("GET", "/api/rooms/{room}/tasks", None, "read"),
    ("POST", "/api/rooms/{room}/tasks/{task}/accept", None, "work"),
    ("POST", "/api/rooms/{room}/tasks/{task}/dismiss", None, "work"),
]

#: What each persona's access level allows, by the class of route.
ALLOWED = {
    "alice": {"read", "work", "edit"},  # assigned
    "bob": {"read", "work"},  # shared
    "vera": {"read"},  # viewer
    "dana": {"read", "work", "edit", "admin"},  # tenant admin
    "carol": set(),  # same company, no access
    "eve": set(),  # another company's admin
}


def _fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


@pytest.mark.parametrize("persona", sorted(ALLOWED))
@pytest.mark.parametrize(("method", "template", "body", "needs"), ROUTES)
def test_status_code_matrix(world, persona, method, template, body, needs) -> None:
    from robothor.rooms import dal

    w = world
    room = _room(w)
    edition = _edition(w, room)
    task = _task(w)
    dal.link_task(w.db.tenant, room.id, task)
    if template.endswith("/unarchive"):
        dal.archive_room(w.db.tenant, room.id)
    values = {
        "room": room.id,
        "task": task,
        "edition": edition.id,
        "carol": w.ids.carol,
        "bob": w.ids.bob,
    }
    path = _fill(template, values)
    payload = (
        None
        if body is None
        else {k: _fill(v, values) if isinstance(v, str) else v for k, v in body.items()}
    )

    response = w.client(persona).request(method, path, json=payload)

    allowed = ALLOWED[persona]
    if needs in allowed:
        assert response.status_code < 300, (persona, method, path, response.text)
    elif not allowed:
        assert response.status_code == 404, (persona, method, path, response.text)
    else:
        assert response.status_code == 403, (persona, method, path, response.text)


@pytest.mark.parametrize(
    ("method", "template", "body", "needs"), [r for r in ROUTES if r[3] != "read"]
)
def test_a_viewer_changes_nothing(world, method, template, body, needs) -> None:
    """D5: every mutation by Vera is 403, and nothing was written or proxied."""
    from robothor.rooms import dal

    w = world
    room = _room(w)
    task = _task(w)
    dal.link_task(w.db.tenant, room.id, task)
    values = {"room": room.id, "task": task, "edition": "x", "carol": w.ids.carol, "bob": w.ids.bob}
    payload = (
        None
        if body is None
        else {k: _fill(v, values) if isinstance(v, str) else v for k, v in body.items()}
    )

    response = w.client("vera").request(method, _fill(template, values), json=payload)

    assert response.status_code == 403
    after = dal.get_room(w.db.tenant, room.id)
    assert after.rev == 1 and after.archived_at is None and after.title == "Weekly numbers"
    assert len(dal.list_members(w.db.tenant, room.id)) == 3
    assert all(t.pending for t in dal.list_room_tasks(w.db.tenant, room.id))
    w.engine.assert_not_called()
    assert not [a for a in w.audit if a["event_type"].startswith("room.")]


def test_a_service_token_has_no_room_access(world, monkeypatch) -> None:
    """A room's access flows from a human; an agent token gets nothing."""
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    w = world
    room = _room(w)
    monkeypatch.setattr("middleware.check_endpoint_access", lambda *a, **k: True)
    token = tokens.issue_service_token("email-classifier", w.db.tenant, agent_id="email-classifier")
    c = TestClient(app_())
    c.headers.update({"Authorization": f"Bearer {token}"})

    assert c.get(f"/api/rooms/{room.id}").status_code == 404
    assert c.get("/api/rooms").status_code == 403
    assert c.post("/api/rooms", json={"kind": "agent", "title": "X"}).status_code == 403
