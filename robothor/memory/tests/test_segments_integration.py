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
import sys
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

    from robothor.db.connection import _apply_ann_scan_mode

    @contextlib.contextmanager
    def connect():
        conn = psycopg2.connect(dsn)
        # What a pooled checkout does (robothor.db.connection): the iterative
        # ANN scan is the connection's, not each search's.
        _apply_ann_scan_mode(conn)
        try:
            yield conn
        finally:
            conn.close()

    from robothor.crm import dal

    monkeypatch.setattr(segments, "get_connection", connect)
    monkeypatch.setattr(dal, "get_connection", connect)
    cur = db.cursor(cursor_factory=RealDictCursor)
    owners: dict[str, str] = {}
    for tid in (A, B):
        cur.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (tid, tid),
        )
        owners[tid] = _operator(cur, tid)
    # The default reader of every search below is the tenant's operator.
    monkeypatch.setattr(sys.modules[__name__], "OWNERS", owners)
    embedded: list[str] = []

    async def embed_batch(texts: list[str]) -> list[list[float] | None]:
        embedded.extend(texts)
        return [fake_embed(t) for t in texts]

    return SimpleNamespace(
        db=db,
        dsn=dsn,
        cur=cur,
        connect=connect,
        embed=embed_batch,
        seen=embedded,
        owner=owners,
    )


OWNERS: dict[str, str] = {}


def _operator(cur: Any, tenant: str) -> str:
    """The tenant's operator: a person linked from an owner ``tenant_users`` row."""
    cur.execute(
        "INSERT INTO crm_people (tenant_id, first_name, last_name) "
        "VALUES (%s, 'Olive', 'Operator') RETURNING id",
        (tenant,),
    )
    person = str(cur.fetchone()["id"])
    cur.execute(
        "INSERT INTO tenant_users (display_name, tenant_id, role, user_id, person_id) "
        "VALUES ('Olive', %s, 'owner', %s, %s)",
        (tenant, f"owner-{tenant}", person),
    )
    return person


def _session(cur: Any, tenant: str, key: str, person_id: str | None = None) -> int:
    cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel, person_id) "
        "VALUES (%s, %s, 'webchat', %s) RETURNING id",
        (tenant, key, person_id),
    )
    return int(cur.fetchone()["id"])


def _person(cur: Any, first_name: str, tenant: str = A) -> str:
    cur.execute(
        "INSERT INTO crm_people (tenant_id, first_name, last_name) "
        "VALUES (%s, %s, 'Example') RETURNING id",
        (tenant, first_name),
    )
    return str(cur.fetchone()["id"])


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
    """Search as the tenant's operator unless the test names another reader."""
    kw.setdefault("limit", 5)
    kw.setdefault("query_embedding", fake_embed(q))
    if "reader_person_id" not in kw and "reader_user_id" not in kw:
        kw["reader_person_id"] = OWNERS.get(tenant)
    return await segments.search_segments(q, tenant_id=tenant, **kw)


async def _search_as_nobody(q: str, tenant: str = A, **kw: Any) -> list[dict[str, Any]]:
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
        # The main session is the operator's own conversation.
        assert (r["scope_kind"], r["scope_id"]) == ("user", f"person:{pg.owner[A]}")
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
    alice = _person(pg.cur, "Alice")
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
        reader_person_id=pg.owner[A],
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
        "text, embedding, scope_kind) VALUES %s",
        [(*row[:6], row[6]) for row in big + small],
        template="(%s, %s, %s, %s, %s, %s, %s::vector, 'shared')",
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
    sid_old = _session(pg.cur, A, "agent:main:old", person_id=pg.owner[A])
    sid_new = _session(pg.cur, A, "agent:main:new", person_id=pg.owner[A])
    _say(pg.cur, sid_old, BIKE, old_start)
    _say(pg.cur, sid_new, BIKE, new_start)
    for key in ("agent:main:old", "agent:main:new"):
        await segments.index_conversation(key, tenant_id=A, embed_batch=pg.embed)

    cut = datetime.now(UTC) - timedelta(days=10)
    recent = await _search("bike locker code", since=cut)
    older = await _search("bike locker code", until=cut)

    assert {h["source_ref"] for h in recent} == {"agent:main:new"}
    assert {h["source_ref"] for h in older} == {"agent:main:old"}


ROOM = "5d0b7c3a-9e2f-4a1b-8c6d-2f4e6a8b0c1d"
SHARED = f"agent:main:room:{ROOM}"


