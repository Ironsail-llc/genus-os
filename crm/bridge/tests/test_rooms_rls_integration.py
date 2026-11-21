"""The workspaces API with row-level security enforcing, as the app role.

``test_rooms_router.py`` runs as a superuser, which bypasses RLS, so its
cross-company cases prove only the data layer's own tenant filters. These run
every connection as ``robothor_app`` (migration 082) bound by the REAL
``_apply_tenant_scope`` to the request's tenant, inside the ``db_conn``
transaction the integration fixtures roll back: a member reads their own room
and its member names (``user_accounts`` is under RLS too), and another
company's admin gets nothing.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.rooms.access import room_session_key  # noqa: E402
from tests.rls_app_role import app_role  # noqa: E402, F401 - pytest fixture

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded(db_conn, _controls_auth_key) -> Any:
    """Two companies; Alice (member) has a room in the first, Eve is the second's admin."""
    suffix = uuid.uuid4().hex[:8]
    first, second = f"test-tenant-{suffix}", f"test-tenant-other-{suffix}"
    alice, eve, room = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    with db_conn.cursor() as cur:
        for tenant in (first, second):
            cur.execute(
                "INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Test tenant')", (tenant,)
            )
        for uid, tenant, name, role in (
            (alice, first, "Alice", "member"),
            (eve, second, "Eve", "admin"),
        ):
            cur.execute(
                "INSERT INTO user_accounts (id, tenant_id, email, display_name, role) "
                "VALUES (%s, %s, %s, %s, %s)",
                (uid, tenant, f"{name.lower()}@example.com", name, role),
            )
        cur.execute(
            "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by, "
            "run_as_user_id) VALUES (%s, %s, 'report', 'Weekly', 'main', %s, %s, %s)",
            (first, room, room_session_key("main", room), alice, alice),
        )
        cur.execute(
            "INSERT INTO room_members (tenant_id, room_id, user_id, assigned) "
            "VALUES (%s, %s, %s, TRUE)",
            (first, room, alice),
        )
    return {"first": first, "second": second, "alice": alice, "eve": eve, "room": room}


def _client(user_id: str, tenant: str, role: str) -> Any:
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app)
    client.headers.update(
        {"Authorization": f"Bearer {tokens.issue_access_token(user_id, tenant, role)}"}
    )
    return client


def test_a_member_reads_their_room_under_rls(seeded, app_role) -> None:  # noqa: F811
    alice = _client(seeded["alice"], seeded["first"], "member")

    listed = alice.get("/api/rooms")
    room = alice.get(f"/api/rooms/{seeded['room']}")

    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()["rooms"]] == [seeded["room"]]
    assert room.status_code == 200
    assert room.json()["members"] == [
        {"userId": seeded["alice"], "displayName": "Alice", "assigned": True}
    ]
    assert room.json()["runsAs"] == {"userId": seeded["alice"], "displayName": "Alice"}


def test_another_companys_admin_gets_nothing_under_rls(seeded, app_role) -> None:  # noqa: F811
    eve = _client(seeded["eve"], seeded["second"], "admin")

    assert eve.get(f"/api/rooms/{seeded['room']}").status_code == 404
    assert eve.get("/api/rooms?scope=all").json() == {"rooms": []}
    assert eve.post(f"/api/rooms/{seeded['room']}/archive").status_code == 404
    share = eve.put(f"/api/rooms/{seeded['room']}/members/{seeded['eve']}", json={})
    assert share.status_code == 404


def test_the_directory_is_per_company_under_rls(seeded, app_role) -> None:  # noqa: F811
    alice = _client(seeded["alice"], seeded["first"], "member")
    eve = _client(seeded["eve"], seeded["second"], "admin")

    assert alice.get("/api/rooms/directory").json() == {
        "users": [{"userId": seeded["alice"], "displayName": "Alice"}]
    }
    assert eve.get("/api/rooms/directory").json() == {
        "users": [{"userId": seeded["eve"], "displayName": "Eve"}]
    }
