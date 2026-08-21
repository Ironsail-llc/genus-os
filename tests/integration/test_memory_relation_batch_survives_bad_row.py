"""One bad relation row must not cost the batch — real Postgres, real FKs.

``memory_relations`` carries foreign keys to ``memory_entities(id)``, and
``add_relations_batch`` used to push every row through a single
``execute_values`` statement. Postgres rejects that statement as a whole, so a
single unusable endpoint (a junk entity that was never stored, a stale id)
destroyed every relation in the batch — and ``ingestion.py`` swallowed the
ForeignKeyViolation with a ``logger.warning``, so the loss was silent.

A mocked cursor cannot prove the fix: the control being tested IS the database's
constraint behaviour. This test seeds real entities, hands the batch one id that
does not exist, and asserts the valid relations are on disk afterwards.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def entity_fixture(db_cursor, test_prefix):
    """A tenant and three real entities to hang relations off."""
    tenant = f"{test_prefix}-tenant"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (tenant, tenant),
    )

    ids = {}
    for label in ("alpha", "beta", "gamma"):
        name = f"{test_prefix}_{label}"
        db_cursor.execute(
            """
            INSERT INTO memory_entities (name, entity_type, tenant_id)
            VALUES (%s, 'organization', %s)
            RETURNING id
            """,
            (name, tenant),
        )
        ids[label] = db_cursor.fetchone()["id"]

    db_cursor.execute("SELECT COALESCE(MAX(id), 0) + 1000000 AS missing FROM memory_entities")
    missing_id = db_cursor.fetchone()["missing"]
    return tenant, ids, missing_id


async def test_batch_survives_a_row_pointing_at_a_missing_entity(
    db_cursor, mock_get_connection, entity_fixture
):
    from robothor.memory.entities import add_relations_batch

    tenant, ids, missing_id = entity_fixture
    rows = [
        (ids["alpha"], ids["beta"], "works_at", None, 1.0),
        (ids["alpha"], missing_id, "uses", None, 1.0),  # FK violation
        (ids["beta"], ids["gamma"], "manages", None, 0.9),
    ]

    stored = await add_relations_batch(rows, tenant_id=tenant)

    assert stored == 2
    db_cursor.execute(
        """
        SELECT source_entity_id, target_entity_id, relation_type
        FROM memory_relations WHERE tenant_id = %s ORDER BY relation_type
        """,
        (tenant,),
    )
    persisted = [
        (r["source_entity_id"], r["target_entity_id"], r["relation_type"])
        for r in db_cursor.fetchall()
    ]
    assert persisted == [
        (ids["beta"], ids["gamma"], "manages"),
        (ids["alpha"], ids["beta"], "works_at"),
    ]


async def test_batch_survives_a_duplicate_pair(db_cursor, mock_get_connection, entity_fixture):
    """The same relation proposed twice in one batch used to kill the batch.

    ``ON CONFLICT DO UPDATE`` cannot touch a row the same command inserted, so
    Postgres raises ``CardinalityViolation: ON CONFLICT DO UPDATE command cannot
    affect row a second time`` for the whole statement. LLM extraction emits a
    duplicate pair routinely — the same relation mentioned by two facts in one
    batch — so this cost relations even when every entity id was valid.
    """
    from robothor.memory.entities import add_relations_batch

    tenant, ids, _missing = entity_fixture
    rows = [
        (ids["alpha"], ids["beta"], "uses", None, 0.5),
        (ids["alpha"], ids["beta"], "uses", None, 0.9),  # same constrained key
        (ids["beta"], ids["gamma"], "manages", None, 1.0),
    ]

    stored = await add_relations_batch(rows, tenant_id=tenant)

    assert stored == 3
    db_cursor.execute(
        """
        SELECT relation_type, confidence FROM memory_relations
        WHERE tenant_id = %s ORDER BY relation_type
        """,
        (tenant,),
    )
    persisted = [(r["relation_type"], r["confidence"]) for r in db_cursor.fetchall()]
    assert persisted == [("manages", 1.0), ("uses", 0.9)]


async def test_batch_drops_none_ids_before_touching_postgres(
    db_cursor, mock_get_connection, entity_fixture
):
    """A ``None`` endpoint (what upsert_entity returns for a junk name) is
    dropped in Python, and the surviving relations still land."""
    from robothor.memory.entities import add_relations_batch

    tenant, ids, _missing = entity_fixture
    rows = [
        (ids["alpha"], None, "uses", None, 1.0),
        (None, ids["beta"], "uses", None, 1.0),
        (ids["alpha"], ids["gamma"], "knows", None, 1.0),
    ]

    stored = await add_relations_batch(rows, tenant_id=tenant)

    assert stored == 1
    db_cursor.execute(
        "SELECT relation_type FROM memory_relations WHERE tenant_id = %s",
        (tenant,),
    )
    assert [r["relation_type"] for r in db_cursor.fetchall()] == ["knows"]
