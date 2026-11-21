"""Company boundaries hold at the database: row-level security on every room table.

As ``robothor_app`` with RLS enforcing, a process bound to one tenant cannot
read another tenant's rooms even when it names that tenant and guesses the
ids, and cannot write a row into it. Everything runs inside the ``db_conn``
transaction the integration fixtures roll back.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg2
import pytest

from robothor.rooms import dal
from robothor.rooms.access import room_session_key
from tests.rls_app_role import app_role, rls_tenants  # noqa: F401 - pytest fixtures

pytestmark = pytest.mark.integration

ROOM_TABLES = ("rooms", "room_members", "room_revisions", "room_editions", "room_tasks")


def _seed_room(cur: Any, tenant: str) -> dict[str, str]:
    room, user, edition, task = (str(uuid.uuid4()) for _ in range(4))
    cur.execute(
        "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by) "
        "VALUES (%s, %s, 'report', 'Secret numbers', 'main', %s, %s)",
        (tenant, room, room_session_key("main", room), user),
    )
    cur.execute(
        "INSERT INTO room_members (tenant_id, room_id, user_id, assigned) VALUES (%s, %s, %s, TRUE)",
        (tenant, room, user),
    )
    cur.execute(
        "INSERT INTO room_revisions (tenant_id, room_id, rev, changed_by, definition) "
        "VALUES (%s, %s, 1, %s, '{}')",
        (tenant, room, user),
    )
    cur.execute(
        "INSERT INTO room_editions (tenant_id, id, room_id, seq, kind, status, revision) "
        "VALUES (%s, %s, %s, 1, 'edition', 'published', 1)",
        (tenant, edition, room),
    )
    cur.execute(
        "INSERT INTO room_tasks (tenant_id, room_id, task_id, origin) "
        "VALUES (%s, %s, %s, 'linked')",
        (tenant, room, task),
    )
    return {"room": room, "user": user, "edition": edition, "task": task}


@pytest.mark.parametrize("table", ROOM_TABLES)
def test_every_room_table_forces_the_tenant_isolation_policy(db_conn, table) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = %s::regclass",
            (table,),
        )
        assert cur.fetchone() == (True, True)
        cur.execute(
            "SELECT COUNT(*) FROM pg_policies WHERE tablename = %s AND policyname = 'tenant_isolation'",
            (table,),
        )
        assert cur.fetchone()[0] == 1


def test_a_bound_process_sees_none_of_another_tenants_rooms(
    db_conn,
    app_role,  # noqa: F811
    rls_tenants,  # noqa: F811
) -> None:
    from robothor.constants import DEFAULT_TENANT
    from robothor.db.connection import get_connection

    a = rls_tenants["a"]
    with db_conn.cursor() as cur:
        theirs = _seed_room(cur, a)
        ours = _seed_room(cur, DEFAULT_TENANT)

    # Naming the other tenant and its ids explicitly still returns nothing.
    assert dal.get_room(a, theirs["room"]) is None
    assert dal.list_rooms_for_user(a, theirs["user"]) == []
    assert dal.list_members(a, theirs["room"]) == []
    assert dal.get_edition(a, theirs["edition"], viewer_id=None) is None
    assert dal.list_room_tasks(a, theirs["room"]) == []
    # And the process's own tenant still works.
    assert dal.get_room(DEFAULT_TENANT, ours["room"]) is not None

    with get_connection() as conn, conn.cursor() as cur:
        for table in ROOM_TABLES:
            # Deliberately no tenant predicate: only RLS limits this.
            cur.execute(f"SELECT DISTINCT tenant_id FROM {table}")  # noqa: S608
            assert a not in {r[0] for r in cur.fetchall()}, table


def test_a_bound_process_cannot_write_into_another_tenant(
    db_conn,
    app_role,  # noqa: F811
    rls_tenants,  # noqa: F811
) -> None:
    with pytest.raises(psycopg2.Error):
        dal.create_room(
            rls_tenants["a"],
            kind="agent",
            title="Planted",
            agent_id="main",
            created_by=str(uuid.uuid4()),
        )
