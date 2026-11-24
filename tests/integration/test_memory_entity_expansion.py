"""Entity-graph expansion actually runs — real DB, real get_entity.

``search_facts(expand_entities=True)`` is supposed to widen a result set by
following the entity graph: take the entities mentioned by the top candidates,
look up their relations, and pull in high-importance facts about the related
entity. There are 38,300 rows in ``memory_relations`` to walk.

It has never walked one. ``facts.py`` reads ``rel["target"]`` / ``rel["source"]``
while ``entities.get_entity`` builds relations with ``SELECT r.*, e.name as
target_name`` — so the key it reads does not exist on any real row,
``related_name`` is always empty, and the expansion query is unreachable. The
whole block is wrapped in ``except Exception: pass``, so it could never
complain either.

The tests that covered this fabricated ``{"relations": [{"target": "Bob"}]}``
— a shape production never emits — and then asserted the expansion ran. They
passed for as long as the feature was broken.

That leg is now superseded by graph activation (``robothor.memory.activation``,
the ``graph`` leg of ``search_facts``); its search tests moved with it (see the
note at the end). What stays here pins the relation shape ``get_entity`` emits,
against real rows.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

_CONSTANT_VECTOR = [0.1] * 1024


@pytest.fixture
def graph_fixture(db_cursor, test_prefix):
    """Two entities joined by a relation, plus a fact about each.

    The seed fact is what a query will match directly. The related fact is
    reachable *only* by following the relation, and carries importance above
    the 0.5 floor the expansion query requires.
    """
    tenant = f"{test_prefix}-tenant"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (tenant, tenant),
    )

    hub = f"{test_prefix}_Hubcorp"
    spoke = f"{test_prefix}_Spokeworks"

    ids = {}
    for name in (hub, spoke):
        db_cursor.execute(
            "INSERT INTO memory_entities (name, entity_type, tenant_id) "
            "VALUES (%s, 'organization', %s) RETURNING id",
            (name, tenant),
        )
        ids[name] = db_cursor.fetchone()["id"]

    db_cursor.execute(
        "INSERT INTO memory_relations "
        "(source_entity_id, target_entity_id, relation_type, tenant_id) "
        "VALUES (%s, %s, 'partners_with', %s)",
        (ids[hub], ids[spoke], tenant),
    )

    # Directly retrievable: shares vocabulary with the query and mentions the hub.
    db_cursor.execute(
        "INSERT INTO memory_facts "
        "(fact_text, category, entities, tenant_id, is_active, importance_score, embedding) "
        "VALUES (%s, 'project', %s, %s, TRUE, 0.6, %s::vector) RETURNING id",
        (
            f"{hub} signed the quarterly logistics agreement",
            [hub],
            tenant,
            str(_CONSTANT_VECTOR),
        ),
    )
    seed_id = db_cursor.fetchone()["id"]

    # Reachable ONLY via the relation. Deliberately has no embedding, so the
    # vector leg cannot return it (that query requires embedding IS NOT NULL),
    # and no vocabulary shared with the query, so BM25 cannot either. If this
    # row appears in results, the graph is the only thing that could have put
    # it there — which is precisely what makes the assertion meaningful.
    db_cursor.execute(
        "INSERT INTO memory_facts "
        "(fact_text, category, entities, tenant_id, is_active, importance_score) "
        "VALUES (%s, 'project', %s, %s, TRUE, 0.9) RETURNING id",
        (f"{spoke} operates the northern depot", [spoke], tenant),
    )
    related_id = db_cursor.fetchone()["id"]

    return {
        "tenant": tenant,
        "hub": hub,
        "spoke": spoke,
        "seed_id": seed_id,
        "related_id": related_id,
        "query": f"{hub} quarterly logistics agreement",
    }


@pytest.mark.asyncio
async def test_get_entity_emits_name_keys_not_bare_target(graph_fixture, mock_get_connection):
    """Pin the actual shape get_entity produces.

    This is the contract facts.py must read against. Asserting it here means the
    next rename of these columns fails loudly instead of silently disabling
    expansion again.
    """
    from robothor.memory.entities import get_entity

    entity = await get_entity(graph_fixture["hub"], tenant_id=graph_fixture["tenant"])
    assert entity is not None
    relations = entity.get("relations") or []
    assert relations, "seeded relation not returned by get_entity"

    keys = set(relations[0])
    assert "target_name" in keys or "source_name" in keys
    assert "target" not in keys and "source" not in keys, (
        "get_entity emits *_name keys; a bare target/source key would mean the "
        "schema changed and facts.py should be updated to match"
    )


# The three tests that followed pinned the one-hop ``entity_expansion`` leg of
# ``search_facts``: that a related fact comes back, that it does not without
# ``expand_entities``, and that a restricted person scope holds through it.
# Graph activation (the ``graph`` leg) superseded that leg, and each test was
# ported to it, on a scratch database with the graph schema, in
# robothor/memory/tests/test_graph_recall_leg.py. "Returns a related fact" is
# now the two-hop reserved-place test, "off by default" the include_graph=False
# test, and "respects person scope" the restricted-scope test there.