async def _three_conversations(pg) -> dict[str, str]:
    """Alice's and Bob's own sessions and a workspace's, all on one topic."""
    people = {name: _person(pg.cur, name) for name in ("Alice", "Bob")}
    for key, person in (
        ("agent:main:alice", people["Alice"]),
        ("agent:main:bob", people["Bob"]),
        (SHARED, None),
    ):
        sid = _session(pg.cur, A, key, person_id=person)
        _say(pg.cur, sid, BIKE, _recent())
        await segments.index_conversation(key, tenant_id=A, embed_batch=pg.embed)
    return people


@pytest.mark.integration
async def test_a_persons_session_is_stored_private_to_that_person(pg) -> None:
    people = await _three_conversations(pg)

    scope = {r["source_ref"]: (r["scope_kind"], r["scope_id"]) for r in _rows(pg.cur)}

    assert scope["agent:main:alice"] == ("user", f"person:{people['Alice']}")
    assert scope["agent:main:bob"] == ("user", f"person:{people['Bob']}")
    assert scope[SHARED] == ("shared", None)


@pytest.mark.integration
async def test_a_workspace_session_stays_shared_even_with_a_person(pg) -> None:
    alice = _person(pg.cur, "Alice")
    key = f"agent:main:room:{uuid.uuid4()}"
    sid = _session(pg.cur, A, key, person_id=alice)
    _say(pg.cur, sid, BIKE, _recent())

    await segments.index_conversation(key, tenant_id=A, embed_batch=pg.embed)

    [row] = _rows(pg.cur)
    assert (row["scope_kind"], row["scope_id"]) == ("shared", None)


@pytest.mark.integration
async def test_a_reader_sees_shared_segments_and_only_their_own_private_ones(pg) -> None:
    people = await _three_conversations(pg)

    alice = await _search("bike locker code", reader_person_id=people["Alice"], room_ids=[ROOM])
    bob = await _search("bike locker code", reader_person_id=people["Bob"], room_ids=[ROOM])

    assert {h["source_ref"] for h in alice} == {"agent:main:alice", SHARED}
    assert {h["source_ref"] for h in bob} == {"agent:main:bob", SHARED}


@pytest.mark.integration
async def test_a_reader_with_no_known_person_sees_shared_segments_only(pg) -> None:
    await _three_conversations(pg)

    hits = await _search_as_nobody("bike locker code", room_ids=[ROOM])

    assert {h["source_ref"] for h in hits} == {SHARED}
    assert await _search_as_nobody("bike locker code") == []


@pytest.mark.integration
async def test_a_long_turn_round_trips_completely_across_parts(pg) -> None:
    from robothor.memory.tests.test_segments import long_text

    original = long_text()
    sid = _session(pg.cur, A, "agent:main:primary")
    [ref] = _say(pg.cur, sid, [("user", original)], _recent())

    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)
    pg.cur.execute(
        "SELECT part, start_ref, end_ref, text FROM memory_segments "
        "WHERE tenant_id = %s ORDER BY start_ref, part",
        (A,),
    )
    rows = pg.cur.fetchall()

    assert len(rows) > 1
    assert [r["part"] for r in rows] == list(range(len(rows)))
    assert all((r["start_ref"], r["end_ref"]) == (ref, ref) for r in rows)
    assert all(len(r["text"]) <= segments.SEGMENT_MAX_CHARS for r in rows)
    assert "".join(r["text"].removeprefix("User: ") for r in rows) == original

    again = await segments.index_conversation(
        "agent:main:primary", tenant_id=A, embed_batch=pg.embed
    )
    assert again == 0


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


# ── who may read a conversation, through the real save path ────────────────


async def _wait_for_rows(pg, tenant: str = A, n: int = 1) -> list[dict[str, Any]]:
    for _ in range(200):
        rows = _rows(pg.cur, tenant)
        if len(rows) >= n:
            return rows
        await asyncio.sleep(0.02)
    raise AssertionError("the background indexer wrote nothing")


