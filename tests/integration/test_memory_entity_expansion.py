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

This test seeds real entities, a real relation, and real facts, and asserts an
``entity_expansion`` row comes back. It is red until the key is fixed, and it
cannot be satisfied by a mock.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

_CONSTANT_VECTOR = [0.1] * 1024


@pytest.fixture
def _no_ollama(monkeypatch):
    """Stub the embedding service only.

    Embeddings are an external dependency, not the control under test — a
    constant vector makes every row tie on the vector leg so BM25 over the
    generated ``tsv`` column does the retrieving, and CI needs no Ollama.
    Stubbing ``get_entity`` instead would be stubbing the thing being tested,
    which is exactly how this bug survived.
    """
    from robothor.llm import ollama as llm_client

    async def _fake_embedding(_text: str):
        return list(_CONSTANT_VECTOR)

    monkeypatch.setattr(llm_client, "get_embedding_async", _fake_embedding)


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


@pytest.mark.asyncio
async def test_expansion_returns_a_related_fact(graph_fixture, mock_get_connection, _no_ollama):
    """The whole point: a fact reachable only through the graph comes back.

    RED before the key fix — related_name is empty, so the expansion query
    never executes and only the directly-matching seed fact is returned.
    """
    from robothor.memory.facts import search_facts

    results = await search_facts(
        graph_fixture["query"],
        limit=10,
        tenant_id=graph_fixture["tenant"],
        expand_entities=True,
        use_reranker=False,
    )

    sources = {r.get("source") for r in results}
    ids = {r.get("id") for r in results}

    assert "entity_expansion" in sources, (
        f"entity expansion never ran — sources were {sources}. "
        "38,300 memory_relations rows are unreachable."
    )
    assert graph_fixture["related_id"] in ids, (
        "the related fact was not pulled in despite an entity_expansion source"
    )


@pytest.mark.asyncio
async def test_expansion_is_off_by_default(graph_fixture, mock_get_connection, _no_ollama):
    """Without expand_entities the related fact must not appear.

    Negative control. Without it, a test that returns everything would pass the
    assertion above for the wrong reason.
    """
    from robothor.memory.facts import search_facts

    results = await search_facts(
        graph_fixture["query"],
        limit=10,
        tenant_id=graph_fixture["tenant"],
        expand_entities=False,
        use_reranker=False,
    )

    assert "entity_expansion" not in {r.get("source") for r in results}


@pytest.mark.asyncio
async def test_expansion_respects_person_scope(db_cursor, test_prefix, mock_get_connection, _no_ollama):
    """A restricted caller must not reach another person's row *through the graph*.

    The expansion query carries the same scope predicate as the main candidate
    queries, but that code had never executed, so the predicate was untested by
    construction. Two mock-based tests in robothor/memory/tests/ claimed to
    cover this by patching get_entity to return {"relations": [{"target": ...}]}
    — a shape production never emits — and were deleted in favour of this.

    Seeds the related fact as Bob's. Alice must see the graph work for her own
    rows and still not receive Bob's.
    """
    import uuid

    from robothor.engine.tools.handlers.memory import _search_memory
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.identity.context import IdentityContext
    from robothor.memory.facts import search_facts
    from robothor.identity.scope import DataScope

    tenant = f"{test_prefix}-scoped"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (tenant, tenant),
    )
    people = {}
    for who in ("alice", "bob"):
        pid = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name, last_name, tenant_id) VALUES (%s,%s,%s,%s)",
            (pid, test_prefix, who, tenant),
        )
        people[who] = pid

    hub, spoke = f"{test_prefix}_Hub2", f"{test_prefix}_Spoke2"
    ids = {}
    for name in (hub, spoke):
        db_cursor.execute(
            "INSERT INTO memory_entities (name, entity_type, tenant_id) "
            "VALUES (%s,'organization',%s) RETURNING id",
            (name, tenant),
        )
        ids[name] = db_cursor.fetchone()["id"]
    db_cursor.execute(
        "INSERT INTO memory_relations (source_entity_id,target_entity_id,relation_type,tenant_id) "
        "VALUES (%s,%s,'partners_with',%s)",
        (ids[hub], ids[spoke], tenant),
    )

    db_cursor.execute(
        "INSERT INTO memory_facts (fact_text, category, entities, tenant_id, is_active, "
        "importance_score, embedding) VALUES (%s,'project',%s,%s,TRUE,0.6,%s::vector) RETURNING id",
        (f"{hub} signed the quarterly logistics agreement", [hub], tenant, str(_CONSTANT_VECTOR)),
    )
    seed_id = db_cursor.fetchone()["id"]

    # Reachable only via the relation, and owned by Bob.
    db_cursor.execute(
        "INSERT INTO memory_facts (fact_text, category, entities, tenant_id, is_active, "
        "importance_score, person_id) VALUES (%s,'project',%s,%s,TRUE,0.9,%s) RETURNING id",
        (f"{spoke} operates the northern depot", [spoke], tenant, people["bob"]),
    )
    bob_related = db_cursor.fetchone()["id"]

    query = f"{hub} quarterly logistics agreement"

    # Negative control: unrestricted, the graph reaches Bob's row.
    unrestricted = await search_facts(
        query, limit=10, tenant_id=tenant, expand_entities=True, use_reranker=False
    )
    assert bob_related in {r.get("id") for r in unrestricted}, (
        "expansion did not reach the related fact even unrestricted — the "
        "scoped assertion below would pass vacuously"
    )

    # Restricted Alice must not receive it.
    scoped = await search_facts(
        query,
        limit=10,
        tenant_id=tenant,
        expand_entities=True,
        use_reranker=False,
        scope=DataScope(tenant_id=tenant, person_id=people["alice"], restricted=True),
    )
    got = {r.get("id") for r in scoped}
    assert bob_related not in got, (
        "entity expansion leaked another person's row past the scope predicate"
    )
    assert seed_id in got, "the caller's own reachable row must still be returned"
