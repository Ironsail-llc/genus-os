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
    "robothor.memory.lifecycle",
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
    return {
        "alice": alice,
        "bob": bob,
        "secret": secret_id,
        "dana": dana,
        "orion": orion,
        "conn": db,
    }


def _ctx(person: str | None, *, role: str = "member", served: bool = True) -> Any:
    """A tool context whose identity is ``person``. ``served``: the run serves
    that person in their own conversation (``ToolContext.private_reader``);
    otherwise it only executes as them, as a cron or workspace run can."""
    from robothor.engine.private_recall import PrivateReader
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
    reader = PrivateReader(person_id=person, user_id=None) if person and served else None
    return ToolContext(
        agent_id="main",
        run_id="",
        tenant_id=TENANT,
        user_id="",
        identity=identity,
        private_reader=reader,
    )


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


@pytest.mark.parametrize("restricted", [False, True])
@pytest.mark.parametrize("query", [DENSE_QUERY, GRAPH_QUERY])
async def test_a_run_that_only_executes_as_the_owner_recalls_nothing_private(
    world, monkeypatch, query: str, restricted: bool
) -> None:
    """The owner's identity without the owner being served (a scheduled,
    workspace or sub-agent run as them): its output may reach others."""
    if restricted:
        monkeypatch.setenv("ROBOTHOR_DATA_SCOPING", "enforce")
    results, rows = await _recall(monkeypatch, _ctx(world["alice"], served=False), query)
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


async def test_warm_up_graph_lines_never_inject_another_persons_private_fact(
    world, monkeypatch
) -> None:
    """The graph-lines path, which is off by default, turned on. The owner as
    the sender is not enough: only the person the run serves is a reader."""
    from robothor.engine import warmup

    monkeypatch.setattr(warmup, "WARMUP_GRAPH_DEFAULT", True)
    for sender in (world["bob"], None, world["alice"]):
        text = warmup._build_entity_context(
            "Any news from Orion Freight or Dana Whitfield?",
            tenant_id=TENANT,
            sender_person_id=sender,
            record_access=False,
        )
        assert SECRET not in text


async def test_the_admin_erasure_view_finds_a_private_fact_the_default_hides(world) -> None:
    """The tenant admin's erasure search must find a private fact to forget it."""
    from robothor.memory.facts import search_facts

    def _ids(rows: list[dict[str, Any]]) -> list[Any]:
        return [r.get("id") for r in rows if r.get("kind", "fact") == "fact"]

    hidden = await search_facts(DENSE_QUERY, tenant_id=TENANT, include_segments=False)
    assert world["secret"] not in _ids(hidden)
    erasure = await search_facts(
        DENSE_QUERY, tenant_id=TENANT, include_segments=False, admin_erasure_view=True
    )
    assert world["secret"] in _ids(erasure)


async def test_relation_inference_never_reads_a_private_fact(world, monkeypatch) -> None:
    """Nightly inference turns fact text into SHARED edges every reader sees,
    so a private fact must reach neither its pair search nor its prompt."""
    from robothor.memory import entities, lifecycle
    from robothor.memory.write_path import WriteContext, write_facts

    db_conn = world["conn"]
    kestrel = _entity(db_conn, "Kestrel Labs", "organization")
    text = "Dana Whitfield is interviewing at Kestrel Labs next week"
    private = await write_facts(
        [
            {
                "fact_text": text,
                "category": "work",
                "confidence": 0.9,
                "entities": ["Dana Whitfield", "Kestrel Labs"],
            }
        ],
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
    (private_id,) = private["fact_ids"]
    assert _sql(db_conn, "SELECT scope_kind FROM memory_facts WHERE id = %s", (private_id,)) == [
        ("user",)
    ]

    pairs = await entities.find_cooccurring_entity_pairs([world["dana"], kestrel], tenant_id=TENANT)
    assert all(private_id not in (p.get("shared_fact_ids") or []) for p in pairs)

    # The prompt's fact text, even when a pair names the private fact's id.
    async def _underconnected(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [{"id": world["dana"]}, {"id": kestrel}]

    async def _pairs(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [
            {
                "entity_a_id": world["dana"],
                "entity_a_name": "Dana Whitfield",
                "entity_b_id": kestrel,
                "entity_b_name": "Kestrel Labs",
                "shared_fact_ids": [private_id],
            }
        ]

    prompted: list[dict[str, Any]] = []

    async def _infer(pairs: list[dict[str, Any]], **_k: Any) -> list[dict[str, Any]]:
        prompted.extend(pairs)
        return []

    monkeypatch.setattr(entities, "find_underconnected_entities", _underconnected)
    monkeypatch.setattr(entities, "find_cooccurring_entity_pairs", _pairs)
    monkeypatch.setattr(entities, "infer_relations", _infer)
    await lifecycle.infer_entity_relationships(tenant_id=TENANT)
    assert prompted, "the pair reached the inference step"
    assert all(text not in (p.get("shared_facts_text") or []) for p in prompted)
