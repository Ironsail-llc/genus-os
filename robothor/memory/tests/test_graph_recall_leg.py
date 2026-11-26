"""The graph leg of ``search_facts``, end to end on real Postgres.

A scratch database with the full migration chain; entities, edges, evidence,
mentions and facts planted in SQL; the embedding service and the reranker's
per-pair scorer faked. What is asserted:

* a fact two hops from the query's entity comes back through the ``graph``
  leg and keeps a reserved place even when the reranker scores it 0;
* ``include_graph=False`` runs no graph leg;
* graph rows are labelled with their origin and the path that reached them;
* the reader's person id decides which private evidence the walk may cross,
  and a restricted person scope still holds through the graph;
* the search writes nothing (no access counts).

These replace the integration tests of the one-hop ``entity_expansion`` leg
(``tests/integration/test_memory_entity_expansion.py``), which this leg
supersedes.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from robothor.memory.tests.fake_embedding import embed_all_facts, fake_embed
from robothor.memory.tests.test_graph_activation import TENANT, Graph

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration


@pytest.fixture
def world(scratch_db, monkeypatch) -> Iterator[Graph]:
    import psycopg2

    import robothor.crm.dal
    import robothor.db

    # Recall reads every column the chain adds (utility, origin, scope), so
    # the database is migrated through the last manifest entry, not only
    # through the migration this leg introduced.
    from robothor.db.migrate import _discover
    from robothor.llm import ollama
    from robothor.memory import entities, facts
    from robothor.rag import reranker

    db, dsn = scratch_db(through=list(_discover())[-1].migration_id)

    @contextmanager
    def _conn(*_a: Any, **_k: Any) -> Iterator[Any]:
        conn = psycopg2.connect(dsn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    for module in (robothor.db, facts, entities, robothor.crm.dal):
        monkeypatch.setattr(module, "get_connection", _conn)
    monkeypatch.setattr(facts, "apply_hnsw_session", lambda _cur: None)

    async def _embed(text: str) -> list[float]:
        return fake_embed(text)

    monkeypatch.setattr(ollama, "get_embedding_async", _embed)
    monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
    monkeypatch.setenv("MEMORY_RANK_BLEND", "1")
    graph = Graph(db)
    real = facts.search_facts

    async def search_after_embedding(*a: Any, **k: Any) -> list[dict[str, Any]]:
        # Every planted fact gets its embedding (the fake model's) first.
        embed_all_facts(graph.sql)
        return await real(*a, **k)

    monkeypatch.setattr(facts, "search_facts", search_after_embedding)
    yield graph


def _scorer(zero: str):
    """The reranker scores every document 0.6 and any containing ``zero`` 0."""

    async def fake_pair(_client: Any, _query: str, doc: str) -> float:
        return 0.0 if zero in doc else 0.6

    return fake_pair


def _fillers(world: Graph, text: str, n: int = 40, **kw: Any) -> None:
    """``n`` shared facts closer to the query than anything planted: they fill
    the vector leg's 30 and the fused head, so a planted fact can reach the
    results only through the walk (or not at all)."""
    for i in range(n):
        world.fact(f"{text} filler {i}", **kw)


def _legs_of(rows: list[dict[str, Any]], fact_id: int) -> set[str]:
    """The legs that found ``fact_id`` in ``rows`` (empty if it is not there)."""
    return next((set(r["leg_ranks"]) for r in rows if r["id"] == fact_id), set())


def _chain(world: Graph, *, fillers: int = 40) -> dict[str, int]:
    a, b, c = (world.entity(n) for n in ("Alice Smith", "Bob Jones", "Carol White"))
    world.link(a, b)
    world.link(b, c)
    # Directly retrievable, and mentioning Alice.
    seed = world.fact("Alice Smith signed the quarterly logistics agreement", about=(a,))
    # Many dense hits that outrank it on every leg but the graph: more than the
    # vector leg fetches (30), so the two-hop fact is the graph's alone.
    for i in range(fillers):
        world.fact(f"quarterly logistics agreement filler {i}")
    # Reachable through two edges; not every word of the query (so the
    # keyword leg misses it) and further from it than the fillers (so the
    # vector leg does), but close in meaning to the question without its name.
    far = world.fact(
        "Carol White operates the northern depot under the logistics agreement",
        about=(c,),
        importance=0.9,
    )
    return {"a": a, "b": b, "c": c, "seed": seed, "far": far}


@pytest.mark.asyncio
async def test_a_two_hop_fact_keeps_a_reserved_place_when_the_reranker_discards_it(
    world: Graph, monkeypatch
) -> None:
    from robothor.memory.facts import search_facts
    from robothor.rag import reranker

    w = _chain(world)
    monkeypatch.setattr(reranker, "rerank_pair", _scorer("northern depot"))

    results = await search_facts(
        "Alice Smith quarterly logistics agreement", limit=5, tenant_id=TENANT, use_reranker=True
    )

    row = next((r for r in results if r["id"] == w["far"]), None)
    assert row is not None, f"the 2-hop fact was not recalled: {[r['id'] for r in results]}"
    assert "graph" in row["leg_ranks"]
    assert row["rerank_score"] in (None, 0.0)
    assert row["origin"] == "user_stated"
    assert row["graph_hops"] == 2
    assert row["graph_path"] == ["Alice Smith", "Bob Jones", "Carol White"]
    assert row["source"] == "graph"
    assert row["graph_relevance"] > 0


@pytest.mark.asyncio
async def test_include_graph_false_returns_no_graph_rows(world: Graph, monkeypatch) -> None:
    from robothor.memory.facts import search_facts
    from robothor.rag import reranker

    w = _chain(world)
    monkeypatch.setattr(reranker, "rerank_pair", _scorer("northern depot"))

    results = await search_facts(
        "Alice Smith quarterly logistics agreement",
        limit=5,
        tenant_id=TENANT,
        use_reranker=True,
        include_graph=False,
    )

    assert w["far"] not in {r["id"] for r in results}
    assert not any("graph" in r["leg_ranks"] for r in results)


@pytest.mark.asyncio
async def test_seeds_also_come_from_the_entities_the_top_hits_mention(
    world: Graph, monkeypatch
) -> None:
    """The query names no entity; the top keyword hit mentions Alice, and the
    graph starts from her."""
    from robothor.memory.facts import search_facts
    from robothor.rag import reranker

    w = _chain(world, fillers=0)
    monkeypatch.setattr(reranker, "rerank_pair", _scorer("northern depot"))

    results = await search_facts(
        "quarterly logistics agreement", limit=5, tenant_id=TENANT, use_reranker=True
    )

    assert w["far"] in {r["id"] for r in results}


@pytest.mark.asyncio
async def test_the_reader_decides_which_private_evidence_is_walked(
    world: Graph, monkeypatch
) -> None:
    from robothor.memory.facts import search_facts
    from robothor.rag import reranker

    bob_reader = str(uuid.uuid4())
    a, p = world.entity("Alice Smith"), world.entity("Pat Hidden")
    world.fact("Alice Smith signed the quarterly logistics agreement", about=(a,))
    secret = world.fact("private support", scope_id=bob_reader)
    world.edge(a, p, evidence=(secret,))
    about_p = world.fact("Pat Hidden keeps the logistics agreement on Elm Street", about=(p,))
    _fillers(world, "Alice Smith quarterly logistics agreement")
    monkeypatch.setattr(reranker, "rerank_pair", _scorer("Elm Street"))
    query = "Alice Smith quarterly logistics agreement"

    anonymous = await search_facts(query, limit=5, tenant_id=TENANT, use_reranker=True)
    bob = await search_facts(
        query, limit=5, tenant_id=TENANT, use_reranker=True, reader_scope_id=bob_reader
    )

    # What is asserted is whether the WALK reached it (the fillers keep it
    # out of the other legs and out of the top hits that seed the walk).
    assert anonymous, "negative control: the anonymous search answers"
    assert "graph" not in _legs_of(anonymous, about_p)
    assert "graph" in _legs_of(bob, about_p)


@pytest.mark.asyncio
async def test_a_restricted_scope_does_not_reach_another_persons_fact_through_the_graph(
    world: Graph, monkeypatch
) -> None:
    """Ported from the entity_expansion leg's person-scope test."""
    from robothor.identity.scope import DataScope
    from robothor.memory.facts import search_facts
    from robothor.rag import reranker

    alice_pid, bob_pid = world.person("Alice", "Smith"), world.person("Bob", "Jones")
    w = _chain(world, fillers=0)
    bobs = world.fact(
        "Carol White shares Bob's private depot code for the logistics agreement",
        about=(w["c"],),
    )
    world.sql("UPDATE memory_facts SET person_id = %s WHERE id = %s", (bob_pid, bobs))
    monkeypatch.setattr(reranker, "rerank_pair", _scorer("depot"))
    query = "Alice Smith quarterly logistics agreement"

    unrestricted = await search_facts(query, limit=10, tenant_id=TENANT, use_reranker=False)
    assert bobs in {r["id"] for r in unrestricted}, "negative control: the graph reaches it"

    scoped = await search_facts(
        query,
        limit=10,
        tenant_id=TENANT,
        use_reranker=False,
        scope=DataScope(tenant_id=TENANT, person_id=alice_pid, restricted=True),
    )
    got = {r["id"] for r in scoped}
    assert bobs not in got
    assert w["seed"] in got and w["far"] in got


