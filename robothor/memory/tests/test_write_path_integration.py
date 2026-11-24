"""The one write path on real Postgres, with the models replaced by strict fakes.

One scratch database is built from the full migration chain (the graph
foundation and the graph keys included) for the whole module, and every table
the write path touches is emptied before each test, so nothing here reads or
writes the shared test database.

The model fakes are strict: extraction answers only the fact-extraction schema
and is validated against it, classification answers only the classification
schema, and anything else fails the test. Embeddings are deterministic vectors
the tests place on purpose (two texts are "similar" only when a test says so).

Acceptance criteria, each with its own test:

* each relation's evidence is the fact it came from, in a multi-fact batch;
* ``store_memory`` contradicting an operator-confirmed fact leaves it active;
* external content yields facts AND edges with external origin, and an
  assistant turn paraphrasing it is agent-generated, never user-stated;
* an extraction failure still stores the raw content and reports the failure.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import math
import os
import random
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import jsonschema
import psycopg2
import pytest

from robothor.memory.graph_schema import Origin

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

TENANT = "test-tenant"
OTHER = "other-tenant"
ALICE = "6f1c1b7e-0000-4000-8000-000000000001"
BOB = "6f1c1b7e-0000-4000-8000-000000000002"
DIM = 1024

_TABLES = (
    "memory_mentions",
    "memory_relation_evidence",
    "memory_resolution_decisions",
    "memory_relations",
    "memory_conflict_decisions",
    "memory_facts_audit",
    "memory_entities",
    "memory_facts",
    "memory_write_jobs",
    "agent_breadcrumbs",
    "contact_identifiers",
    "crm_people",
    "crm_companies",
)


# ═══ the database ════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    """One scratch database, fully migrated, for the module."""
    from robothor.db.migrate import _discover, _strip_outer_transaction

    admin = os.environ.get("ROBOTHOR_TEST_ADMIN_DSN", f"dbname=postgres user={getpass.getuser()}")
    name = f"scratch_wp_{uuid.uuid4().hex[:8]}"
    try:
        root = psycopg2.connect(admin)
    except psycopg2.OperationalError as exc:  # pragma: no cover - environment
        pytest.skip(f"no Postgres for integration tests: {exc}")
    root.autocommit = True
    with root.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    root.close()
    parts = dict(psycopg2.extensions.parse_dsn(admin))
    parts["dbname"] = name
    dsn = " ".join(f"{k}={v}" for k, v in parts.items())
    db = psycopg2.connect(dsn)
    db.autocommit = True
    with db.cursor() as cur:
        for ext in ("vector", "pgcrypto", '"uuid-ossp"'):
            with contextlib.suppress(Exception):
                cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        for m in _discover():
            cur.execute(_strip_outer_transaction(m.path.read_text()))
        for tenant in (TENANT, OTHER):
            cur.execute(
                "INSERT INTO crm_tenants (id, display_name) VALUES (%s, 'Test') "
                "ON CONFLICT DO NOTHING",
                (tenant,),
            )
    db.close()
    yield dsn
    root = psycopg2.connect(admin)
    root.autocommit = True
    with root.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (name,)
        )
        cur.execute(f"DROP DATABASE IF EXISTS {name}")
    root.close()


class DB:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True

    def q(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        rows = self.q(sql, params)
        return rows[0][0] if rows else None

    def facts(self, tenant: str = TENANT) -> list[dict[str, Any]]:
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, fact_text, category, entities, is_active, origin, event_time, "
                "scope_kind, scope_id, person_id::text AS person_id, metadata, source_channel, "
                "superseded_by FROM memory_facts WHERE tenant_id = %s ORDER BY id",
                (tenant,),
            )
            return [dict(r) for r in cur.fetchall()]

    def fact_id(self, text: str, tenant: str = TENANT) -> int:
        return int(
            self.one(
                "SELECT id FROM memory_facts WHERE tenant_id = %s AND fact_text = %s "
                "ORDER BY id DESC LIMIT 1",
                (tenant, text),
            )
        )

    def edges(self, tenant: str = TENANT) -> list[dict[str, Any]]:
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT r.id, s.name AS source, t.name AS target, r.relation_type, r.predicate, "
                "r.origin, r.fact_id, ARRAY(SELECT e.fact_id FROM memory_relation_evidence e "
                "WHERE e.relation_id = r.id ORDER BY e.fact_id) AS evidence "
                "FROM memory_relations r JOIN memory_entities s ON s.id = r.source_entity_id "
                "JOIN memory_entities t ON t.id = r.target_entity_id WHERE r.tenant_id = %s "
                "ORDER BY r.id",
                (tenant,),
            )
            return [dict(r) for r in cur.fetchall()]

    def mentions(self, fact_id: int) -> list[tuple[str, str | None, int | None]]:
        return [
            (r[0], r[1], r[2])
            for r in self.q(
                "SELECT surface_form, entity_type, entity_id FROM memory_mentions "
                "WHERE fact_id = %s ORDER BY id",
                (fact_id,),
            )
        ]

    def entity(self, name: str, etype: str = "person", tenant: str = TENANT) -> int:
        return int(
            self.one(
                "INSERT INTO memory_entities (name, entity_type, tenant_id) VALUES (%s, %s, %s) "
                "RETURNING id",
                (name, etype, tenant),
            )
        )


# ═══ the models ══════════════════════════════════════════════════════════


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _random_vec(text: str) -> list[float]:
    rnd = random.Random(hashlib.sha256(text.encode()).hexdigest())
    return _unit([rnd.gauss(0, 1) for _ in range(DIM)])


class Embeddings:
    """Deterministic: unrelated texts are near-orthogonal; ``near`` places one."""

    def __init__(self) -> None:
        self.placed: dict[str, list[float]] = {}

    def vec(self, text: str) -> list[float]:
        return self.placed.get(text) or _random_vec(text)

    def near(self, text: str, anchor: str, cos: float) -> None:
        a = self.vec(anchor)
        noise = _random_vec("noise:" + text)
        # Orthogonalise the noise against the anchor, then mix to the cosine asked.
        dot = sum(x * y for x, y in zip(a, noise, strict=True))
        ortho = _unit([n - dot * x for n, x in zip(noise, a, strict=True)])
        s = math.sqrt(max(0.0, 1 - cos * cos))
        self.placed[text] = _unit([cos * x + s * o for x, o in zip(a, ortho, strict=True)])

    async def one(self, text: str, *_a: Any, **_k: Any) -> list[float]:
        return self.vec(text)

    async def batch(self, texts: list[str], *_a: Any, **_k: Any) -> list[list[float]]:
        return [self.vec(t) for t in texts]


def fact(text: str, mentions=(), relations=(), **kw: Any) -> dict[str, Any]:
    return {
        "fact_text": text,
        "category": kw.get("category", "project"),
        "confidence": kw.get("confidence", 0.9),
        "event_time": kw.get("event_time"),
        "mentions": [{"surface_form": s, "entity_type": t} for s, t in mentions],
        "relations": [{"source": s, "relation": r, "target": t} for s, r, t in relations],
    }


class Model:
    """Scripted extraction (by a marker found in the content) and classification."""

    def __init__(self) -> None:
        self.extractions: list[tuple[str, Any]] = []
        self.classifications: list[str] = []
        self.calls: list[str] = []

    def on(self, marker: str, *facts: dict[str, Any], raw: str | None = None) -> None:
        self.extractions.append((marker, raw if raw is not None else {"facts": list(facts)}))

    async def generate(self, prompt: str, system: str | None = None, **kw: Any) -> str:
        from robothor.memory.conflicts import CLASSIFICATION_SCHEMA
        from robothor.memory.facts import FACT_EXTRACTION_SCHEMA

        fmt = kw.get("format")
        if fmt == FACT_EXTRACTION_SCHEMA:
            self.calls.append("extract")
            content = prompt.rsplit("Content:\n", 1)[-1]
            for marker, answer in self.extractions:
                if marker in content:
                    if isinstance(answer, str):
                        return answer
                    jsonschema.validate(answer, FACT_EXTRACTION_SCHEMA)
                    return json.dumps(answer)
            return json.dumps({"facts": []})
        if fmt == CLASSIFICATION_SCHEMA:
            self.calls.append("classify")
            kind = self.classifications.pop(0) if self.classifications else "new"
            return json.dumps({"classification": kind, "reasoning": "scripted"})
        raise AssertionError(f"unexpected model call (format={fmt!r})")


# ═══ the world ═══════════════════════════════════════════════════════════


class World:
    def __init__(self, db: DB, emb: Embeddings, model: Model) -> None:
        self.db, self.emb, self.model = db, emb, model


@pytest.fixture
def world(database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    db = DB(database)
    db.q(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
    # The two readers of private memory are CRM people (memory_facts.person_id
    # references crm_people), named so that no fact below mentions them.
    for pid, first in ((ALICE, "Reader"), (BOB, "Other")):
        db.q(
            "INSERT INTO crm_people (id, first_name, last_name, tenant_id) "
            "VALUES (%s, %s, 'Person', %s)",
            (pid, first, TENANT),
        )

    @contextmanager
    def _conn(*_a: Any, **_k: Any) -> Iterator[Any]:
        conn = psycopg2.connect(database)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    import importlib

    for name in (
        "robothor.memory.facts",
        "robothor.memory.conflicts",
        "robothor.memory.entities",
        "robothor.memory.identity",
        "robothor.memory.bitemporal",
        "robothor.memory.ingestion",
        "robothor.memory.lifecycle",
        "robothor.memory.breadcrumbs",
        "robothor.memory.resolution",
        "robothor.memory.write_jobs",
        "robothor.memory.write_path",
        "robothor.memory.quality",
        "robothor.memory.ingest_state",
        "robothor.memory.conversation_ingest",
        "robothor.crm.dal",
    ):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError:  # pragma: no cover - only before the module exists
            continue
        if hasattr(module, "get_connection"):
            monkeypatch.setattr(module, "get_connection", _conn)
    from robothor.crm import dal
    from robothor.llm import ollama
    from robothor.memory import generation

    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    emb = Embeddings()
    model = Model()
    monkeypatch.setattr(ollama, "get_embedding_async", emb.one)
    monkeypatch.setattr(ollama, "get_embeddings_batch_async", emb.batch)
    monkeypatch.setattr(generation, "generate", model.generate)
    for var in ("MEMORY_QUALITY_GATE", "MEMORY_ASYNC_WRITE", "MEMORY_VAULT_POPULATE",
                "MEMORY_RESOLUTION_CAPTURE", "MEMORY_INGEST_SKIP_GENERATED"):  # fmt: skip
        monkeypatch.delenv(var, raising=False)
    yield World(db, emb, model)
    db.conn.close()


async def _ingest(content: str, channel: str = "api", ctype: str = "note", **kw: Any) -> Any:
    from robothor.memory.ingestion import ingest_content

    return await ingest_content(content, channel, ctype, tenant_id=kw.pop("tenant", TENANT), **kw)


async def _store_memory(content: str, *, person: str | None = None, tenant: str = TENANT) -> Any:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers.memory import HANDLERS
    from robothor.identity.context import IdentityContext

    ident = (
        IdentityContext(
            tenant_id=tenant, channel="telegram", identifier="4242", verified=True, person_id=person
        )  # fmt: skip
        if person
        else None
    )
    return await HANDLERS["store_memory"](
        {"content": content, "content_type": "note"},
        ToolContext(agent_id="example-agent", tenant_id=tenant, identity=ident),
    )


# ═══ acceptance (i): evidence is the fact a relation came from ═══════════


@pytest.mark.asyncio
async def test_each_relation_in_a_batch_is_evidenced_by_the_fact_it_came_from(world: World) -> None:
    world.model.on(
        "BATCH-1",
        fact("Alice Moreno works at Northwind Traders.",
             [("Alice Moreno", "person"), ("Northwind Traders", "organization")],
             [("Alice Moreno", "works at", "Northwind Traders")]),
        fact("Bob Chen leads Project Helios.",
             [("Bob Chen", "person"), ("Project Helios", "project")],
             [("Bob Chen", "leads", "Project Helios")]),
        fact("Project Helios uses PostgreSQL.",
             [("Project Helios", "project"), ("PostgreSQL", "technology")],
             [("Project Helios", "uses", "PostgreSQL")]),
    )  # fmt: skip
    out = await _ingest("BATCH-1 notes from the planning meeting")

    assert out["facts_processed"] == 3 and out["relations_stored"] == 3
    ids = {f["fact_text"]: f["id"] for f in world.db.facts()}
    by_pair = {(e["source"], e["target"]): e for e in world.db.edges()}
    assert by_pair[("Alice Moreno", "Northwind Traders")]["evidence"] == [
        ids["Alice Moreno works at Northwind Traders."]
    ]
    assert by_pair[("Bob Chen", "Project Helios")]["evidence"] == [
        ids["Bob Chen leads Project Helios."]
    ]
    helios_pg = by_pair[("Project Helios", "PostgreSQL")]
    assert helios_pg["evidence"] == [ids["Project Helios uses PostgreSQL."]]
    # The legacy single fact_id is the edge's first evidence, not the batch's first fact.
    assert helios_pg["fact_id"] == ids["Project Helios uses PostgreSQL."]
    assert by_pair[("Alice Moreno", "Northwind Traders")]["relation_type"] == "works at"
    assert by_pair[("Alice Moreno", "Northwind Traders")]["predicate"] == "works_at"


# ═══ acceptance (ii): store_memory keeps an operator-confirmed fact ══════


@pytest.mark.asyncio
async def test_store_memory_contradicting_an_operator_confirmed_fact_keeps_it_active(
    world: World,
) -> None:
    confirmed_text = "Alice Moreno's preferred contact channel is email."
    new_text = "Alice Moreno's preferred contact channel is phone."
    confirmed = int(
        world.db.one(
            "INSERT INTO memory_facts (fact_text, category, entities, embedding, metadata, "
            "tenant_id, origin) VALUES (%s, 'preference', %s, %s::vector, %s::jsonb, %s, "
            "'operator_confirmed') RETURNING id",
            (confirmed_text, ["Alice Moreno"], str(world.emb.vec(confirmed_text)),
             json.dumps({"operator_confirmed": {"at": "2026-01-01T00:00:00+00:00", "by": "op"}}),
             TENANT),
        )
    )  # fmt: skip
    world.emb.near(new_text, confirmed_text, 0.86)
    world.model.on("STORE-2", fact(new_text, [("Alice Moreno", "person")], category="preference"))
    world.model.classifications.append("contradiction")

    out = await _store_memory("STORE-2: Alice said to phone her from now on")

    rows = {f["id"]: f for f in world.db.facts()}
    assert rows[confirmed]["is_active"] is True
    overruled = [f for f in rows.values() if f["fact_text"] == new_text]
    assert len(overruled) == 1 and overruled[0]["is_active"] is False
    assert overruled[0]["superseded_by"] == confirmed
    assert out["facts_kept_confirmed"] == 1
    assert "classify" in world.model.calls


# ═══ acceptance (iii): external stays external ═══════════════════════════


@pytest.mark.asyncio
async def test_external_content_gives_external_facts_and_edges(world: World) -> None:
    world.model.on(
        "EMAIL-3",
        fact("Carol Diaz is the new CFO of Globex Corporation.",
             [("Carol Diaz", "person"), ("Globex Corporation", "organization")],
             [("Carol Diaz", "is CFO of", "Globex Corporation")]),
    )  # fmt: skip
    await _ingest("EMAIL-3 From: news@example.com ...", channel="email", ctype="email")

    (row,) = world.db.facts()
    assert row["origin"] == Origin.EXTERNAL and row["source_channel"] == "email"
    (edge,) = world.db.edges()
    assert edge["origin"] == Origin.EXTERNAL


@pytest.mark.asyncio
async def test_an_assistant_turn_paraphrasing_external_content_is_agent_generated(
    world: World,
) -> None:
    from robothor.memory.conversation_ingest import ingest_conversation_session

    world.model.on(
        "ASST-3",
        fact("Carol Diaz became CFO of Globex Corporation.",
             [("Carol Diaz", "person"), ("Globex Corporation", "organization")],
             [("Carol Diaz", "is CFO of", "Globex Corporation")]),
    )  # fmt: skip
    history = [
        {"role": "user", "content": "anything new in my inbox?"},
        {"role": "assistant", "content": "ASST-3 The email says Carol Diaz is now CFO at Globex."},
        {"role": "user", "content": "ok thanks"},
        {"role": "assistant", "content": "You're welcome."},
    ]
    await ingest_conversation_session(
        "agent:main:webchat:abc", history, "main", "webchat", "run-1", TENANT
    )
    (row,) = world.db.facts()
    assert row["origin"] == Origin.AGENT_GENERATED
    (edge,) = world.db.edges()
    assert edge["origin"] == Origin.AGENT_GENERATED


@pytest.mark.asyncio
async def test_user_turns_of_a_conversation_are_user_stated(world: World) -> None:
    from robothor.memory.conversation_ingest import ingest_conversation_session

    world.model.on("USER-T", fact("Dana Reyes manages Project Atlas.",
                                  [("Dana Reyes", "person"), ("Project Atlas", "project")],
                                  [("Dana Reyes", "manages", "Project Atlas")]))  # fmt: skip
    world.model.on("ASST-T", fact("Project Atlas launches in June.",
                                  [("Project Atlas", "project")]))  # fmt: skip
    history = [
        {"role": "user", "content": "USER-T Dana Reyes manages Project Atlas now."},
        {"role": "assistant", "content": "ASST-T Got it; Atlas launches in June per the plan."},
        {"role": "user", "content": "yes"},
        {"role": "assistant", "content": "ok"},
    ]
    await ingest_conversation_session(
        "agent:main:webchat:abc", history, "main", "webchat", "run-1", TENANT
    )
    origin = {f["fact_text"]: f["origin"] for f in world.db.facts()}
    assert origin == {
        "Dana Reyes manages Project Atlas.": Origin.USER_STATED,
        "Project Atlas launches in June.": Origin.AGENT_GENERATED,
    }
    (edge,) = world.db.edges()
    assert edge["origin"] == Origin.USER_STATED


# ═══ acceptance (iv): extraction failure stores the raw content ══════════


@pytest.mark.asyncio
async def test_extraction_failure_stores_the_raw_content_and_reports_it(world: World) -> None:
    world.model.on("BROKEN-4", raw="{ this is not json")
    content = "BROKEN-4 Alice Moreno called about the Helios invoice."
    out = await _ingest(content, channel="email", ctype="email")

    assert out["extraction_failed"] is True and out["extraction_error"] == "unparseable"
    (row,) = world.db.facts()
    assert row["fact_text"] == content and row["is_active"] is True
    assert row["metadata"]["extraction"] == "fallback_raw"
    assert row["origin"] == Origin.EXTERNAL

    out = await _store_memory("BROKEN-4 Bob Chen asked for the deck.")
    assert out["extraction_failed"] is True and out["facts_stored"] == 1


# ═══ edge origin ═════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_an_edge_takes_the_origin_of_its_best_supporting_fact(world: World) -> None:
    world.model.on("EMAIL-E", fact("Erin Park works for Initech Systems.",
                                   [("Erin Park", "person"), ("Initech Systems", "organization")],
                                   [("Erin Park", "works for", "Initech Systems")]))  # fmt: skip
    await _ingest("EMAIL-E newsletter", channel="email", ctype="email")
    (edge,) = world.db.edges()
    assert edge["origin"] == Origin.EXTERNAL

    world.model.on("CRM-E", fact("Erin Park is employed by Initech Systems.",
                                 [("Erin Park", "person"), ("Initech Systems", "organization")],
                                 [("Erin Park", "works for", "Initech Systems")]))  # fmt: skip
    await _ingest("CRM-E contact note", channel="crm", ctype="note")
    (edge,) = world.db.edges()
    assert edge["origin"] == Origin.CRM
    assert len(edge["evidence"]) == 2
    origins = {r[0] for r in world.db.q(
        "SELECT f.origin FROM memory_relation_evidence e JOIN memory_facts f ON f.id = e.fact_id "
        "WHERE e.relation_id = %s", (edge["id"],))}  # fmt: skip
    assert origins == {Origin.EXTERNAL, Origin.CRM}  # per-fact origin stays readable

    # A weaker source later does not lower it.
    world.model.on("EMAIL-F", fact("Erin Park is with Initech Systems.",
                                   [("Erin Park", "person"), ("Initech Systems", "organization")],
                                   [("Erin Park", "works for", "Initech Systems")]))  # fmt: skip
    await _ingest("EMAIL-F second newsletter", channel="email", ctype="email")
    (edge,) = world.db.edges()
    assert edge["origin"] == Origin.CRM and len(edge["evidence"]) == 3


@pytest.mark.asyncio
async def test_an_unresolved_endpoint_creates_no_edge_and_is_counted(world: World) -> None:
    world.model.on("SINGLE-U", fact("Frank met Grace Hopper Labs.",
                                    [("Frank", "person"), ("Grace Hopper Labs", "organization")],
                                    [("Frank", "met", "Grace Hopper Labs")]))  # fmt: skip
    out = await _ingest("SINGLE-U met")
    assert world.db.edges() == []
    assert out["relations_unresolved"] == 1
    fid = world.db.fact_id("Frank met Grace Hopper Labs.")
    mentions = world.db.mentions(fid)
    assert ("Frank", "person", None) in mentions  # recorded, deliberately unresolved


# ═══ mentions ════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mentions_are_written_once_when_the_same_content_is_ingested_again(
    world: World,
) -> None:
    world.model.on("AGAIN-M", fact("Alice Moreno owns the Helios budget.",
                                   [("Alice Moreno", "person"), ("Helios", "project")],
                                   [("Alice Moreno", "owns", "Helios")]))  # fmt: skip
    await _ingest("AGAIN-M budget")
    await _ingest("AGAIN-M budget")
    (row,) = world.db.facts()
    assert len(world.db.mentions(row["id"])) == 2
    assert world.db.one("SELECT count(*) FROM memory_mentions") == 2
    assert world.db.one("SELECT count(*) FROM memory_relation_evidence") == 1
    # The database itself enforces it now, whatever a writer does.
    with pytest.raises(psycopg2.errors.UniqueViolation):
        world.db.q(
            "INSERT INTO memory_mentions (tenant_id, fact_id, surface_form) VALUES (%s, %s, %s)",
            (TENANT, row["id"], "ALICE MORENO"),
        )


@pytest.mark.asyncio
async def test_the_entities_column_is_still_filled(world: World) -> None:
    world.model.on("COMPAT", fact("Alice Moreno uses Grafana daily.",
                                  [("Alice Moreno", "person"), ("Grafana", "technology")]))  # fmt: skip
    await _ingest("COMPAT tools")
    (row,) = world.db.facts()
    assert row["entities"] == ["Alice Moreno", "Grafana"]


# ═══ event time ══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_event_time_is_stored_when_stated_and_null_when_not(world: World) -> None:
    world.model.on(
        "WHEN-T",
        fact("Alice Moreno signed the Northwind contract on 2026-03-02.",
             [("Alice Moreno", "person")], event_time="2026-03-02"),
        fact("Alice Moreno prefers morning meetings.", [("Alice Moreno", "person")]),
    )  # fmt: skip
    await _ingest("WHEN-T notes")
    when = {f["fact_text"]: f["event_time"] for f in world.db.facts()}
    signed = when["Alice Moreno signed the Northwind contract on 2026-03-02."]
    assert signed is not None and signed.date().isoformat() == "2026-03-02"
    assert when["Alice Moreno prefers morning meetings."] is None


# ═══ scope and the hash ══════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("private_first", [True, False])
async def test_a_private_and_a_shared_fact_with_identical_text_never_merge(
    world: World, private_first: bool
) -> None:
    text = "The quarterly review moved to Thursday at Northwind Traders."
    world.model.on("SAME-T", fact(text, [("Northwind Traders", "organization")]))
    if private_first:
        await _store_memory("SAME-T private", person=ALICE)
        await _store_memory("SAME-T shared")
    else:
        await _store_memory("SAME-T shared")
        await _store_memory("SAME-T private", person=ALICE)

    rows = [f for f in world.db.facts() if f["fact_text"] == text]
    assert len(rows) == 2 and all(r["is_active"] for r in rows)
    scopes = {(r["scope_kind"], r["scope_id"], r["person_id"]) for r in rows}
    assert scopes == {("shared", None, None), ("user", ALICE, ALICE)}
    # Neither was reinforced into the other.
    assert (
        world.db.one("SELECT count(*) FROM memory_conflict_decisions WHERE action = 'reinforced'")
        == 0
    )


@pytest.mark.asyncio
async def test_two_peoples_private_facts_with_identical_text_never_merge(world: World) -> None:
    text = "My flight to Lisbon leaves on Friday morning."
    world.model.on("TWO-P", fact(text, [("Lisbon", "location")]))
    await _store_memory("TWO-P mine", person=ALICE)
    await _store_memory("TWO-P mine", person=BOB)
    await _store_memory("TWO-P mine", person=ALICE)  # Alice again: deduplicated into her own

    rows = [f for f in world.db.facts() if f["fact_text"] == text]
    assert sorted(r["scope_id"] for r in rows) == sorted([ALICE, BOB])


@pytest.mark.asyncio
async def test_private_facts_write_mentions_but_no_edges_or_new_nodes(world: World) -> None:
    alice = world.db.entity("Alice Moreno")
    org = world.db.entity("Northwind Traders", "organization")
    world.model.on("PRIV-E", fact("Alice Moreno is interviewing at Northwind Traders.",
                                  [("Alice Moreno", "person"), ("Northwind Traders", "organization"),
                                   ("Hidden Project Zeta", "project")],
                                  [("Alice Moreno", "interviewing at", "Northwind Traders")]))  # fmt: skip
    out = await _store_memory("PRIV-E confidential", person=ALICE)

    fid = world.db.fact_id("Alice Moreno is interviewing at Northwind Traders.")
    mentions = {m[0]: m[2] for m in world.db.mentions(fid)}
    assert mentions["Alice Moreno"] == alice and mentions["Northwind Traders"] == org
    # No shared node is minted from a private fact's text ...
    assert mentions["Hidden Project Zeta"] is None
    assert (
        world.db.one("SELECT count(*) FROM memory_entities WHERE name = 'Hidden Project Zeta'") == 0
    )
    # ... and no edge exists that today's readers would show to everyone.
    assert world.db.edges() == []
    assert out["relations_private_deferred"] == 1


# ═══ consolidation ═══════════════════════════════════════════════════════


def _seed(world: World, text: str, *, origin: str, scope=("shared", None), tenant=TENANT,
          anchor: str | None = None, mentions=()) -> int:  # fmt: skip
    if anchor:
        world.emb.near(text, anchor, 0.95)
    person = scope[1] if scope[0] == "user" else None
    fid = int(world.db.one(
        "INSERT INTO memory_facts (fact_text, category, entities, embedding, tenant_id, origin, "
        "scope_kind, scope_id, person_id) VALUES (%s, 'project', %s, %s::vector, %s, %s, %s, %s, "
        "%s) RETURNING id",
        (text, [m[0] for m in mentions], str(world.emb.vec(text)), tenant, origin, scope[0],
         scope[1], person),
    ))  # fmt: skip
    for surface, etype in mentions:
        eid = world.db.one(
            "SELECT id FROM memory_entities WHERE tenant_id = %s AND name = %s", (tenant, surface)
        ) or world.db.entity(surface, etype, tenant)
        world.db.q(
            "INSERT INTO memory_mentions (tenant_id, fact_id, entity_id, surface_form, "
            "entity_type) VALUES (%s, %s, %s, %s, %s)",
            (tenant, fid, eid, surface, etype),
        )
    return fid


@pytest.mark.asyncio
async def test_a_consolidated_fact_takes_the_least_trusted_origin_and_the_sources_graph(
    world: World,
) -> None:
    from robothor.memory import lifecycle

    anchor = "Project Helios ships in Q3."
    a = _seed(world, anchor, origin="user_stated", mentions=[("Project Helios", "project")])
    b = _seed(world, "Project Helios ships in the third quarter.", origin="external",
              anchor=anchor, mentions=[("Project Helios", "project")])  # fmt: skip
    c = _seed(world, "Helios is shipping in Q3.", origin="crm", anchor=anchor,
              mentions=[("Project Helios", "project")])  # fmt: skip
    # An edge evidenced by the user-stated source.
    helios = world.db.one("SELECT id FROM memory_entities WHERE name = 'Project Helios'")
    team = world.db.entity("Platform Team", "organization")
    rel = world.db.one(
        "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
        "fact_id, tenant_id, origin) VALUES (%s, %s, 'owned by', %s, %s, 'user_stated') "
        "RETURNING id",
        (helios, team, a, TENANT),
    )
    world.db.q(
        "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) VALUES (%s,%s,%s)",
        (TENANT, rel, a),
    )

    groups = await lifecycle.find_consolidation_candidates(min_group_size=3)
    assert [sorted(f["id"] for f in g) for g in groups] == [sorted([a, b, c])]
    new_id = await lifecycle.store_consolidated(groups[0], "Project Helios ships in Q3 2026.")

    row = next(f for f in world.db.facts() if f["id"] == new_id)
    assert row["origin"] == Origin.EXTERNAL
    assert (row["scope_kind"], row["scope_id"]) == ("shared", None)
    assert [m[0] for m in world.db.mentions(new_id)] == ["Project Helios"]
    (edge,) = world.db.edges()
    assert new_id in edge["evidence"]


@pytest.mark.asyncio
async def test_consolidation_never_groups_across_scopes_or_tenants(world: World) -> None:
    from robothor.memory import lifecycle

    anchor = "The Lisbon offsite is booked for May."
    _seed(world, anchor, origin="user_stated")
    _seed(world, "The Lisbon offsite is booked for May 2026.", origin="user_stated",
          anchor=anchor, scope=("user", ALICE))  # fmt: skip
    _seed(world, "Lisbon offsite booked: May.", origin="user_stated", anchor=anchor,
          scope=("user", BOB))  # fmt: skip
    _seed(world, "Offsite in Lisbon is in May.", origin="user_stated", anchor=anchor,
          tenant=OTHER)  # fmt: skip
    _seed(world, "May is when the Lisbon offsite happens.", origin="user_stated",
          anchor=anchor, scope=("user", ALICE))  # fmt: skip

    groups = await lifecycle.find_consolidation_candidates(min_group_size=2)
    for group in groups:
        keys = {(f["tenant_id"], f["scope_kind"], f["scope_id"]) for f in group}
        assert len(keys) == 1, group
    # Alice's two private facts may consolidate with each other, and only with each other.
    assert any({f["scope_id"] for f in g} == {ALICE} and len(g) == 2 for g in groups)


# ═══ every routed writer produces mentions ═══════════════════════════════


@pytest.mark.asyncio
async def test_record_resolution_writes_mentions_through_the_write_path(world: World) -> None:
    from robothor.memory.resolution import record_resolution

    helios = world.db.entity("Project Helios", "project")
    out = await record_resolution(
        "the Project Helios outage alert", "confirmed resolved", tenant_id=TENANT
    )
    fid = out["resolution_id"]
    (row,) = [f for f in world.db.facts() if f["id"] == fid]
    assert row["origin"] == Origin.AGENT_GENERATED
    assert ("Project Helios", "project", helios) in world.db.mentions(fid)


@pytest.mark.asyncio
async def test_breadcrumb_promotion_writes_mentions(world: World) -> None:
    from robothor.memory.breadcrumbs import promote_hot_breadcrumbs

    helios = world.db.entity("Project Helios", "project")
    world.db.q(
        "INSERT INTO agent_breadcrumbs (tenant_id, agent_id, content, access_count) "
        "VALUES (%s, 'example-agent', %s::jsonb, 9)",
        (TENANT, json.dumps({"note": "Project Helios deploys need the staging flag"})),
    )
    assert (await promote_hot_breadcrumbs(tenant_id=TENANT))["promoted"] == 1
    (row,) = world.db.facts()
    assert row["origin"] == Origin.AGENT_GENERATED
    assert ("Project Helios", "project", helios) in world.db.mentions(row["id"])


@pytest.mark.asyncio
async def test_a_write_job_produces_mentions_and_keeps_its_scope(world: World) -> None:
    from robothor.memory.write_jobs import enqueue_write, process_write_job
    from robothor.memory.write_path import WriteContext

    world.model.on("JOB-W", fact("Alice Moreno renewed the Initech Systems contract.",
                                 [("Alice Moreno", "person"),
                                  ("Initech Systems", "organization")]))  # fmt: skip
    ctx = WriteContext(tenant_id=TENANT, source_channel="mcp", content_type="note",
                       origin=Origin.AGENT_GENERATED, scope_kind="user", scope_id=ALICE)  # fmt: skip
    job = await enqueue_write("JOB-W renewal", content_type="note", tenant_id=TENANT,
                              context=ctx)  # fmt: skip
    assert await process_write_job(job) is True
    (row,) = world.db.facts()
    assert (row["scope_kind"], row["scope_id"]) == ("user", ALICE)
    assert {m[0] for m in world.db.mentions(row["id"])} == {"Alice Moreno", "Initech Systems"}


# ═══ tenant isolation ════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_everything_written_stays_in_its_tenant(world: World) -> None:
    other_alice = world.db.entity("Alice Moreno", "person", OTHER)
    world.model.on("TEN-I", fact("Alice Moreno works at Northwind Traders.",
                                 [("Alice Moreno", "person"),
                                  ("Northwind Traders", "organization")],
                                 [("Alice Moreno", "works at", "Northwind Traders")]))  # fmt: skip
    await _ingest("TEN-I staffing", tenant=TENANT)

    assert world.db.facts(OTHER) == [] and world.db.edges(OTHER) == []
    for table in ("memory_facts", "memory_mentions", "memory_relations",
                  "memory_relation_evidence", "memory_resolution_decisions"):  # fmt: skip
        assert world.db.one(f"SELECT count(*) FROM {table} WHERE tenant_id <> %s", (TENANT,)) == 0
    # The other tenant's same-named node was never used.
    assert (
        world.db.one("SELECT count(*) FROM memory_mentions WHERE entity_id = %s", (other_alice,))
        == 0
    )
    assert world.db.one("SELECT count(*) FROM memory_entities WHERE tenant_id = %s", (OTHER,)) == 1


# ═══ the migration and older code ════════════════════════════════════════


def test_the_graph_keys_migration_applies_twice(world: World) -> None:
    from robothor.db.migrate import _discover, _strip_outer_transaction

    (m,) = [m for m in _discover() if m.migration_id.endswith("_memory_graph_keys")]
    sql = _strip_outer_transaction(m.path.read_text())
    world.db.q(sql)
    world.db.q(sql)
    assert (
        world.db.one(
            "SELECT count(*) FROM pg_indexes WHERE tablename = 'memory_mentions' "
            "AND indexdef ILIKE '%%UNIQUE%%lower(surface_form)%%'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_older_code_still_writes_against_the_migrated_schema(world: World) -> None:
    """The statements an older release runs, verbatim, against this schema."""
    tenant = TENANT
    upsert = (
        "INSERT INTO memory_entities (name, entity_type, aliases, tenant_id) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, name, entity_type) DO UPDATE SET mention_count = "
        "memory_entities.mention_count + 1, last_seen = NOW() RETURNING id"
    )
    a = world.db.one(upsert, ("Alice Moreno", "person", [], tenant))
    assert world.db.one(upsert, ("Alice Moreno", "person", [], tenant)) == a
    b = world.db.one(upsert, ("Northwind Traders", "organization", [], tenant))
    fid = world.db.one(
        "INSERT INTO memory_facts (fact_text, category, entities, confidence, source_content, "
        "source_type, embedding, metadata, tenant_id, content_hash) VALUES (%s, %s, %s, %s, %s, "
        "%s, %s, %s, %s, %s) ON CONFLICT (tenant_id, content_hash) WHERE is_active = TRUE AND "
        "content_hash IS NOT NULL DO NOTHING RETURNING id",
        ("Alice Moreno works at Northwind Traders.", "project", ["Alice Moreno"], 0.9, "src",
         "note", str(world.emb.vec("x")), "{}", tenant, "legacyhash"),
    )  # fmt: skip
    rel = (
        "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, fact_id, "
        "confidence, tenant_id) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, "
        "source_entity_id, target_entity_id, relation_type) DO UPDATE SET confidence = "
        "GREATEST(memory_relations.confidence, EXCLUDED.confidence) RETURNING id"
    )
    r = world.db.one(rel, (a, b, "works_at", fid, 1.0, tenant))
    assert world.db.one(rel, (a, b, "works_at", fid, 1.0, tenant)) == r
    # The old write_mentions statement (guarded insert, no ON CONFLICT) still works too.
    for _ in range(2):
        world.db.q(
            "INSERT INTO memory_mentions (tenant_id, fact_id, entity_id, surface_form, "
            "entity_type, decision_id) SELECT %s, %s, %s, %s, %s, %s WHERE EXISTS (SELECT 1 FROM "
            "memory_facts WHERE id = %s AND tenant_id = %s) AND NOT EXISTS (SELECT 1 FROM "
            "memory_mentions WHERE fact_id = %s AND lower(surface_form) = lower(%s))",
            (tenant, fid, a, "Alice Moreno", "person", None, fid, tenant, fid, "Alice Moreno"),
        )
    assert world.db.one("SELECT count(*) FROM memory_mentions WHERE fact_id = %s", (fid,)) == 1
    # And the old store_fact call, with none of the new arguments.
    from robothor.memory.facts import store_fact

    new = await store_fact(
        {"fact_text": "Bob Chen joined Project Helios.", "category": "project",
         "entities": ["Bob Chen"], "confidence": 0.8},
        "src", "note", tenant_id=tenant,
    )  # fmt: skip
    (row,) = [f for f in world.db.facts() if f["id"] == new]
    assert (row["scope_kind"], row["origin"]) == ("shared", None)
