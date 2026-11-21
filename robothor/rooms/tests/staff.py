"""People for the workspace definition and tool tests, against real Postgres.

Builds on ``rooms_db`` (two tenants, committed connections): each person is a
``user_accounts`` row in the first tenant, with one more in the other tenant,
and everything written for them is removed afterwards. Import the fixture
next to ``rooms_db`` in a test module:

    from robothor.rooms.tests.conftest import rooms_db  # noqa: F401
    from robothor.rooms.tests.staff import staff_db  # noqa: F401
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from robothor.rooms.tests.conftest import committed_connection

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import SimpleNamespace

#: The agents the tests treat as installed.
KNOWN_AGENTS = frozenset({"main", "analyst"})

#: name -> tenant role. Alice, Bob and Carol are ``rooms_db``'s own ids.
PEOPLE = {
    "alice": "member",
    "bob": "member",
    "carol": "member",
    "vera": "viewer",
    "dana": "admin",
    "owen": "owner",
}


@pytest.fixture
def staff_db(rooms_db: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from robothor.engine import user_cron
    from robothor.rooms import definition

    monkeypatch.setattr(definition, "get_connection", committed_connection)
    monkeypatch.setattr(user_cron, "get_connection", committed_connection)
    monkeypatch.setattr(definition, "agent_exists", lambda agent_id: agent_id in KNOWN_AGENTS)
    db = rooms_db
    cur = db.cur
    for name, role in PEOPLE.items():
        user_id = getattr(db, name, None) or str(uuid.uuid4())
        setattr(db, name, user_id)
        cur.execute(
            "INSERT INTO user_accounts (id, tenant_id, email, display_name, role) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, db.tenant, f"{name}@example.com", name.title(), role),
        )
    db.olga = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO user_accounts (id, tenant_id, email, display_name, role) "
        "VALUES (%s, %s, 'olga@example.com', 'Olga', 'member')",
        (db.olga, db.other),
    )
    try:
        yield db
    finally:
        tenants = [db.tenant, db.other]
        for table in ("user_cronjobs", "user_permissions", "role_permissions"):
            with contextlib.suppress(Exception):
                cur.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ANY(%s)",  # noqa: S608
                    (tenants,),
                )
        # Rooms reference nobody by foreign key, but the tenants' rows go
        # before ``rooms_db`` removes the tenants themselves.
        for table in ("room_tasks", "room_editions", "room_revisions", "room_members", "rooms"):
            with contextlib.suppress(Exception):
                cur.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ANY(%s)",  # noqa: S608
                    (tenants,),
                )
        cur.execute("DELETE FROM user_accounts WHERE tenant_id = ANY(%s)", (tenants,))