@pytest.mark.asyncio
async def test_the_search_path_writes_nothing(world: Graph, monkeypatch) -> None:
    from robothor.memory.facts import search_facts
    from robothor.rag import reranker

    w = _chain(world)
    monkeypatch.setattr(reranker, "rerank_pair", _scorer("northern depot"))
    before = world.sql("SELECT id, access_count, last_accessed FROM memory_facts ORDER BY id")

    results = await search_facts(
        "Alice Smith quarterly logistics agreement", limit=5, tenant_id=TENANT, use_reranker=True
    )

    assert w["far"] in {r["id"] for r in results}
    assert world.sql("SELECT id, access_count, last_accessed FROM memory_facts ORDER BY id") == (
        before
    )


@pytest.mark.asyncio
async def test_a_top_hit_the_reader_cannot_see_does_not_seed_the_walk(
    world: Graph, monkeypatch
) -> None:
    """The keyword leg does not filter by scope, so it can return a fact
    private to someone else. Its mentions must not start a walk for a reader
    who cannot see it."""
    from robothor.memory.facts import search_facts
    from robothor.rag import reranker

    alice_reader, bob_reader = world.person("Alice", "Smith"), world.person("Bob", "Jones")
    p, q = world.entity("Pat Hidden"), world.entity("Quinn Far")
    world.fact("zephyr logistics memo from Bob", about=(p,), scope_id=bob_reader)
    # A shared hit on every word, so the keyword leg never falls back to any
    # word and finds the fact below directly; it mentions no one.
    world.fact("zephyr logistics memo template")
    world.link(p, q)
    # A note is a memo (the fake model's synonym), with no word of the query.
    about_q = world.fact("Quinn Far files each note at the depot", about=(q,))
    _fillers(world, "zephyr logistics memo")
    monkeypatch.setattr(reranker, "rerank_pair", _scorer("depot"))

    bob = await search_facts(
        "zephyr logistics memo", limit=5, tenant_id=TENANT, reader_scope_id=bob_reader
    )
    assert "graph" in _legs_of(bob, about_q), "negative control: its owner's walk reaches it"

    alice = await search_facts(
        "zephyr logistics memo", limit=5, tenant_id=TENANT, reader_scope_id=alice_reader
    )
    assert "graph" not in _legs_of(alice, about_q)


