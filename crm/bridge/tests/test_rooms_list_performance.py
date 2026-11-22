"""Story I6: ``GET /api/rooms`` for 200 workspaces stays within its query bound.

Through the bridge's real app and auth, against real Postgres: Bob's
``scope=mine`` list of 200 workspaces is one rooms statement and Dana's
(company admin) ``scope=all`` is three (rooms, members, accounts), the same
count at 20 workspaces as at 200, each response well under two seconds.

The statements counted are the rooms data layer's (``robothor.rooms.dal``),
which is what the spec's bound is about; the request's own sign-in work runs
through other modules and is not counted.
"""

from __future__ import annotations

import contextlib
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.rooms.tests.conftest import (  # noqa: E402, F401 - pytest collects the fixture
    rooms_db,
)
from robothor.rooms.tests.perf import counting_connection, seed_rooms  # noqa: E402

pytestmark = pytest.mark.integration

LIST_BUDGET_S = 2.0


@pytest.fixture
def people(rooms_db, _controls_auth_key) -> Any:  # noqa: F811
    """Alice (member, assigned everywhere), Bob (member, shared), Dana (admin)."""
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    db = rooms_db
    dana = str(uuid.uuid4())
    accounts = {"alice": (db.alice, "member"), "bob": (db.bob, "member"), "dana": (dana, "admin")}
    for name, (uid, role) in accounts.items():
        db.cur.execute(
            "INSERT INTO user_accounts (id, tenant_id, email, display_name, role, status) "
            "VALUES (%s, %s, %s, %s, %s, 'active')",
            (uid, db.tenant, f"{name}@example.com", name.capitalize(), role),
        )

    def client(name: str) -> Any:
        uid, role = accounts[name]
        c = TestClient(app)
        c.headers.update(
            {"Authorization": f"Bearer {tokens.issue_access_token(uid, db.tenant, role)}"}
        )
        return c

    try:
        yield SimpleNamespace(db=db, client=client)
    finally:
        with contextlib.suppress(Exception):
            db.cur.execute("DELETE FROM user_accounts WHERE tenant_id = %s", (db.tenant,))


def _get(client: Any, url: str, monkeypatch: Any) -> tuple[dict[str, Any], int, float]:
    """The response body, the rooms statements it took, and its seconds."""
    log: list[str] = []
    with monkeypatch.context() as patched:
        patched.setattr("robothor.rooms.dal.get_connection", counting_connection(log))
        started = time.perf_counter()
        response = client.get(url)
        elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    body = response.json()
    print(
        f"I6 GET {url}: {len(body['rooms'])} rooms, {len(log)} statements, {elapsed * 1000:.1f} ms"
    )
    return body, len(log), elapsed


def test_listing_200_workspaces_is_one_query_for_mine_and_three_for_all(
    people, monkeypatch
) -> None:
    db = people.db
    bob, dana = people.client("bob"), people.client("dana")
    small = seed_rooms(db.cur, db.tenant, owner=db.alice, reader=db.bob, start=0, count=20)
    _, mine_20, _ = _get(bob, "/api/rooms", monkeypatch)
    _, all_20, _ = _get(dana, "/api/rooms?scope=all", monkeypatch)

    seeded = small + seed_rooms(
        db.cur, db.tenant, owner=db.alice, reader=db.bob, start=20, count=180
    )
    mine, mine_200, mine_s = _get(bob, "/api/rooms", monkeypatch)
    every, all_200, all_s = _get(dana, "/api/rooms?scope=all", monkeypatch)

    assert [r["id"] for r in mine["rooms"]] == [s.room_id for s in seeded]
    assert {r["id"]: r["unread"] for r in mine["rooms"]} == {s.room_id: s.unread for s in seeded}
    assert all(r["latestEdition"]["publishedSeq"] == 1 for r in mine["rooms"])
    assert len(every["rooms"]) == 200
    for row in every["rooms"]:
        assert row["assigned"] == [{"userId": db.alice, "displayName": "Alice"}]
        assert [(m["displayName"], m["assigned"]) for m in row["members"]] == [
            ("Alice", True),
            ("Bob", False),
        ]
    assert mine_20 == mine_200 == 1
    assert all_20 == all_200 == 3
    assert mine_s < LIST_BUDGET_S, f"scope=mine took {mine_s:.3f}s"
    assert all_s < LIST_BUDGET_S, f"scope=all took {all_s:.3f}s"
