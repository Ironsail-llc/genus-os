"""The room tables follow the platform's RLS policy, including its unbound branch.

Every tenant-keyed table's ``tenant_isolation`` policy admits a connection
that binds no tenant (``app.tenant_id`` unset or empty): the migration runner,
an RLS-off deployment, and the two operator widenings
(``read_every_tenant_in_transaction``) the workload and account moves run
under. A room table that refused them would hide every room from those paths:
rooms orphaned by a move, memberships never removed, room sessions swept as
idle, and an RLS-off install unable to read or write a room at all.

These run as ``robothor_app`` (not a superuser, which bypasses RLS), inside the
``db_conn`` transaction the integration fixtures roll back.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from robothor.auth import accounts
from robothor.engine import chat_store
from robothor.rooms import dal
from robothor.rooms.access import room_session_key
from robothor.tenancy import move_workload as mw
from tests.rls_app_role import app_role, rls_tenants  # noqa: F401 - pytest fixtures

pytestmark = pytest.mark.integration


def _seed_room(cur: Any, tenant: str, agent: str, *members: str) -> str:
    """As the superuser: a room with members, a revision, an edition and its
    conversation (one idle message)."""
    room = str(uuid.uuid4())
    key = room_session_key(agent, room)
    creator = members[0] if members else str(uuid.uuid4())
    cur.execute(
        "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by) "
        "VALUES (%s, %s, 'report', 'Weekly', %s, %s, %s)",
        (tenant, room, agent, key, creator),
    )
    for user in members or (creator,):
        cur.execute(
            "INSERT INTO room_members (tenant_id, room_id, user_id, assigned) "
            "VALUES (%s, %s, %s, TRUE)",
            (tenant, room, user),
        )
    cur.execute(
        "INSERT INTO room_revisions (tenant_id, room_id, rev, changed_by, definition) "
        "VALUES (%s, %s, 1, %s, '{}')",
        (tenant, room, creator),
    )
    cur.execute(
        "INSERT INTO room_editions (tenant_id, id, room_id, seq, kind, status, revision) "
        "VALUES (%s, %s, %s, 1, 'edition', 'published', 1)",
        (tenant, str(uuid.uuid4()), room),
    )
    cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, last_active_at) "
        "VALUES (%s, %s, NOW() - interval '90 days') RETURNING id",
        (tenant, key),
    )
    return room


def _tenants_of(db_conn: Any, table: str, column: str, value: str) -> list[str]:
    with db_conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute(
            f"SELECT tenant_id FROM {table} WHERE {column}::text = %s ORDER BY 1",  # noqa: S608
            (value,),
        )
        return [r[0] for r in cur.fetchall()]


# ─── workload move ───────────────────────────────────────────────────────


def test_a_workload_move_carries_its_rooms_as_the_app_role(
    db_conn,
    app_role,  # noqa: F811
    rls_tenants,  # noqa: F811
) -> None:
    a, b = rls_tenants["a"], rls_tenants["b"]
    agent = f"tri_{uuid.uuid4().hex[:8]}"
    with db_conn.cursor() as cur:
        room = _seed_room(cur, a, agent)

    report = mw.move_workload(
        from_tenant=a, to_tenant=b, agents=[agent], workflows=[], actor="cli:test"
    )

    for table in ("rooms", "room_members", "room_revisions", "room_editions"):
        assert report.counts[table] == 1, table
    assert _tenants_of(db_conn, "rooms", "id", room) == [b]
    assert _tenants_of(db_conn, "room_members", "room_id", room) == [b]


def test_a_rooms_linked_task_refuses_the_move_as_the_app_role(
    db_conn,
    app_role,  # noqa: F811
    rls_tenants,  # noqa: F811
) -> None:
    a, b = rls_tenants["a"], rls_tenants["b"]
    agent = f"tri_{uuid.uuid4().hex[:8]}"
    with db_conn.cursor() as cur:
        room = _seed_room(cur, a, agent)
        cur.execute(
            "INSERT INTO room_tasks (tenant_id, room_id, task_id, origin) "
            "VALUES (%s, %s, %s, 'linked')",
            (a, room, str(uuid.uuid4())),
        )

    with pytest.raises(mw.WorkloadMoveError, match="room_tasks"):
        mw.move_workload(from_tenant=a, to_tenant=b, agents=[agent], workflows=[], actor="cli:t")


# ─── account move ────────────────────────────────────────────────────────


def test_an_account_move_removes_its_memberships_as_the_app_role(
    db_conn,
    app_role,  # noqa: F811
    rls_tenants,  # noqa: F811
) -> None:
    a, b = rls_tenants["a"], rls_tenants["b"]
    bob = str(uuid.uuid4())
    email = f"alice-{uuid.uuid4().hex[:8]}@example.com"
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (tenant_id, email, display_name, role, status) "
            "VALUES (%s, %s, 'Alice', 'member', 'active') RETURNING id",
            (a, email),
        )
        alice = str(cur.fetchone()[0])
        room = _seed_room(cur, a, "main", alice, bob)

    dry = accounts.move_account(alice, b, actor="cli:test", dry_run=True)
    assert dry.room_memberships_removed == 1

    report = accounts.move_account(alice, b, actor="cli:test")

    assert report.room_memberships_removed == 1
    with db_conn.cursor() as cur:
        cur.execute("RESET ROLE")
        cur.execute("SELECT user_id::text FROM room_members WHERE room_id = %s", (room,))
        assert [r[0] for r in cur.fetchall()] == [bob]


# ─── retention ───────────────────────────────────────────────────────────


def test_cleanup_with_no_process_tenant_spares_room_sessions(
    db_conn,
    app_role,  # noqa: F811
    rls_tenants,  # noqa: F811
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROBOTHOR_DEFAULT_TENANT", "")
    a = rls_tenants["a"]
    with db_conn.cursor() as cur:
        room = _seed_room(cur, a, "main")
        ordinary = f"agent:main:user:{uuid.uuid4()}"
        cur.execute(
            "INSERT INTO chat_sessions (tenant_id, session_key, last_active_at) "
            "VALUES (%s, %s, NOW() - interval '90 days')",
            (a, ordinary),
        )

    chat_store.cleanup_stale_sessions(ttl_days=7)

    key = room_session_key("main", room)
    assert _tenants_of(db_conn, "chat_sessions", "session_key", key) == [a]
    assert _tenants_of(db_conn, "chat_sessions", "session_key", ordinary) == []


# ─── an RLS-off deployment connecting as the app role ────────────────────


def test_rooms_work_with_the_rls_flag_off(
    db_conn,
    app_role,  # noqa: F811
    monkeypatch,
) -> None:
    from robothor.constants import DEFAULT_TENANT

    monkeypatch.delenv("ROBOTHOR_RLS_ENABLED", raising=False)
    alice = str(uuid.uuid4())

    room = dal.create_room(
        DEFAULT_TENANT, kind="agent", title="Planning", agent_id="main", created_by=alice
    )

    assert dal.get_room(DEFAULT_TENANT, room.id) == room
    assert [s.id for s in dal.list_rooms_for_user(DEFAULT_TENANT, alice)] == [room.id]
    assert [m.user_id for m in dal.list_members(DEFAULT_TENANT, room.id)] == [alice]
