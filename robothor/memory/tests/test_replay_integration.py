"""The replay dry run against real Postgres: it reads, and only reads, one tenant.

Every test runs in a scratch database migrated through the whole chain of this
tree, reached through the platform's own connection pool, so the read-only
switch under test is the one ``get_connection`` applies. Embeddings are the
deterministic bag-of-words fake and the reranker a deterministic word-overlap
scorer: no test reaches a model.

The acceptance criteria proven here:

* a replay of recent recall questions writes nothing to any memory table:
  row counts and a checksum of every row of every ``memory_*`` and
  ``fact_access_*`` table are equal before and after;
* a write attempted inside the replay fails loudly (read-only connections);
* only the given tenant is read, though another tenant holds the same text;
* each question is read as its own reader: Alice's question never shows
  Bob's private fact or conversation, and the other way round.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from robothor.memory.tests.test_segments import fake_embed

psycopg2 = pytest.importorskip("psycopg2")

pytestmark = pytest.mark.integration

A = "test-tenant-a"
B = "test-tenant-b"
QUESTION = "what is the bike locker code?"

SHARED_FACT = "The garage bike locker code is 4417"
ALICE_FACT = "Alice keeps the bike locker spare key in the red drawer"
BOB_FACT = "Bob set the bike locker combination to 9921"
ALICE_SEGMENT = "[user] note for me: my office bike locker code is 5150"
BOB_SEGMENT = "[user] note for me: my gym bike locker code is 8008"
B_ONLY_FACT = "Tenant B keeps its bike locker code in the binder"


@pytest.fixture
def pg(scratch_db, monkeypatch):
    """A fully migrated scratch database behind the real pool, models faked."""
    from robothor import config
    from robothor.db import connection
    from robothor.db.migrate import _discover

    db, dsn = scratch_db(through=list(_discover())[-1].migration_id)
    db.autocommit = True
    parts = dict(psycopg2.extensions.parse_dsn(dsn))
    monkeypatch.setenv("ROBOTHOR_DB_NAME", parts["dbname"])
    monkeypatch.setenv("ROBOTHOR_TEST_DB_ALLOW", parts["dbname"])
    for key, env in (
        ("host", "ROBOTHOR_DB_HOST"),
        ("port", "ROBOTHOR_DB_PORT"),
        ("user", "ROBOTHOR_DB_USER"),
        ("password", "ROBOTHOR_DB_PASSWORD"),
    ):
        if key in parts:
            monkeypatch.setenv(env, parts[key])
        else:
            monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("ROBOTHOR_RLS_ENABLED", raising=False)
    monkeypatch.delenv("ROBOTHOR_DATA_SCOPING", raising=False)
    monkeypatch.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)
    monkeypatch.delenv("MEMORY_NARROW_SEARCH", raising=False)
    monkeypatch.setenv("MEMORY_RERANK_ENABLED", "1")
    connection.close_pool()
    config.reset_config()

    from robothor.llm import ollama
    from robothor.memory import facts, segments
    from robothor.rag import reranker

    async def embed(text: str, *_a: Any, **_k: Any) -> list[float]:
        return fake_embed(text)

    async def embed_batch(texts: list[str], *_a: Any, **_k: Any) -> list[list[float]]:
        return [fake_embed(t) for t in texts]

    async def rerank(query: str, results: list[dict[str, Any]], top_k: int = 10) -> list[Any]:
        terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        for row in results:
            words = set(re.findall(r"[a-z0-9]+", str(row.get("content", "")).lower()))
            row["rerank_score"] = len(terms & words) / (len(terms) or 1)
        ordered = sorted(results, key=lambda r: (-r["rerank_score"], str(r.get("id"))))
        return ordered[:top_k]

    monkeypatch.setattr(ollama, "get_embedding_async", embed)
    monkeypatch.setattr(ollama, "get_embeddings_batch_async", embed_batch)
    monkeypatch.setattr(facts, "_embed_query", embed)
    monkeypatch.setattr(segments, "_embed_query", embed)
    monkeypatch.setattr(reranker, "rerank", rerank)

    yield db

    connection.close_pool()
    config.reset_config()


def _sql(db: Any, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with db.cursor() as cur:
        cur.execute(statement, params)
        return cur.fetchall() if cur.description else []


def _one(db: Any, statement: str, params: tuple[Any, ...] = ()) -> Any:
    return _sql(db, statement, params)[0][0]


def _vec(text: str) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in fake_embed(text)) + "]"


def _fact(db: Any, tenant: str, text: str, scope_id: str | None = None) -> int:
    return int(
        _one(
            db,
            "INSERT INTO memory_facts (fact_text, category, tenant_id, embedding, entities, "
            "importance_score, scope_kind, scope_id) "
            "VALUES (%s, 'personal', %s, %s::vector, %s, 0.8, %s, %s) RETURNING id",
            (
                text,
                tenant,
                _vec(text),
                ["bike locker"],
                "user" if scope_id else "shared",
                scope_id,
            ),
        )
    )


def _segment(db: Any, tenant: str, session_key: str, text: str, scope_id: str) -> int:
    now = datetime.now(UTC)
    return int(
        _one(
            db,
            "INSERT INTO memory_segments (tenant_id, source_kind, source_ref, start_ref, "
            "end_ref, started_at, ended_at, topic, text, embedding, scope_kind, scope_id) "
            "VALUES (%s, 'conversation', %s, 1, 2, %s, %s, 'locker', %s, %s::vector, "
            "'user', %s) RETURNING id",
            (tenant, session_key, now - timedelta(days=1), now, text, _vec(text), scope_id),
        )
    )


def _session(db: Any, tenant: str, key: str, person: str | None, turns: list[tuple]) -> None:
    sid = _one(
        db,
        "INSERT INTO chat_sessions (tenant_id, session_key, channel, person_id) "
        "VALUES (%s, %s, 'webchat', %s) RETURNING id",
        (tenant, key, person),
    )
    for role, content in turns:
        _sql(
            db,
            "INSERT INTO chat_messages (session_id, message) VALUES (%s, %s)",
            (sid, json.dumps({"role": role, "content": content})),
        )


def _run(db: Any, tenant: str, person: str | None, steps: list[tuple]) -> str:
    run_id = str(uuid.uuid4())
    _sql(
        db,
        "INSERT INTO agent_runs (id, tenant_id, agent_id, trigger_type, status, person_id) "
        "VALUES (%s, %s, 'main', 'manual', 'completed', %s)",
        (run_id, tenant, person),
    )
    for n, (tool, tool_input, age_days) in enumerate(steps, start=1):
        _sql(
            db,
            "INSERT INTO agent_run_steps (run_id, step_number, step_type, tool_name, "
            "tool_input, created_at) VALUES (%s, %s, 'tool_call', %s, %s, %s)",
            (
                run_id,
                n,
                tool,
                json.dumps(tool_input),
                datetime.now(UTC) - timedelta(days=age_days),
            ),
        )
    return run_id


@pytest.fixture
def world(pg) -> dict[str, Any]:
    """Two tenants holding the same shared text; two private readers in A."""
    db = pg
    for t in (A, B):
        _sql(db, "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)", (t, t))
    alice = _one(
        db,
        "INSERT INTO crm_people (first_name, last_name, tenant_id) "
        "VALUES ('Alice', 'Example', %s) RETURNING id",
        (A,),
    )
    bob = _one(
        db,
        "INSERT INTO crm_people (first_name, last_name, tenant_id) "
        "VALUES ('Bob', 'Example', %s) RETURNING id",
        (A,),
    )
    alice, bob = str(alice), str(bob)
    ids: dict[str, Any] = {"alice": alice, "bob": bob}
    ids["shared_a"] = _fact(db, A, SHARED_FACT)
    ids["alice_fact"] = _fact(db, A, ALICE_FACT, f"person:{alice}")
    ids["bob_fact"] = _fact(db, A, BOB_FACT, f"person:{bob}")
    ids["shared_b"] = _fact(db, B, SHARED_FACT)
    ids["b_only"] = _fact(db, B, B_ONLY_FACT)
    ids["alice_seg"] = _segment(db, A, "agent:main:webchat:alice", ALICE_SEGMENT, f"person:{alice}")
    ids["bob_seg"] = _segment(db, A, "agent:main:webchat:bob", BOB_SEGMENT, f"person:{bob}")
    ids["b_seg"] = _segment(db, B, "agent:main:webchat:b", ALICE_SEGMENT, "person:nobody")

    # The graph and identity tables, so the checksum covers rows a replay
    # could plausibly touch (relation weight, mentions, decisions).
    locker = _one(
        db,
        "INSERT INTO memory_entities (name, entity_type, tenant_id) "
        "VALUES ('bike locker', 'object', %s) RETURNING id",
        (A,),
    )
    garage = _one(
        db,
        "INSERT INTO memory_entities (name, entity_type, tenant_id) "
        "VALUES ('garage', 'location', %s) RETURNING id",
        (A,),
    )
    rel = _one(
        db,
        "INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, "
        "fact_id, tenant_id) VALUES (%s, %s, 'located_in', %s, %s) RETURNING id",
        (locker, garage, ids["shared_a"], A),
    )
    _sql(
        db,
        "INSERT INTO memory_relation_evidence (tenant_id, relation_id, fact_id) VALUES (%s, %s, %s)",
        (A, rel, ids["shared_a"]),
    )
    decision = _one(
        db,
        "INSERT INTO memory_resolution_decisions (tenant_id, surface_form, resolved_entity_id, "
        "method) VALUES (%s, 'bike locker', %s, 'exact') RETURNING id",
        (A, locker),
    )
    _sql(
        db,
        "INSERT INTO memory_mentions (tenant_id, fact_id, entity_id, surface_form, decision_id) "
        "VALUES (%s, %s, %s, 'bike locker', %s)",
        (A, ids["shared_a"], locker, decision),
    )
    _sql(
        db,
        "INSERT INTO fact_access_log (run_id, agent_id, tenant_id, fact_id) "
        "VALUES ('earlier-run', 'main', %s, %s)",
        (A, ids["shared_a"]),
    )

    _session(
        db,
        A,
        "agent:main:webchat:alice",
        alice,
        [("user", "what is my bike locker code?"), ("assistant", "Let me check.")],
    )
    _session(db, A, "agent:main:webchat:bob", bob, [("user", "remind me of my bike locker code")])
    _session(db, A, "tui:local", None, [("user", "ownerless question about the bike locker")])
    _session(db, B, "agent:main:webchat:b", None, [("user", "tenant B question about lockers")])

    ids["run_alice"] = _run(
        db,
        A,
        alice,
        [
            ("search_memory", {"query": "bike locker spare key", "limit": 5}, 0),
            (
                "tool_call",
                {"name": "search_memory", "arguments": json.dumps({"query": "locker combination"})},
                0,
            ),
            ("store_memory", {"content": "not a question"}, 0),
            ("search_memory", {"query": "an old question about the garage"}, 30),
        ],
    )
    ids["run_bob"] = _run(db, A, bob, [("search_memory", {"query": "gym bike locker code"}, 1)])
    _run(db, B, None, [("search_memory", {"query": "tenant B question about binders"}, 0)])
    return ids


def _memory_snapshot(db: Any) -> dict[str, tuple[int, str]]:
    """Row count and an md5 over every row of every memory table."""
    tables = [
        r[0]
        for r in _sql(
            db,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
            "AND table_type = 'BASE TABLE' "
            "AND (table_name LIKE 'memory\\_%%' OR table_name LIKE 'fact\\_access\\_%%') "
            "ORDER BY 1",
        )
    ]
    assert {"memory_facts", "memory_segments", "fact_access_log", "memory_mentions"} <= set(tables)
    out: dict[str, tuple[int, str]] = {}
    for table in tables:
        count, digest = _sql(
            db,
            f"SELECT count(*), coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '') "  # noqa: S608
            f"FROM {table} t",
        )[0]
        out[table] = (int(count), str(digest))
    return out


async def _replay(tenant: str, questions: list[Any], **kwargs: Any) -> dict[str, Any]:
    from robothor.memory import replay

    return await replay.run_replay(tenant, questions, **kwargs)


def _keys(entry: dict[str, Any]) -> set[str]:
    return {r["key"] for side in entry["sides"].values() for r in side["results"]}


# --------------------------------------------------------------------------- #
# Acceptance 2: it writes nothing to memory tables (access counts included)
# --------------------------------------------------------------------------- #


async def test_a_replay_writes_nothing_to_any_memory_table(world, pg) -> None:
    from robothor.memory import replay

    before = _memory_snapshot(pg)
    from_runs = replay.questions_from_runs(A)
    from_chat = replay.questions_from_chat(A)
    reports = [
        await _replay(A, from_runs),
        await _replay(A, from_chat, compare=("new+graph", "new-segments")),
        await _replay(A, [replay.Question(QUESTION)], compare=("today", "new+graph")),
    ]
    after = _memory_snapshot(pg)

    assert after == before
    # Not vacuous: the replays found the seeded rows.
    assert all(any(_keys(e) for e in r["questions"]) for r in reports)
    access = _sql(pg, "SELECT access_count FROM memory_facts WHERE tenant_id = %s", (A,))
    assert {row[0] for row in access} == {0}


async def test_a_write_inside_the_replay_fails_loudly(world, pg) -> None:
    from robothor.db.connection import get_connection, read_only_scope
    from robothor.memory import replay
    from robothor.memory.outcomes import record_fact_access

    async def writing_search(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE memory_facts SET access_count = access_count + 1")
        return []

    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        await replay.run_replay(A, [replay.Question(QUESTION)], search=writing_search)

    # The read-only switch also holds for an autocommit (read_only=True) checkout
    # and for the access recorder the tool handler schedules.
    with read_only_scope():
        with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            with get_connection(read_only=True) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM fact_access_log")
        record_fact_access(A, fact_ids=[world["shared_a"]])

    assert _one(pg, "SELECT max(access_count) FROM memory_facts") == 0
    assert _one(pg, "SELECT count(*) FROM fact_access_log") == 1
    # The pool hands back writable connections once the replay is over.
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SHOW default_transaction_read_only")
        assert cur.fetchone()[0] == "off"


# --------------------------------------------------------------------------- #
# Acceptance 3: only the given tenant's data is read
# --------------------------------------------------------------------------- #


async def test_only_the_given_tenant_is_read(world, pg) -> None:
    from robothor.memory import replay

    questions = replay.questions_from_runs(A) + replay.questions_from_chat(A)
    texts = " ".join(q.text for q in questions)
    assert "tenant B" not in texts

    report = await _replay(A, questions + [replay.Question(QUESTION)])
    found = set().union(*(_keys(e) for e in report["questions"]))
    a_facts = {
        f"fact:{i}" for (i,) in _sql(pg, "SELECT id FROM memory_facts WHERE tenant_id=%s", (A,))
    }
    a_segs = {
        f"segment:{i}"
        for (i,) in _sql(pg, "SELECT id FROM memory_segments WHERE tenant_id=%s", (A,))
    }
    assert found and found <= a_facts | a_segs
    assert f"fact:{world['shared_b']}" not in found
    assert f"fact:{world['b_only']}" not in found
    assert B_ONLY_FACT not in json.dumps(report, default=str)


def test_an_unknown_tenant_is_refused(world) -> None:
    from robothor.memory import replay

    replay.require_tenant(A)
    for bad in ("no-such-tenant", ""):
        with pytest.raises(LookupError):
            replay.require_tenant(bad)


# --------------------------------------------------------------------------- #
# Privacy: each question is read as its own reader
# --------------------------------------------------------------------------- #


async def test_a_chat_turn_is_replayed_as_its_own_person(world, pg) -> None:
    from robothor.memory import replay

    questions = replay.questions_from_chat(A)
    by_reader = {q.reader.person_id: q for q in questions}
    assert set(by_reader) == {world["alice"], world["bob"]}  # the ownerless turn is skipped

    report = await _replay(A, questions, compare=("today", "new"))
    entries = {e["reader"]["person_id"]: e for e in report["questions"]}
    alice, bob = _keys(entries[world["alice"]]), _keys(entries[world["bob"]])

    assert f"fact:{world['alice_fact']}" in alice
    assert f"segment:{world['alice_seg']}" in alice
    assert f"fact:{world['bob_fact']}" not in alice
    assert f"segment:{world['bob_seg']}" not in alice
    assert f"fact:{world['bob_fact']}" in bob
    assert f"segment:{world['bob_seg']}" in bob
    assert f"fact:{world['alice_fact']}" not in bob
    assert f"segment:{world['alice_seg']}" not in bob
    alice_text = json.dumps(entries[world["alice"]], default=str)
    assert "9921" not in alice_text and "8008" not in alice_text


async def test_a_question_with_no_reader_sees_shared_rows_only(world, pg) -> None:
    from robothor.memory import replay

    report = await _replay(A, [replay.Question(QUESTION)], compare=("today", "new+graph"))
    keys = _keys(report["questions"][0])
    assert f"fact:{world['shared_a']}" in keys
    private = {world["alice_fact"], world["bob_fact"]}
    assert not {f"fact:{i}" for i in private} & keys
    assert not {f"segment:{world['alice_seg']}", f"segment:{world['bob_seg']}"} & keys


# --------------------------------------------------------------------------- #
# The question sources, from seeded rows
# --------------------------------------------------------------------------- #


def test_from_runs_reads_the_persisted_search_memory_calls(world) -> None:
    from robothor.memory import replay

    questions = replay.questions_from_runs(A)
    texts = [q.text for q in questions]
    assert set(texts) == {
        "bike locker spare key",
        "locker combination",
        "an old question about the garage",
        "gym bike locker code",
    }
    by_text = {q.text: q for q in questions}
    assert by_text["bike locker spare key"].reader.person_id == world["alice"]
    assert by_text["bike locker spare key"].limit == 5
    assert by_text["gym bike locker code"].reader.person_id == world["bob"]
    assert by_text["locker combination"].origin["run_id"] == world["run_alice"]
    # Newest first.
    assert texts[-1] == "an old question about the garage"

    recent = replay.questions_from_runs(A, since=datetime.now(UTC) - timedelta(days=7))
    assert "an old question about the garage" not in {q.text for q in recent}
    assert len(replay.questions_from_runs(A, limit=2)) == 2


def test_from_chat_reads_user_turns_only(world) -> None:
    from robothor.memory import replay

    questions = replay.questions_from_chat(A)
    assert {q.text for q in questions} == {
        "what is my bike locker code?",
        "remind me of my bike locker code",
    }
    assert all(q.origin["source"] == "chat" for q in questions)
    assert replay.questions_from_chat(A, since=datetime.now(UTC) + timedelta(days=1)) == []
