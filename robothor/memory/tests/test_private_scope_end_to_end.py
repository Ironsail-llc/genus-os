"""A private fact is visible to its owner and to no one else, end to end.

The write path stores a fact private to one person as ``scope_kind='user'``,
``scope_id='person:{id}'``. This file proves, on a database migrated through
the whole chain, that every reader which returns fact text honours that scope:

* ``search_memory`` (the tool handler, as the agent calls it): the dense and
  keyword legs, and the graph-activation leg reached through a shared edge;
* ``get_all_about`` (the bridge's entity lookup) and warm-up's entity context.

The owner recalls the fact through the dense leg and through the graph leg.
Another person, a caller with no identity, and a restricted reader recall it
through no leg at all.
"""

from __future__ import annotations

import importlib
import uuid
from contextlib import contextmanager
from typing import Any

import pytest

psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration

TENANT = "test-tenant"
SECRET = "Orion Freight's loading-dock door code is 4417-K"
_VECTOR = [0.05] * 1024

# Every module that opens its own connection on the paths under test.
_MODULES = (
    "robothor.db",
    "robothor.memory.facts",
    "robothor.memory.entities",
    "robothor.memory.write_path",
    "robothor.memory.identity",
    "robothor.memory.segments",
    "robothor.memory.episodes",
    "robothor.memory.outcomes",
    "robothor.memory.conflicts",
    "robothor.crm.dal",
    "robothor.rooms.dal",
    "robothor.engine.warmup",
)


@pytest.fixture
def db(scratch_db, monkeypatch):
    from robothor.db.migrate import _discover

    conn, dsn = scratch_db(through=list(_discover())[-1].migration_id)
    conn.autocommit = True

    @contextmanager
    def _connect(*_a: Any, **_k: Any):
        c = psycopg2.connect(dsn)
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    for name in _MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "get_connection"):
            monkeypatch.setattr(module, "get_connection", _connect)

    from robothor.llm import ollama
    from robothor.memory import facts

    async def _embed(_text: str, *_a: Any, **_k: Any) -> list[float]:
        return list(_VECTOR)

    async def _embed_batch(texts: list[str], *_a: Any, **_k: Any) -> list[list[float]]:
        return [list(_VECTOR) for _ in texts]

    monkeypatch.setattr(ollama, "get_embedding_async", _embed)
    monkeypatch.setattr(ollama, "get_embeddings_batch_async", _embed_batch)
    monkeypatch.setattr(facts, "_embed_query", _embed)
    monkeypatch.setattr(facts, "apply_hnsw_session", lambda _cur: None)
    # Privacy is proven with the graph leg on (off by default), the case
    # with the most ways to reach a private fact.
    monkeypatch.setattr(facts, "GRAPH_LEG_DEFAULT", True)
    monkeypatch.setenv("MEMORY_RERANK_ENABLED", "0")
    monkeypatch.delenv("ROBOTHOR_DATA_SCOPING", raising=False)
    monkeypatch.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (TENANT, TENANT),
    )
    return conn