@pytest.mark.asyncio
async def test_the_one_hop_expansion_answers_on_data_without_mentions(
    world: Graph, monkeypatch
) -> None:
    """Existing data has entities by name and relations, but no mentions: the
    graph leg finds nothing and the one-hop expansion answers as before."""
    from robothor.memory.facts import search_facts
    from robothor.rag import reranker

    a, b = (
        world.entity("Hubcorp Example", entity_type="organization"),
        world.entity("Spokeworks Example"),
    )
    world.edge(a, b, legacy_fact=world.fact("support", origin=None))
    world.fact(
        "Hubcorp Example signed the quarterly logistics agreement",
        origin=None,
        entities=("Hubcorp Example",),
    )
    _fillers(
        world,
        "Hubcorp Example quarterly logistics agreement",
        origin=None,
        entities=("Hubcorp Example",),
    )
    related = world.fact(
        "Spokeworks Example operates the northern depot",
        origin=None,
        importance=0.9,
        entities=("Spokeworks Example",),
    )
    monkeypatch.setattr(reranker, "rerank_pair", _scorer("northern depot"))

    results = await search_facts(
        "Hubcorp Example quarterly logistics agreement",
        limit=5,
        tenant_id=TENANT,
        expand_entities=True,
    )

    row = next((r for r in results if r["id"] == related), None)
    assert row is not None and "entity_expansion" in row["leg_ranks"]
