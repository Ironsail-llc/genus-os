"""``memory_segments`` on real Postgres: index a conversation, then recall it.

Every test runs in a throwaway database migrated through the segments
migration (the ``scratch_db`` fixture), never in the shared test database.
The module's ``get_connection`` is pointed at that database. Embeddings are the
deterministic bag-of-words fake from ``test_segments``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from robothor.memory import segments
from robothor.memory.tests.test_segments import BIKE, DENTIST, fake_embed

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor, execute_values  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
MIGRATION = "177_memory_segments"
SQL_FILE = REPO / "crm" / "migrations" / f"{MIGRATION}.sql"
MANIFEST = REPO / "robothor" / "migrations" / "manifest.txt"

A = "test-tenant-a"
B = "test-tenant-b"

HIT_KEYS = {
    "id",
    "kind",
    "text",
    "topic",
    "source_kind",
    "source_ref",
    "start_ref",
    "end_ref",
    "started_at",
    "ended_at",
    "similarity",
    "keyword_rank",
    "rrf_score",
}


def test_the_migration_is_in_the_manifest() -> None:
    entries = [line.strip() for line in MANIFEST.read_text().splitlines()]
    assert f"crm/{MIGRATION}.sql" in entries


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def pg(scratch_db, monkeypatch):
    db, dsn = scratch_db(through=MIGRATION)

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(segments, "get_connection", connect)
    cur = db.cursor(cursor_factory=RealDictCursor)
    for tid in (A, B):
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tid, tid),
        )
    embedded: list[str] = []

    async def embed_batch(texts: list[str]) -> list[list[float] | None]:
        embedded.extend(texts)
        return [fake_embed(t) for t in texts]

    return SimpleNamespace(
        db=db, dsn=dsn, cur=cur, connect=connect, embed=embed_batch, seen=embedded
    )


def _session(cur: Any, tenant: str, key: str, person_id: str | None = None) -> int:
    cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel, person_id) "
        "VALUES (%s, %s, 'webchat', %s) RETURNING id",
        (tenant, key, person_id),
    )
    return int(cur.fetchone()["id"])


def _say(cur: Any, session_id: int, pairs, start_at: datetime) -> list[int]:
    ids = []
    for i, (role, text) in enumerate(pairs):
        cur.execute(
            "INSERT INTO chat_messages (session_id, message, created_at) "
            "VALUES (%s, %s::jsonb, %s) RETURNING id",
            (
                session_id,
                json.dumps({"role": role, "content": text}),
                start_at + timedelta(minutes=i),
            ),
        )
        ids.append(int(cur.fetchone()["id"]))
    return ids


def _rows(cur: Any, tenant: str = A) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT id, tenant_id, source_kind, source_ref, start_ref, end_ref, started_at, ended_at, "
        "topic, text, person_id, scope_kind, scope_id, chat_session_id, "
        "embedding IS NOT NULL AS embedded "
        "FROM memory_segments WHERE tenant_id = %s ORDER BY start_ref",
        (tenant,),
    )
    return [dict(r) for r in cur.fetchall()]


def _recent() -> datetime:
    return datetime.now(UTC) - timedelta(hours=3)


async def _search(q: str, tenant: str = A, **kw: Any) -> list[dict[str, Any]]:
    kw.setdefault("limit", 5)
    kw.setdefault("query_embedding", fake_embed(q))
    return await segments.search_segments(q, tenant_id=tenant, **kw)


# ── the migration ──────────────────────────────────────────────────────────


@pytest.mark.integration
def test_the_migration_applies_twice(pg) -> None:
    cur = pg.db.cursor()
    cur.execute(SQL_FILE.read_text())
    cur.execute(SQL_FILE.read_text())
    cur.execute("SELECT to_regclass('public.memory_segments') IS NOT NULL")
    assert cur.fetchone()[0]


@pytest.mark.integration
def test_the_table_is_tenant_policied_and_forced(pg) -> None:
    pg.cur.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'memory_segments'"
    )
    row = pg.cur.fetchone()
    assert row["relrowsecurity"] and row["relforcerowsecurity"]
    pg.cur.execute(
        "SELECT 1 FROM pg_policies WHERE tablename = 'memory_segments' "
        "AND policyname = 'tenant_isolation'"
    )
    assert pg.cur.fetchone()


@pytest.mark.integration
def test_the_tenant_is_required_and_has_no_default(pg) -> None:
    pg.cur.execute(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'memory_segments' AND column_name = 'tenant_id'"
    )
    row = pg.cur.fetchone()
    assert row["is_nullable"] == "NO" and row["column_default"] is None


@pytest.mark.integration
def test_source_kind_and_scope_kind_are_checked(pg) -> None:
    sid = _session(pg.cur, A, "agent:main:primary")
    for column, value in (("source_kind", "fax"), ("scope_kind", "planet")):
        cols = {"source_kind": "conversation", "scope_kind": "shared", column: value}
        with pytest.raises(psycopg2.errors.CheckViolation):
            pg.cur.execute(
                "INSERT INTO memory_segments (tenant_id, source_kind, source_ref, start_ref, "
                "end_ref, text, scope_kind, chat_session_id) "
                "VALUES (%s, %s, 'k', 1, 1, 'x', %s, %s)",
                (A, cols["source_kind"], cols["scope_kind"], sid),
            )


# ── indexing ───────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_two_topics_are_stored_as_two_segments_with_their_turn_ranges(pg) -> None:
    sid = _session(pg.cur, A, "agent:main:primary")
    ids = _say(pg.cur, sid, BIKE + DENTIST, _recent())

    written = await segments.index_conversation(
        "agent:main:primary", tenant_id=A, embed_batch=pg.embed
    )

    rows = _rows(pg.cur)
    assert written == 2
    assert [(r["start_ref"], r["end_ref"]) for r in rows] == [(ids[0], ids[3]), (ids[4], ids[7])]
    for r in rows:
        assert r["source_kind"] == "conversation"
        assert r["source_ref"] == "agent:main:primary"
        assert r["chat_session_id"] == sid
        assert r["scope_kind"] == "shared" and r["scope_id"] is None
        assert r["embedded"]
        assert r["started_at"] <= r["ended_at"]
    assert "4417" in rows[0]["text"] and "Tuesday" in rows[1]["text"]


@pytest.mark.integration
async def test_reindexing_is_idempotent(pg) -> None:
    sid = _session(pg.cur, A, "agent:main:primary")
    _say(pg.cur, sid, BIKE + DENTIST, _recent())

    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)
    before = _rows(pg.cur)
    again = await segments.index_conversation(
        "agent:main:primary", tenant_id=A, embed_batch=pg.embed
    )

    assert again == 0
    assert _rows(pg.cur) == before


@pytest.mark.integration
async def test_new_turns_extend_the_open_segment_then_start_a_new_one(pg) -> None:
    sid = _session(pg.cur, A, "agent:main:primary")
    start = _recent()
    first = _say(pg.cur, sid, BIKE[:2], start)
    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)
    [open_seg] = _rows(pg.cur)
    assert (open_seg["start_ref"], open_seg["end_ref"]) == (first[0], first[1])

    more = _say(pg.cur, sid, BIKE[2:], start + timedelta(minutes=5))
    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)
    [extended] = _rows(pg.cur)
    assert extended["id"] == open_seg["id"]
    assert (extended["start_ref"], extended["end_ref"]) == (first[0], more[-1])
    assert "4417 holds until March" in extended["text"]

    topic_two = _say(pg.cur, sid, DENTIST, start + timedelta(minutes=10))
    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)
    rows = _rows(pg.cur)
    assert [(r["start_ref"], r["end_ref"]) for r in rows] == [
        (first[0], more[-1]),
        (topic_two[0], topic_two[-1]),
    ]
    assert rows[0]["id"] == open_seg["id"]


@pytest.mark.integration
async def test_text_is_redacted_before_it_is_stored_or_embedded(pg) -> None:
    token = "ghp_" + "a1B2" * 9
    sid = _session(pg.cur, A, "agent:main:primary")
    # Written straight to the table, as a row from before the store redacted.
    _say(
        pg.cur,
        sid,
        [
            ("user", f"Here is the deploy token for the bike locker garage repo: {token}"),
            ("assistant", "Thanks, I will use the deploy token for the bike locker garage repo."),
        ],
        _recent(),
    )

    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)

    [row] = _rows(pg.cur)
    assert token not in row["text"]
    assert "deploy token" in row["text"]
    assert pg.seen and all(token not in t for t in pg.seen)


@pytest.mark.integration
async def test_the_segment_carries_the_sessions_person(pg) -> None:
    pg.cur.execute(
        "INSERT INTO crm_people (tenant_id, first_name, last_name) "
        "VALUES (%s, 'Alice', 'Example') RETURNING id",
        (A,),
    )
    alice = str(pg.cur.fetchone()["id"])
    sid = _session(pg.cur, A, "agent:main:alice", person_id=alice)
    _say(pg.cur, sid, BIKE, _recent())

    await segments.index_conversation("agent:main:alice", tenant_id=A, embed_batch=pg.embed)

    assert str(_rows(pg.cur)[0]["person_id"]) == alice


@pytest.mark.integration
async def test_an_unknown_session_indexes_nothing(pg) -> None:
    assert (
        await segments.index_conversation("agent:main:nobody", tenant_id=A, embed_batch=pg.embed)
        == 0
    )


@pytest.mark.integration
async def test_another_tenants_session_of_the_same_key_is_not_indexed(pg) -> None:
    sid_b = _session(pg.cur, B, "agent:main:primary")
    _say(pg.cur, sid_b, BIKE, _recent())

    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)

    assert _rows(pg.cur, A) == [] and _rows(pg.cur, B) == []


# ── searching ──────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_a_detail_said_only_in_conversation_is_found(pg) -> None:
    sid = _session(pg.cur, A, "agent:main:primary")
    ids = _say(pg.cur, sid, BIKE + DENTIST, _recent())
    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)

    hits = await _search("what was the code for the bike locker in the garage?")

    assert hits
    assert "4417" in hits[0]["text"]
    assert (hits[0]["start_ref"], hits[0]["end_ref"]) == (ids[0], ids[3])
    assert hits[0]["keyword_rank"] is not None and hits[0]["similarity"] is not None


@pytest.mark.integration
async def test_each_hit_names_its_source_and_turn_range(pg) -> None:
    sid = _session(pg.cur, A, "agent:main:primary")
    _say(pg.cur, sid, BIKE + DENTIST, _recent())
    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)

    hits = await _search("dentist appointment Tuesday")

    assert hits
    for hit in hits:
        assert set(hit) >= HIT_KEYS
        assert hit["kind"] == "segment"
        assert hit["source_kind"] == "conversation"
        assert hit["source_ref"] == "agent:main:primary"
        assert hit["start_ref"] <= hit["end_ref"]
        assert hit["started_at"] <= hit["ended_at"]
        assert hit["rrf_score"] > 0
    assert hits[0]["topic"]


@pytest.mark.integration
async def test_tenant_a_never_sees_tenant_b(pg) -> None:
    sid_b = _session(pg.cur, B, "agent:main:primary")
    _say(pg.cur, sid_b, BIKE, _recent())
    await segments.index_conversation("agent:main:primary", tenant_id=B, embed_batch=pg.embed)
    assert _rows(pg.cur, B)

    assert await _search("bike locker code", tenant=A) == []
    assert await _search("bike locker code", tenant=B)


@pytest.mark.integration
async def test_a_long_natural_question_still_matches_by_keyword(pg, monkeypatch) -> None:
    sid = _session(pg.cur, A, "agent:main:primary")
    _say(pg.cur, sid, BIKE + DENTIST, _recent())
    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)

    async def down(_query: str) -> list[float]:
        raise httpx.ConnectError("embedding service down")

    monkeypatch.setattr(segments, "_embed_query", down)
    hits = await segments.search_segments(
        "hey, sorry to bother you again, but could you remind me what the locker number thing was",
        tenant_id=A,
        limit=5,
    )

    assert hits and "4417" in hits[0]["text"]
    assert hits[0]["similarity"] is None
    assert hits[0]["keyword_rank"] > 0


@pytest.mark.integration
async def test_the_limit_is_honoured_for_a_small_tenant_beside_a_large_one(pg) -> None:
    # Seeded random vectors: the large tenant's lean hard toward the query, the
    # small tenant's away from it, so every HNSW candidate within the default
    # budget belongs to the large tenant and the tenant filter discards it.
    rng = random.Random(867)

    def vec(lean: float) -> str:
        v = [rng.gauss(0, 1) for _ in range(1024)]
        v[0] += lean
        return "[" + ",".join(f"{x:.5f}" for x in v) + "]"

    big = [(B, "conversation", f"big-{i}", i, i, f"turn {i}", vec(40.0)) for i in range(1500)]
    small = [(A, "conversation", f"small-{i}", i, i, f"turn {i}", vec(-10.0)) for i in range(5)]
    execute_values(
        pg.cur,
        "INSERT INTO memory_segments (tenant_id, source_kind, source_ref, start_ref, end_ref, "
        "text, embedding) VALUES %s",
        [(*row[:6], row[6]) for row in big + small],
        template="(%s, %s, %s, %s, %s, %s, %s::vector)",
    )
    pg.cur.execute("ANALYZE memory_segments")
    # Make the planner walk the HNSW index (the filtered-ANN case) instead of
    # sorting the small tenant's rows: on a real corpus it does.
    pg.cur.execute("SELECT current_database()")
    dbname = pg.cur.fetchone()["current_database"]
    pg.cur.execute(f'ALTER DATABASE "{dbname}" SET enable_sort = off')
    query = [0.0] * 1024
    query[0] = 1.0

    hits = await segments.search_segments("zzz", tenant_id=A, limit=5, query_embedding=query)

    assert len(hits) == 5
    assert {h["source_ref"] for h in hits} == {f"small-{i}" for i in range(5)}


@pytest.mark.integration
async def test_since_and_until_bound_the_event_time(pg) -> None:
    old_start = datetime.now(UTC) - timedelta(days=20)
    new_start = _recent()
    sid_old = _session(pg.cur, A, "agent:main:old")
    sid_new = _session(pg.cur, A, "agent:main:new")
    _say(pg.cur, sid_old, BIKE, old_start)
    _say(pg.cur, sid_new, BIKE, new_start)
    for key in ("agent:main:old", "agent:main:new"):
        await segments.index_conversation(key, tenant_id=A, embed_batch=pg.embed)

    cut = datetime.now(UTC) - timedelta(days=10)
    recent = await _search("bike locker code", since=cut)
    older = await _search("bike locker code", until=cut)

    assert {h["source_ref"] for h in recent} == {"agent:main:new"}
    assert {h["source_ref"] for h in older} == {"agent:main:old"}


@pytest.mark.integration
async def test_a_restricted_person_scope_sees_its_own_and_shared_segments(pg) -> None:
    from robothor.identity.scope import DataScope

    people = {}
    for name in ("Alice", "Bob"):
        pg.cur.execute(
            "INSERT INTO crm_people (tenant_id, first_name, last_name) "
            "VALUES (%s, %s, 'Example') RETURNING id",
            (A, name),
        )
        people[name] = str(pg.cur.fetchone()["id"])
    for key, person in (
        ("agent:main:alice", people["Alice"]),
        ("agent:main:bob", people["Bob"]),
        ("agent:main:shared", None),
    ):
        sid = _session(pg.cur, A, key, person_id=person)
        _say(pg.cur, sid, BIKE, _recent())
        await segments.index_conversation(key, tenant_id=A, embed_batch=pg.embed)

    scope = DataScope(tenant_id=A, person_id=people["Alice"], restricted=True)
    hits = await _search("bike locker code", person_scope=scope)

    assert {h["source_ref"] for h in hits} == {"agent:main:alice", "agent:main:shared"}
    unrestricted = DataScope(tenant_id=A, person_id=None, restricted=False)
    assert len(await _search("bike locker code", person_scope=unrestricted)) == 3


@pytest.mark.integration
async def test_a_workspace_conversation_is_searched_only_for_its_members(pg) -> None:
    room = str(uuid.uuid4())
    key = f"agent:main:room:{room}"
    sid = _session(pg.cur, A, key)
    _say(pg.cur, sid, BIKE, _recent())
    await segments.index_conversation(key, tenant_id=A, embed_batch=pg.embed)

    assert await _search("bike locker code") == []
    assert await _search("bike locker code", room_ids=[str(uuid.uuid4())]) == []
    hits = await _search("bike locker code", room_ids=[room])
    assert [h["source_ref"] for h in hits] == [key]


# ── retention ──────────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_segments_past_retention_are_pruned_for_that_tenant_only(pg) -> None:
    old = datetime.now(UTC) - timedelta(days=segments.SEGMENT_RETENTION_DAYS + 5)
    for tenant in (A, B):
        sid = _session(pg.cur, tenant, "agent:main:primary")
        _say(pg.cur, sid, BIKE, _recent())
        await segments.index_conversation(
            "agent:main:primary", tenant_id=tenant, embed_batch=pg.embed
        )
        pg.cur.execute(
            "INSERT INTO memory_segments (tenant_id, source_kind, source_ref, start_ref, end_ref, "
            "started_at, ended_at, text, chat_session_id) "
            "VALUES (%s, 'conversation', 'agent:main:primary', 0, 0, %s, %s, 'old', %s)",
            (tenant, old, old, sid),
        )

    pruned = await asyncio.to_thread(
        segments.cleanup_stale_segments, segments.SEGMENT_RETENTION_DAYS, A
    )

    assert pruned == 1
    assert [r["text"] for r in _rows(pg.cur, A) if r["text"] == "old"] == []
    assert len(_rows(pg.cur, A)) == 1
    assert len(_rows(pg.cur, B)) == 2


@pytest.mark.integration
async def test_turns_past_retention_are_not_indexed(pg) -> None:
    sid = _session(pg.cur, A, "agent:main:primary")
    _say(pg.cur, sid, BIKE, datetime.now(UTC) - timedelta(days=segments.SEGMENT_RETENTION_DAYS + 1))
    recent = _say(pg.cur, sid, DENTIST, _recent())

    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)

    assert [(r["start_ref"], r["end_ref"]) for r in _rows(pg.cur)] == [(recent[0], recent[-1])]


@pytest.mark.integration
async def test_deleting_the_conversation_deletes_its_segments(pg) -> None:
    sid = _session(pg.cur, A, "agent:main:primary")
    _say(pg.cur, sid, BIKE, _recent())
    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)
    assert _rows(pg.cur)

    pg.cur.execute("DELETE FROM chat_sessions WHERE id = %s", (sid,))

    assert _rows(pg.cur) == []


# ── a workload move takes its segments along ───────────────────────────────


@pytest.mark.integration
async def test_segments_move_with_their_conversation(pg, monkeypatch) -> None:
    from robothor.tenancy import move_workload as mw

    child = "test-tenant-a-child"
    pg.cur.execute(
        "INSERT INTO crm_tenants (id, display_name, parent_tenant_id) VALUES (%s, %s, %s)",
        (child, child, A),
    )
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", A)
    monkeypatch.setattr(mw, "get_connection", pg.connect)
    moving = _session(pg.cur, A, "agent:alice-agent:primary")
    staying = _session(pg.cur, A, "agent:bob-agent:primary")
    _say(pg.cur, moving, BIKE, _recent())
    _say(pg.cur, staying, DENTIST, _recent())
    for key in ("agent:alice-agent:primary", "agent:bob-agent:primary"):
        await segments.index_conversation(key, tenant_id=A, embed_batch=pg.embed)

    report = await asyncio.to_thread(
        mw.move_workload,
        from_tenant=A,
        to_tenant=child,
        agents=["alice-agent"],
        workflows=[],
        actor="test",
    )

    assert report.counts["memory_segments"] == 1
    assert [r["source_ref"] for r in _rows(pg.cur, child)] == ["agent:alice-agent:primary"]
    assert [r["source_ref"] for r in _rows(pg.cur, A)] == ["agent:bob-agent:primary"]