def _sql(conn: Any, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with conn.cursor() as cur:
        cur.execute(statement, params)
        return cur.fetchall() if cur.description else []


def _person(conn: Any, first: str) -> str:
    pid = str(uuid.uuid4())
    _sql(
        conn,
        "INSERT INTO crm_people (id, first_name, last_name, tenant_id) VALUES (%s, %s, %s, %s)",
        (pid, first, "Example", TENANT),
    )
    return pid


def _entity(conn: Any, name: str, entity_type: str) -> int:
    return int(
        _sql(
            conn,
            "INSERT INTO memory_entities (name, entity_type, tenant_id) VALUES (%s, %s, %s) "
            "RETURNING id",
            (name, entity_type, TENANT),
        )[0][0]
    )


@pytest.fixture
async def world(db) -> dict[str, Any]:
    """Two shared nodes joined by a shared, user-stated edge, and one fact
    private to Alice that mentions the far node, written by the write path."""
    from robothor.memory.write_path import WriteContext, write_facts

    alice, bob = _person(db, "Alice"), _person(db, "Bob")
    dana = _entity(db, "Dana Whitfield", "person")
    orion = _entity(db, "Orion Freight", "organization")
    shared = await write_facts(
        [
            {
                "fact_text": "Dana Whitfield manages the Orion Freight account",
                "category": "work",
                "confidence": 0.9,
                "entities": ["Dana Whitfield", "Orion Freight"],
                "mentions": [
                    {"surface_form": "Dana Whitfield", "entity_type": "person"},
                    {"surface_form": "Orion Freight", "entity_type": "organization"},
                ],
                "relations": [
                    {
                        "source": "Dana Whitfield",
                        "target": "Orion Freight",
                        "relation": "manages account of",
                    }
                ],
            }
        ],
        source_content="shared note",
        context=WriteContext.for_source(
            tenant_id=TENANT, source_channel="webchat", content_type="conversation", role="user"
        ),
        conflict_check=False,
    )
    private = await write_facts(
        [
            {
                "fact_text": SECRET,
                "category": "work",
                "confidence": 0.9,
                "entities": ["Orion Freight"],
                "mentions": [{"surface_form": "Orion Freight", "entity_type": "organization"}],
            }
        ],
        source_content="private note",
        context=WriteContext.for_source(
            tenant_id=TENANT,
            source_channel="webchat",
            content_type="conversation",
            role="user",
            scope=("user", f"person:{alice}"),
        ),
        conflict_check=False,
    )
    (secret_id,) = private["fact_ids"]
    row = _sql(
        db,
        "SELECT scope_kind, scope_id, person_id::text FROM memory_facts WHERE id = %s",
        (secret_id,),
    )[0]
    assert row == ("user", f"person:{alice}", alice), "the write path's private scope"
    edges = _sql(
        db,
        "SELECT count(*) FROM memory_relation_evidence e JOIN memory_relations r "
        "ON r.id = e.relation_id WHERE r.source_entity_id = %s AND r.target_entity_id = %s",
        (dana, orion),
    )[0][0]
    assert edges >= 1, f"the shared edge needs evidence for the walk: {shared}"
    mentions = _sql(
        db,
        "SELECT entity_id FROM memory_mentions WHERE fact_id = %s",
        (secret_id,),
    )
    assert [m[0] for m in mentions] == [orion], "the private fact mentions the far node"
    return {"alice": alice, "bob": bob, "secret": secret_id, "dana": dana, "orion": orion}


def _ctx(person: str | None, *, role: str = "member") -> Any:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.identity.context import IdentityContext

    identity = None
    if person is not None:
        identity = IdentityContext(
            tenant_id=TENANT,
            channel="webchat",
            identifier="agent@example.com",
            verified=True,
            role=role,
            person_id=person,
        )
    return ToolContext(agent_id="main", run_id="", tenant_id=TENANT, user_id="", identity=identity)


async def _recall(monkeypatch, ctx: Any, query: str) -> tuple[list[dict[str, Any]], list[dict]]:
    """``search_memory`` as the agent calls it, and the rows ``search_facts``
    produced for it (with ``leg_ranks``), so each leg can be checked."""
    from robothor.engine.tools.handlers.memory import HANDLERS
    from robothor.memory import facts

    captured: list[dict[str, Any]] = []
    real = facts.search_facts

    async def spy(*a: Any, **k: Any) -> list[dict[str, Any]]:
        rows = await real(*a, **k)
        captured.extend(rows)
        return rows

    monkeypatch.setattr(facts, "search_facts", spy)
    out = await HANDLERS["search_memory"](
        {"query": query, "limit": 10, "include_insights": False, "include_episodes": False},
        ctx,
        record_access=False,
    )
    monkeypatch.setattr(facts, "search_facts", real)
    return out["results"], captured


def _legs_with(rows: list[dict[str, Any]], fact_id: int) -> set[str]:
    legs: set[str] = set()
    for r in rows:
        if r.get("kind", "fact") == "fact" and r.get("id") == fact_id:
            legs |= set(r.get("leg_ranks") or {})
    return legs


DENSE_QUERY = "What is the loading-dock door code?"
GRAPH_QUERY = "What do we know about Dana Whitfield?"


async def test_the_owner_recalls_the_private_fact_by_the_dense_leg(world, monkeypatch) -> None:
    results, rows = await _recall(monkeypatch, _ctx(world["alice"]), DENSE_QUERY)
    assert SECRET in [r["fact"] for r in results]
    assert "vector" in _legs_with(rows, world["secret"])


async def test_the_owner_recalls_the_private_fact_by_the_graph_leg(world, monkeypatch) -> None:
    _, rows = await _recall(monkeypatch, _ctx(world["alice"]), GRAPH_QUERY)
    assert "graph" in _legs_with(rows, world["secret"])


@pytest.mark.parametrize("query", [DENSE_QUERY, GRAPH_QUERY])
@pytest.mark.parametrize("who", ["bob", "nobody"])
async def test_no_other_reader_recalls_the_private_fact_by_any_leg(
    world, monkeypatch, who: str, query: str
) -> None:
    person = world["bob"] if who == "bob" else None
    results, rows = await _recall(monkeypatch, _ctx(person), query)
    assert SECRET not in [r["fact"] for r in results]
    assert _legs_with(rows, world["secret"]) == set()


@pytest.mark.parametrize("query", [DENSE_QUERY, GRAPH_QUERY])
async def test_a_restricted_reader_never_recalls_another_persons_private_fact(
    world, monkeypatch, query: str
) -> None:
    monkeypatch.setenv("ROBOTHOR_DATA_SCOPING", "enforce")
    results, rows = await _recall(monkeypatch, _ctx(world["bob"]), query)
    assert SECRET not in [r["fact"] for r in results]
    assert _legs_with(rows, world["secret"]) == set()
    owner, owner_rows = await _recall(monkeypatch, _ctx(world["alice"]), query)
    leg = "vector" if query == DENSE_QUERY else "graph"
    assert leg in _legs_with(owner_rows, world["secret"])


async def test_the_entity_lookup_never_returns_a_private_fact(world) -> None:
    from robothor.memory.entities import get_all_about

    about = await get_all_about("Orion Freight", tenant_id=TENANT)
    assert SECRET not in [f["fact_text"] for f in about["facts"]]


async def test_warm_up_never_injects_another_persons_private_fact(world) -> None:
    """Both warm-up paths: the graph lines, and today's name match (which also
    matches a one-word entity name in the message)."""
    from robothor.engine import warmup
    from robothor.memory.write_path import WriteContext, write_facts

    kestrel = "Kestrel's vault combination is 7781"
    await write_facts(
        [{"fact_text": kestrel, "category": "work", "confidence": 0.9, "entities": ["Kestrel"]}],
        source_content="private note",
        context=WriteContext.for_source(
            tenant_id=TENANT,
            source_channel="webchat",
            content_type="conversation",
            role="user",
            scope=("user", f"person:{world['alice']}"),
        ),
        conflict_check=False,
    )
    for sender in (world["bob"], None):
        for message in ("Any news from Orion Freight or Dana Whitfield?", "Any news on Kestrel?"):
            text = warmup._build_entity_context(
                message, tenant_id=TENANT, sender_person_id=sender, record_access=False
            )
            assert SECRET not in text
            assert kestrel not in text