@pytest.mark.integration
async def test_a_members_webchat_session_is_private_to_that_member(pg, monkeypatch) -> None:
    """Through ``save_exchange_async`` with the key webchat really uses: the
    session row carries no person, and the conversation is still nobody
    else's to read."""
    from unittest.mock import AsyncMock

    from robothor.engine import chat_store
    from robothor.engine.chat import derive_user_session_key

    monkeypatch.setattr(chat_store, "get_connection", pg.connect)
    monkeypatch.setattr(chat_store, "_embed_turns", AsyncMock())
    monkeypatch.setattr(segments, "_default_embed_batch", pg.embed)
    alice_uid, bob_uid = str(uuid.uuid4()), str(uuid.uuid4())
    bob = _person(pg.cur, "Bob")
    key = derive_user_session_key("main", alice_uid)
    for user, assistant in BIKE_EXCHANGES:
        assert await chat_store.save_exchange_async(
            key, user, assistant, channel="webchat", tenant_id=A
        )
        await _wait_for_rows(pg)
    pg.cur.execute("SELECT person_id FROM chat_sessions WHERE session_key = %s", (key,))
    assert pg.cur.fetchone()["person_id"] is None

    rows = _rows(pg.cur)
    assert {(r["scope_kind"], r["scope_id"]) for r in rows} == {("user", f"user:{alice_uid}")}
    assert await _search("bike locker code", reader_user_id=alice_uid)
    assert await _search("bike locker code", reader_user_id=bob_uid, reader_person_id=bob) == []
    assert await _search("bike locker code") == []  # the operator
    assert await _search_as_nobody("bike locker code") == []


BIKE_EXCHANGES = [(BIKE[0][1], BIKE[1][1]), (BIKE[2][1], BIKE[3][1])]


@pytest.mark.integration
async def test_the_main_session_is_private_to_the_operator(pg) -> None:
    alice = _person(pg.cur, "Alice")
    sid = _session(pg.cur, A, "agent:main:primary")
    _say(pg.cur, sid, BIKE, _recent())
    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)

    assert await _search("bike locker code")  # the operator
    assert await _search("bike locker code", reader_person_id=alice) == []
    assert await _search_as_nobody("bike locker code") == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "key", ["telegram:4242", "agent:main:tui-alice", "agent:main:service:report-bot"]
)
async def test_a_session_with_no_known_owner_is_readable_by_no_one(pg, key) -> None:
    alice = _person(pg.cur, "Alice")
    sid = _session(pg.cur, A, key)
    _say(pg.cur, sid, BIKE, _recent())
    await segments.index_conversation(key, tenant_id=A, embed_batch=pg.embed)

    [row] = _rows(pg.cur)
    assert (row["scope_kind"], row["scope_id"]) == ("user", None)
    assert await _search("bike locker code") == []
    assert await _search("bike locker code", reader_person_id=alice) == []
    assert await _search_as_nobody("bike locker code", room_ids=[ROOM]) == []


@pytest.mark.integration
async def test_a_session_that_gains_its_person_rescopes_its_earlier_segments(pg) -> None:
    alice = _person(pg.cur, "Alice")
    sid = _session(pg.cur, A, "telegram:4242")
    start = _recent()
    _say(pg.cur, sid, BIKE, start)
    await segments.index_conversation("telegram:4242", tenant_id=A, embed_batch=pg.embed)
    assert {(r["scope_kind"], r["scope_id"]) for r in _rows(pg.cur)} == {("user", None)}

    pg.cur.execute("UPDATE chat_sessions SET person_id = %s WHERE id = %s", (alice, sid))
    _say(pg.cur, sid, DENTIST, start + timedelta(minutes=10))
    await segments.index_conversation("telegram:4242", tenant_id=A, embed_batch=pg.embed)

    rows = _rows(pg.cur)
    assert len(rows) == 2
    assert {(r["scope_kind"], r["scope_id"]) for r in rows} == {("user", f"person:{alice}")}
    hits = await _search("bike locker code", reader_person_id=alice)
    assert any("4417" in h["text"] for h in hits)


# ── the first index of a long history is bounded ───────────────────────────


@pytest.mark.integration
async def test_a_long_history_is_indexed_in_bounded_runs(pg, monkeypatch) -> None:
    monkeypatch.setattr(segments, "SEGMENT_INDEX_MAX_TURNS", 4)
    sid = _session(pg.cur, A, "agent:main:primary")
    ids = _say(pg.cur, sid, BIKE + DENTIST, _recent())

    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)
    assert max(r["end_ref"] for r in _rows(pg.cur)) <= ids[3]

    await segments.index_conversation("agent:main:primary", tenant_id=A, embed_batch=pg.embed)
    assert [(r["start_ref"], r["end_ref"]) for r in _rows(pg.cur)] == [
        (ids[0], ids[3]),
        (ids[4], ids[7]),
    ]
