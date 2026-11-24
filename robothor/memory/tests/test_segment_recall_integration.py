"""Conversation segments as a recall leg: ``search_facts`` and ``search_memory``.

A detail said in conversation but never extracted as a fact must come back
from the recall path agents actually use, tagged as a segment with its source
and turn range, and only to readers allowed to see it: a person's own
conversation is private to that person whatever the data-scoping rollout says,
and a workspace conversation reaches only that workspace's members.

Every test runs in a scratch database migrated through the segments
migration. Embeddings are the deterministic bag-of-words fake; the reranker is
off unless a test replaces it.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from robothor.memory import segments
from robothor.memory.tests.test_segments import BIKE, DENTIST, fake_embed

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

MIGRATION = "177_memory_segments"
A = "test-tenant-a"
B = "test-tenant-b"
QUESTION = "what was the code for the bike locker in the garage?"

_PATCHED_MODULES = (
    "robothor.memory.facts",
    "robothor.memory.segments",
    "robothor.memory.episodes",
    "robothor.memory.outcomes",
    "robothor.rooms.dal",
)


@pytest.fixture
def pg(scratch_db, monkeypatch):
    import importlib

    db, dsn = scratch_db(through=MIGRATION)

    @contextlib.contextmanager
    def connect(*_a: Any, **_kw: Any):
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    for name in _PATCHED_MODULES:
        module = importlib.import_module(name)
        if hasattr(module, "get_connection"):
            monkeypatch.setattr(module, "get_connection", connect)

    async def embed_query(query: str) -> list[float]:
        return fake_embed(query)

    from robothor.memory import facts

    monkeypatch.setattr(facts, "_embed_query", embed_query)
    monkeypatch.setattr(segments, "_embed_query", embed_query)
    monkeypatch.setenv("MEMORY_RERANK_ENABLED", "0")
    monkeypatch.delenv("ROBOTHOR_DATA_SCOPING", raising=False)
    monkeypatch.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)

    cur = db.cursor(cursor_factory=RealDictCursor)
    for tid in (A, B):
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tid, tid),
        )
    # Segment ids far from fact ids, so an id list cannot confuse the two.
    cur.execute("SELECT setval('memory_segments_id_seq', 100000)")

    async def embed_batch(texts: list[str]) -> list[list[float] | None]:
        return [fake_embed(t) for t in texts]

    return SimpleNamespace(db=db, cur=cur, embed=embed_batch)


def _person(cur: Any, first_name: str, tenant: str = A) -> str:
    cur.execute(
        "INSERT INTO crm_people (tenant_id, first_name, last_name) "
        "VALUES (%s, %s, 'Example') RETURNING id",
        (tenant, first_name),
    )
    return str(cur.fetchone()["id"])


async def _conversation(
    pg, key: str, pairs=BIKE, *, tenant: str = A, person: str | None = None
) -> list[int]:
    pg.cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel, person_id) "
        "VALUES (%s, %s, 'webchat', %s) RETURNING id",
        (tenant, key, person),
    )
    sid = int(pg.cur.fetchone()["id"])
    start = datetime.now(UTC) - timedelta(hours=3)
    ids = []
    for i, (role, text) in enumerate(pairs):
        pg.cur.execute(
            "INSERT INTO chat_messages (session_id, message, created_at) "
            "VALUES (%s, %s::jsonb, %s) RETURNING id",
            (sid, json.dumps({"role": role, "content": text}), start + timedelta(minutes=i)),
        )
        ids.append(int(pg.cur.fetchone()["id"]))
    await segments.index_conversation(key, tenant_id=tenant, embed_batch=pg.embed)
    return ids


def _fact(pg, text: str, tenant: str = A) -> int:
    pg.cur.execute(
        "INSERT INTO memory_facts (fact_text, category, tenant_id, is_active, importance_score, "
        "embedding) VALUES (%s, 'event', %s, TRUE, 0.5, %s::vector) RETURNING id",
        (text, tenant, str(fake_embed(text))),
    )
    return int(pg.cur.fetchone()["id"])


def _segments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("kind") == "segment"]


async def _search_facts(query: str = QUESTION, **kw: Any) -> list[dict[str, Any]]:
    from robothor.memory.facts import search_facts

    kw.setdefault("tenant_id", A)
    kw.setdefault("use_reranker", False)
    return await search_facts(query, limit=10, **kw)


def _ctx(*, person: str | None = None, user_id: str = "", run_id: str = "", tenant: str = A) -> Any:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.identity.context import IdentityContext

    identity = None
    if person is not None:
        identity = IdentityContext(
            tenant_id=tenant,
            channel="webchat",
            identifier="agent@example.com",
            verified=True,
            role="member",
            person_id=person,
        )
    return ToolContext(
        agent_id="main", run_id=run_id, tenant_id=tenant, user_id=user_id, identity=identity
    )


async def _search_memory(ctx: Any, query: str = QUESTION) -> list[dict[str, Any]]:
    from robothor.engine.tools.handlers.memory import HANDLERS

    out = await HANDLERS["search_memory"](
        {
            "query": query,
            "limit": 10,
            "expand_entities": False,
            "include_insights": False,
            "include_episodes": False,
        },
        ctx,
    )
    return out["results"]


# ── search_facts ───────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_a_detail_said_only_in_conversation_is_recalled_by_search_facts(pg) -> None:
    ids = await _conversation(pg, "agent:main:shared", BIKE + DENTIST)
    _fact(pg, "The team prefers short standups on Mondays.")

    rows = await _search_facts()

    hits = _segments(rows)
    assert hits, [r.get("kind") for r in rows]
    top = hits[0]
    assert "4417" in top["fact_text"]
    assert top["source"] == "segment"
    assert top["source_ref"] == "agent:main:shared"
    assert (top["start_ref"], top["end_ref"]) == (ids[0], ids[3])
    assert top["started_at"] <= top["ended_at"]
    assert top["topic"]
    assert "segment" in top["leg_ranks"]


@pytest.mark.integration
async def test_include_segments_false_returns_no_segment(pg) -> None:
    await _conversation(pg, "agent:main:shared")

    assert _segments(await _search_facts(include_segments=False)) == []


@pytest.mark.integration
async def test_search_facts_never_returns_another_tenants_segment(pg) -> None:
    await _conversation(pg, "agent:main:shared", tenant=B)

    assert _segments(await _search_facts(tenant_id=A)) == []
    assert _segments(await _search_facts(tenant_id=B))


@pytest.mark.integration
async def test_the_segment_leg_replaces_the_chat_turn_leg(pg, monkeypatch) -> None:
    from robothor.engine import chat_store

    calls: list[Any] = []

    def turns(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        calls.append(kw)
        return []

    monkeypatch.setattr(chat_store, "search_chat_turns", turns)
    await _conversation(pg, "agent:main:shared")

    await _search_facts(include_chat_turns=True)
    assert calls == []

    await _search_facts(include_chat_turns=True, include_segments=False)
    assert len(calls) == 1


@pytest.mark.integration
async def test_search_facts_keeps_private_segments_to_their_reader(pg) -> None:
    alice, bob = _person(pg.cur, "Alice"), _person(pg.cur, "Bob")
    await _conversation(pg, "agent:main:alice", person=alice)

    assert _segments(await _search_facts(reader_person_id=alice))
    assert _segments(await _search_facts(reader_person_id=bob)) == []
    assert _segments(await _search_facts()) == []


@pytest.mark.integration
async def test_the_reranker_reads_a_bounded_excerpt_not_the_stored_text(pg, monkeypatch) -> None:
    from robothor.rag import reranker

    long_topic = [
        (
            "user" if i % 2 == 0 else "assistant",
            f"Note {i}: the garage bike locker rota, bike locker cleaning and the bike locker "
            "code process for the garage are discussed again here at some length today.",
        )
        for i in range(14)
    ]
    long_topic.append(("user", "And the bike locker code in the garage is 4417, right?"))
    await _conversation(pg, "agent:main:shared", long_topic)
    seen: list[dict[str, Any]] = []

    async def capture(query: str, pool: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        seen.extend(pool)
        for r in pool:
            r["rerank_score"] = 0.9
        return pool[:top_k]

    monkeypatch.setattr(reranker, "rerank_with_fallback", capture)

    rows = await _search_facts(use_reranker=True)

    pooled = _segments(seen)
    assert pooled
    assert all(len(r["content"]) <= segments.SEGMENT_RERANK_CHARS for r in pooled)
    assert any(len(r["fact_text"]) > segments.SEGMENT_RERANK_CHARS for r in _segments(rows))


# ── the search_memory tool ─────────────────────────────────────────────────


@pytest.mark.integration
async def test_a_detail_said_only_in_conversation_is_recalled_by_search_memory(pg) -> None:
    ids = await _conversation(pg, "agent:main:shared", BIKE + DENTIST)

    results = await _search_memory(_ctx())

    hits = [r for r in results if r.get("kind") == "segment"]
    assert hits
    top = hits[0]
    assert "4417" in top["fact"]
    assert top["source"] == "segment"
    assert top["source_ref"] == "agent:main:shared"
    assert (top["start_ref"], top["end_ref"]) == (ids[0], ids[3])
    assert top["topic"] and top["started_at"] and top["ended_at"]


@pytest.mark.integration
@pytest.mark.parametrize("routed", [False, True], ids=["fallback", "rip15-routed"])
@pytest.mark.parametrize("scoping", ["off", "enforce"])
async def test_reader_a_never_receives_reader_bs_private_segment(
    pg, monkeypatch, routed, scoping
) -> None:
    monkeypatch.setenv("ROBOTHOR_DATA_SCOPING", scoping)
    if routed:
        monkeypatch.setenv("ROBOTHOR_RIP_15_ENABLED", "1")
    alice, bob = _person(pg.cur, "Alice"), _person(pg.cur, "Bob")
    await _conversation(pg, "agent:main:bob", person=bob)

    as_alice = await _search_memory(_ctx(person=alice))
    as_nobody = await _search_memory(_ctx())
    as_bob = await _search_memory(_ctx(person=bob))

    assert [r for r in as_alice if r.get("kind") == "segment"] == []
    assert [r for r in as_nobody if r.get("kind") == "segment"] == []
    assert [r["source_ref"] for r in as_bob if r.get("kind") == "segment"] == ["agent:main:bob"]


@pytest.mark.integration
async def test_a_workspace_segment_reaches_only_that_workspaces_members(pg) -> None:
    member, outsider = str(uuid.uuid4()), str(uuid.uuid4())
    room = str(uuid.uuid4())
    key = f"agent:main:room:{room}"
    pg.cur.execute(
        "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by) "
        "VALUES (%s, %s, 'agent', 'Garage', 'main', %s, %s)",
        (A, room, key, member),
    )
    pg.cur.execute(
        "INSERT INTO room_members (tenant_id, room_id, user_id) VALUES (%s, %s, %s)",
        (A, room, member),
    )
    await _conversation(pg, key)

    inside = await _search_memory(_ctx(user_id=member))
    outside = await _search_memory(_ctx(user_id=outsider))

    assert [r["source_ref"] for r in inside if r.get("kind") == "segment"] == [key]
    assert [r for r in outside if r.get("kind") == "segment"] == []


@pytest.mark.integration
async def test_a_segment_row_is_not_recorded_as_fact_access(pg, monkeypatch) -> None:
    from robothor.memory import outcomes

    logged: list[list[Any]] = []
    monkeypatch.setattr(
        outcomes, "log_fact_access", lambda _run, ids, *_a, **_kw: logged.append(list(ids))
    )
    fact_id = _fact(pg, "The bike locker garage rota changes every quarter.")
    await _conversation(pg, "agent:main:shared")

    results = await _search_memory(_ctx(run_id="run-1"))

    assert any(r.get("kind") == "segment" for r in results)
    assert logged and all(set(ids) <= {fact_id} for ids in logged)
    seg_ids = {r["id"] for r in results if r.get("kind") == "segment"}
    assert not seg_ids & {i for ids in logged for i in ids}
