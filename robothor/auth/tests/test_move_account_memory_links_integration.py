"""``move_account`` and the memory graph's links onto the moved CRM contact.

A memory entity can name the CRM contact it is about (``crm_person_id``).
Memory stays with its tenant when an account moves, but the contact moves with
the account, so the link would point across tenants. Like
``contact_identifiers.memory_entity_id``, it is cleared instead — and, being a
derived pointer rather than CRM history, it does not refuse the move.

Runs against a scratch database migrated through the memory graph migration,
because the shared test database is migrated by CI, not by this suite.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from robothor.auth import accounts

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

MANIFEST = Path(__file__).resolve().parents[3] / "robothor" / "migrations" / "manifest.txt"
_SUFFIX = "_memory_graph_foundation.sql"
PARENT, CHILD = "acme-parent", "acme-child-a"


def _migration() -> str:
    found = [
        Path(line.strip()).stem
        for line in MANIFEST.read_text().splitlines()
        if line.strip().endswith(_SUFFIX)
    ]
    return found[0] if len(found) == 1 else f"missing{_SUFFIX[:-4]}"


@pytest.fixture
def world(scratch_db, monkeypatch) -> Iterator[dict[str, Any]]:
    """acme-parent (root, the shell's tenant) with alice@example.com, her CRM
    contact, and two memory entities: one linked to her contact, one not;
    acme-child-a beneath it."""
    import psycopg2

    db, dsn = scratch_db(through=_migration())
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", PARENT)

    @contextmanager
    def _conn(*_a: Any, **_k: Any) -> Iterator[Any]:
        conn = psycopg2.connect(dsn)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(accounts, "get_connection", _conn)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Acme Parent')", (PARENT,)
        )
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name, parent_tenant_id) VALUES (%s, 'Child', %s)",
            (CHILD, PARENT),
        )
        cur.execute(
            "INSERT INTO crm_people (tenant_id, first_name, last_name, email) "
            "VALUES (%s, 'Alice', 'Example', 'alice@example.com') RETURNING id",
            (PARENT,),
        )
        person = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO user_accounts (tenant_id, email, display_name, role, status, person_id) "
            "VALUES (%s, 'alice@example.com', 'Alice', 'member', 'active', %s) RETURNING id",
            (PARENT, person),
        )
        user_id = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO memory_entities (name, entity_type, tenant_id, crm_person_id) "
            "VALUES ('Alice', 'person', %s, %s), ('Bob', 'person', %s, NULL) RETURNING id",
            (PARENT, person, PARENT),
        )
        alice_entity, bob_entity = (r[0] for r in cur.fetchall())
    yield {
        "db": db,
        "person": person,
        "user_id": user_id,
        "alice_entity": alice_entity,
        "bob_entity": bob_entity,
    }


def _entities(db: Any) -> dict[int, tuple[str, str | None]]:
    with db.cursor() as cur:
        cur.execute("SELECT id, tenant_id, crm_person_id::text FROM memory_entities")
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def test_the_link_does_not_refuse_the_move_and_is_cleared(world) -> None:
    report = accounts.move_account(world["user_id"], CHILD, actor="cli:test")

    with world["db"].cursor() as cur:
        cur.execute("SELECT tenant_id FROM crm_people WHERE id = %s", (world["person"],))
        assert cur.fetchone()[0] == CHILD  # the contact moved with the account
    entities = _entities(world["db"])
    # The entity stays with the tenant's memory; its link no longer points across.
    assert entities[world["alice_entity"]] == (PARENT, None)
    assert entities[world["bob_entity"]] == (PARENT, None)
    assert report.links_cleared == {"memory_entities.crm_person_id": 1}


def test_a_dry_run_counts_the_link_and_clears_nothing(world) -> None:
    report = accounts.move_account(world["user_id"], CHILD, actor="cli:test", dry_run=True)

    assert report.links_cleared == {"memory_entities.crm_person_id": 1}
    assert _entities(world["db"])[world["alice_entity"]] == (PARENT, world["person"])


def test_detaching_the_person_keeps_the_link(world) -> None:
    """With --detach-person the contact stays in the source, so the link is still sound."""
    report = accounts.move_account(world["user_id"], CHILD, actor="cli:test", detach_person=True)

    assert _entities(world["db"])[world["alice_entity"]] == (PARENT, world["person"])
    assert report.links_cleared == {}
