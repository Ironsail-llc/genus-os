"""A name that exists several times must resolve to the real node.

`memory_entities` is unique on `(tenant_id, name, entity_type)`, so one name can
legitimately exist as several rows. `get_entity` looked it up with

    SELECT * FROM memory_entities WHERE lower(name) = lower(%s) AND tenant_id = %s

and took `fetchone()` — no ORDER BY, no entity_type. Postgres is free to return
any matching row, and on this box it returns the wrong one.

Measured 2026-08-22 on production. The operator's own name is split four ways:

    id    95  person        5207 relations   <- the real identity graph
    id  3072  event            1 relation    <- what get_entity actually returns
    id  8235  person (lower)   1 relation
    id 51819  organization     0 relations

So every agent that asked about the operator received an event node with one
edge. 194 of 687 recorded get_entity calls returned found:false, and the ones
that "succeeded" could be returning a stub like this.

Degree is the tie-break: among rows sharing a name, the one carrying the
relationships is the one the caller means.

These tests run against a real database. The defect is in what SQL returns when
several rows match, which a mocked cursor cannot express — it returns whatever
the test author decided to hand back, which is the bug in miniature.
"""

from __future__ import annotations

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from robothor.memory import entities  # noqa: E402

pytestmark = pytest.mark.integration

_NAME = "Zzz Resolution Probe"


@pytest.fixture
def split_entity():
    """The same name as three rows of different types and degrees."""
    from robothor.db.connection import get_connection

    created: list[int] = []
    with get_connection() as conn, conn.cursor() as cur:
        for entity_type in ("event", "person", "organization"):
            cur.execute(
                """
                INSERT INTO memory_entities (name, entity_type, tenant_id)
                VALUES (%s, %s, %s) RETURNING id
                """,
                (_NAME, entity_type, entities.DEFAULT_TENANT),
            )
            created.append(cur.fetchone()[0])
        event_id, person_id, _org_id = created

        # Give the PERSON row the relationships; leave the event row with one.
        cur.execute(
            "INSERT INTO memory_entities (name, entity_type, tenant_id) VALUES (%s,%s,%s) RETURNING id",
            ("Zzz Probe Peer", "person", entities.DEFAULT_TENANT),
        )
        peer = cur.fetchone()[0]
        created.append(peer)
        for i in range(5):
            cur.execute(
                """
                INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, tenant_id)
                VALUES (%s, %s, %s, %s)
                """,
                (person_id, peer, f"probe_rel_{i}", entities.DEFAULT_TENANT),
            )
        cur.execute(
            """
            INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, tenant_id)
            VALUES (%s, %s, %s, %s)
            """,
            (event_id, peer, "probe_rel_event", entities.DEFAULT_TENANT),
        )
        conn.commit()

    yield {"person": person_id, "event": event_id}

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM memory_relations WHERE relation_type LIKE 'probe_rel%%' AND tenant_id = %s",
            (entities.DEFAULT_TENANT,),
        )
        cur.execute("DELETE FROM memory_entities WHERE id = ANY(%s)", (created,))
        conn.commit()


@pytest.mark.asyncio
async def test_resolves_to_the_node_carrying_the_relationships(split_entity):
    """The defect: this returned the 1-relation event node."""
    result = await entities.get_entity(_NAME)
    assert result is not None, "a name that exists did not resolve at all"
    assert result.get("id") == split_entity["person"], (
        f"resolved to entity {result.get('id')} ({result.get('entity_type')}) instead of "
        f"the {split_entity['person']} node that holds the relationships"
    )


@pytest.mark.asyncio
async def test_the_relation_payload_is_bounded(split_entity):
    """The operator's real node has 5,207 edges; an unbounded payload blows context."""
    result = await entities.get_entity(_NAME)
    assert result is not None
    relations = result.get("relations") or []
    assert len(relations) <= entities.MAX_RELATIONS_RETURNED


@pytest.mark.asyncio
async def test_a_unique_name_is_unaffected(split_entity):
    """Only ambiguous names change behaviour."""
    result = await entities.get_entity("Zzz Probe Peer")
    assert result is not None
    assert result.get("entity_type") == "person"


@pytest.mark.asyncio
async def test_a_missing_name_still_returns_none():
    assert await entities.get_entity("Zzz Definitely Not A Real Entity Name") is None
