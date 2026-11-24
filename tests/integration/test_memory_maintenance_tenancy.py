"""Nightly memory maintenance reads and writes one tenant at a time.

``run_lifecycle_maintenance`` used to run its first seven steps — importance,
decay, pruning, consolidation, insight discovery and relation inference —
across every tenant's ``memory_facts`` at once. Facts of two companies could
be merged into one consolidated fact, an insight could cite both, and an
inferred edge could join an entity of one tenant to an entity of another. The
derived rows then landed in whatever tenant the column default said.

With row-level security on, the same unscoped queries ran bound to the
process's own tenant, so a child tenant received no maintenance at all.

These tests seed two tenants with deliberately similar data, run the real
nightly pass, and read the database. Only the LLM and the embedding model are
stubbed: they are external services, and nothing they return can make a row
land in the right tenant.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest

from tests.rls_app_role import app_role, rls_tenants  # noqa: F401 - pytest fixtures

pytestmark = pytest.mark.integration

_VECTOR = [0.1] * 1024
_INSIGHT_TEXT = "The recurring planning sessions line up with the shared weekend routine."


async def _fake_generate(prompt: str = "", system: str = "", **kw: Any) -> str:
    """Answer each maintenance prompt the way a model would, deterministically."""
    from robothor.memory.entities import RELATION_INFERENCE_SCHEMA
    from robothor.memory.lifecycle import IMPORTANCE_SCHEMA, INSIGHT_SCHEMA

    fmt = kw.get("format")
    if fmt == IMPORTANCE_SCHEMA:
        return json.dumps({"score": 0.7})
    if fmt == INSIGHT_SCHEMA:
        ids = [int(i) for i in re.findall(r"^\[(\d+)\]", prompt, flags=re.MULTILINE)]
        return json.dumps({"insights": [{"insight_text": _INSIGHT_TEXT, "source_fact_ids": ids}]})
    if fmt == RELATION_INFERENCE_SCHEMA:
        a = re.search(r"Entity A: (.+)", prompt)
        b = re.search(r"Entity B: (.+)", prompt)
        if not (a and b):
            return json.dumps({"relations": []})
        return json.dumps(
            {
                "relations": [
                    {
                        "source": a.group(1).strip(),
                        "target": b.group(1).strip(),
                        "relation": "collaborates_with",
                        "confidence": 0.6,
                    }
                ]
            }
        )
    if system.startswith("Combine these facts"):
        first = re.search(r"^- (.+)$", prompt, flags=re.MULTILINE)
        return f"Combined note: {first.group(1) if first else 'planning'}"
    return "none"


@pytest.fixture
def stub_models(monkeypatch):
    from robothor.llm import ollama as llm_client
    from robothor.memory import generation, lifecycle

    async def _embed(_text: str) -> list[float]:
        return list(_VECTOR)

    async def _embed_batch(texts: list[str]) -> list[list[float]]:
        return [list(_VECTOR) for _ in texts]

    monkeypatch.setattr(generation, "generate", _fake_generate)
    monkeypatch.setattr(llm_client, "get_embedding_async", _embed)
    monkeypatch.setattr(llm_client, "get_embeddings_batch_async", _embed_batch)
    # A per-test lock key: another lane's maintenance run must not make this
    # one skip, and this one must not hold theirs.
    monkeypatch.setattr(lifecycle, "_LIFECYCLE_LOCK_KEY", f"test:lifecycle:{uuid.uuid4().hex}")


def _seed_tenant(cur, tenant: str, partner: str) -> dict[str, Any]:
    """Three similar facts about Alice and ``partner``, and both entities."""
    entity_ids = {}
    for name in ("Alice", partner):
        cur.execute(
            """
            INSERT INTO memory_entities (name, entity_type, mention_count, tenant_id)
            VALUES (%s, 'person', 5, %s) RETURNING id
            """,
            (name, tenant),
        )
        entity_ids[name] = cur.fetchone()["id"]

    fact_ids = []
    for category, text in (
        ("project", f"Alice and {partner} hold the weekly planning session every Monday."),
        ("personal", f"Alice and {partner} share a weekend routine of long morning walks."),
        ("project", f"Alice and {partner} review the planning board after each Monday session."),
    ):
        cur.execute(
            """
            INSERT INTO memory_facts
                (fact_text, category, entities, confidence, source_type, embedding,
                 importance_score, decay_score, tenant_id)
            VALUES (%s, %s, %s, 0.9, 'conversation', %s::vector, 0.7, 0.42, %s)
            RETURNING id
            """,
            (text, category, ["Alice", partner], str(_VECTOR), tenant),
        )
        fact_ids.append(cur.fetchone()["id"])
    return {"tenant": tenant, "entity_ids": entity_ids, "fact_ids": fact_ids}


def _tenant_of_facts(cur, fact_ids: list[int]) -> set[str]:
    cur.execute("SELECT DISTINCT tenant_id FROM memory_facts WHERE id = ANY(%s)", (fact_ids,))
    return {r["tenant_id"] for r in cur.fetchall()}


def _consolidated(cur, seeded: dict[str, Any]) -> list[dict[str, Any]]:
    """Consolidated facts that superseded any of this tenant's seeded facts."""
    cur.execute(
        """
        SELECT c.id, c.tenant_id,
               ARRAY_AGG(DISTINCT s.tenant_id) AS source_tenants
        FROM memory_facts c
        JOIN memory_facts s ON s.superseded_by = c.id
        WHERE c.source_type = 'consolidation'
          AND c.id IN (SELECT superseded_by FROM memory_facts WHERE id = ANY(%s))
        GROUP BY c.id, c.tenant_id
        """,
        (seeded["fact_ids"],),
    )
    return [dict(r) for r in cur.fetchall()]


def _insights(cur, seeded: dict[str, Any]) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT id, tenant_id, source_fact_ids FROM memory_insights WHERE source_fact_ids && %s",
        (seeded["fact_ids"],),
    )
    return [dict(r) for r in cur.fetchall()]


def _relations(cur, seeded: dict[str, Any]) -> list[dict[str, Any]]:
    ids = list(seeded["entity_ids"].values())
    cur.execute(
        """
        SELECT r.id, r.tenant_id, r.fact_id,
               s.tenant_id AS source_tenant, t.tenant_id AS target_tenant
        FROM memory_relations r
        JOIN memory_entities s ON s.id = r.source_entity_id
        JOIN memory_entities t ON t.id = r.target_entity_id
        WHERE r.source_entity_id = ANY(%s) OR r.target_entity_id = ANY(%s)
        """,
        (ids, ids),
    )
    return [dict(r) for r in cur.fetchall()]


def _maintain_only(monkeypatch, tenants: dict[str, Any]) -> None:
    """Limit the pass to the seeded tenants and the platform tenant.

    The shared test database carries well over a thousand tenants left by
    other suites; walking every one of them per test is minutes of nothing.
    The real enumeration is ``active_tenant_ids``, exercised on its own below.
    """
    from robothor.constants import DEFAULT_TENANT
    from robothor.memory import lifecycle

    ids = [DEFAULT_TENANT] + [t["tenant"] for t in tenants.values()]
    monkeypatch.setattr(lifecycle, "active_tenant_ids", lambda: list(ids), raising=False)


@pytest.fixture
def two_tenants(db_cursor, test_prefix, monkeypatch):
    tenants = {}
    for key, partner in (("a", "Bob"), ("b", "Carol")):
        tid = f"{test_prefix}-tenant-{key}"
        db_cursor.execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)",
            (tid, tid),
        )
        tenants[key] = _seed_tenant(db_cursor, tid, partner)
    _maintain_only(monkeypatch, tenants)
    return tenants


# ── Without row-level security: the statements scope themselves ─────────────


@pytest.mark.asyncio
async def test_inferred_edges_stay_inside_the_tenant_of_their_source_facts(
    two_tenants, db_cursor, mock_get_connection, stub_models
):
    from robothor.memory.lifecycle import run_lifecycle_maintenance

    await run_lifecycle_maintenance()

    for seeded in two_tenants.values():
        rels = _relations(db_cursor, seeded)
        assert rels, f"no relation was inferred for {seeded['tenant']}"
        for rel in rels:
            assert rel["source_tenant"] == rel["target_tenant"] == seeded["tenant"], (
                f"edge {rel['id']} joins entities of {rel['source_tenant']} and "
                f"{rel['target_tenant']}"
            )
            assert rel["tenant_id"] == seeded["tenant"], (
                f"edge {rel['id']} stored under {rel['tenant_id']}, not {seeded['tenant']}"
            )
            if rel["fact_id"] is not None:
                assert _tenant_of_facts(db_cursor, [rel["fact_id"]]) == {seeded["tenant"]}


@pytest.mark.asyncio
async def test_each_tenant_keeps_its_own_similar_insight(
    two_tenants, db_cursor, mock_get_connection, stub_models
):
    from robothor.memory.lifecycle import run_lifecycle_maintenance

    await run_lifecycle_maintenance()

    for seeded in two_tenants.values():
        insights = _insights(db_cursor, seeded)
        assert insights, f"{seeded['tenant']} kept no insight of its own"
        for ins in insights:
            assert ins["tenant_id"] == seeded["tenant"], (
                f"insight {ins['id']} written to {ins['tenant_id']}, not {seeded['tenant']}"
            )
            assert _tenant_of_facts(db_cursor, ins["source_fact_ids"]) == {seeded["tenant"]}


@pytest.mark.asyncio
async def test_consolidation_never_merges_two_tenants_facts(
    two_tenants, db_cursor, mock_get_connection, stub_models
):
    from robothor.memory.lifecycle import run_lifecycle_maintenance

    await run_lifecycle_maintenance()

    for seeded in two_tenants.values():
        merged = _consolidated(db_cursor, seeded)
        assert merged, f"nothing of {seeded['tenant']} was consolidated"
        for row in merged:
            assert row["source_tenants"] == [seeded["tenant"]], (
                f"consolidated fact {row['id']} merges sources from {row['source_tenants']}"
            )
            assert row["tenant_id"] == seeded["tenant"], (
                f"consolidated fact {row['id']} stored under {row['tenant_id']}"
            )


# ── With row-level security: every tenant is maintained under its own scope ──


@pytest.fixture
def two_child_tenants(db_cursor, rls_tenants, monkeypatch):  # noqa: F811
    tenants = {
        "a": _seed_tenant(db_cursor, rls_tenants["a"], "Bob"),
        "b": _seed_tenant(db_cursor, rls_tenants["b"], "Carol"),
    }
    _maintain_only(monkeypatch, tenants)
    return tenants


def test_active_tenant_ids_lists_every_active_tenant_and_no_inactive_one(
    db_cursor, mock_get_connection, test_prefix
):
    from robothor.memory.lifecycle import active_tenant_ids

    on, off = f"{test_prefix}-tenant-on", f"{test_prefix}-tenant-off"
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name, active) VALUES (%s, %s, TRUE), (%s, %s, FALSE)",
        (on, on, off, off),
    )
    ids = active_tenant_ids()
    assert on in ids
    assert off not in ids


@pytest.mark.asyncio
async def test_child_tenants_receive_maintenance_with_rls_on(
    two_child_tenants,
    db_cursor,
    app_role,  # noqa: F811
    stub_models,
):
    from robothor.memory.lifecycle import run_lifecycle_maintenance

    await run_lifecycle_maintenance()

    for seeded in two_child_tenants.values():
        tid = seeded["tenant"]
        db_cursor.execute(
            "SELECT decay_score FROM memory_facts WHERE id = ANY(%s)", (seeded["fact_ids"],)
        )
        assert all(r["decay_score"] != 0.42 for r in db_cursor.fetchall()), (
            f"decay was never computed for {tid}"
        )
        merged = _consolidated(db_cursor, seeded)
        assert merged and all(r["tenant_id"] == tid for r in merged), (
            f"{tid} was not consolidated under its own tenant: {merged}"
        )
        insights = _insights(db_cursor, seeded)
        assert insights and all(i["tenant_id"] == tid for i in insights), (
            f"{tid} got no insight of its own: {insights}"
        )
        rels = _relations(db_cursor, seeded)
        assert rels and all(r["tenant_id"] == tid for r in rels), (
            f"{tid} got no inferred relation of its own: {rels}"
        )


# ── Knowledge gaps: the engine tool answers for the caller's tenant only ─────


@pytest.mark.asyncio
async def test_get_knowledge_gaps_returns_nothing_of_another_tenant(
    db_cursor, mock_get_connection, test_prefix
):
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers.memory import _get_knowledge_gaps

    tenant_a = f"{test_prefix}-tenant-a"
    tenant_b = f"{test_prefix}-tenant-b"
    for tid in (tenant_a, tenant_b):
        db_cursor.execute("INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s)", (tid, tid))

    # Tenant A: one dominant type, and an orphan of its own so the answer is
    # not empty for a reason unrelated to tenancy.
    for i in range(12):
        db_cursor.execute(
            "INSERT INTO memory_entities (name, entity_type, mention_count, tenant_id) "
            "VALUES (%s, 'person', 2, %s)",
            (f"Alice {i}", tenant_a),
        )
    db_cursor.execute(
        "INSERT INTO memory_entities (name, entity_type, mention_count, tenant_id) "
        "VALUES ('Alice orphan', 'person', 1, %s) RETURNING id",
        (tenant_a,),
    )
    a_orphan = db_cursor.fetchone()["id"]

    # Tenant B: every kind of gap, each ranked to the top of a global query.
    db_cursor.execute(
        "INSERT INTO memory_entities (name, entity_type, mention_count, last_seen, tenant_id) "
        "VALUES ('Bob orphan', 'gap-probe-b', 1, NOW() + INTERVAL '1 day', %s) RETURNING id",
        (tenant_b,),
    )
    b_orphan = db_cursor.fetchone()["id"]
    db_cursor.execute(
        "INSERT INTO memory_entities (name, entity_type, mention_count, tenant_id) "
        "VALUES ('Bob thin', 'person', 1000000, %s) RETURNING id",
        (tenant_b,),
    )
    b_thin = db_cursor.fetchone()["id"]
    db_cursor.execute(
        "INSERT INTO memory_facts (fact_text, category, confidence, tenant_id) "
        "VALUES ('Bob may have moved to another office.', 'personal', 0.0, %s) RETURNING id",
        (tenant_b,),
    )
    b_fact = db_cursor.fetchone()["id"]
    b_agent = f"{test_prefix}-agent-b"
    db_cursor.execute(
        "INSERT INTO agent_runs (tenant_id, agent_id, trigger_type, status, output_text) "
        "VALUES (%s, %s, 'cron', 'completed', 'I don''t know where Bob works now.')",
        (tenant_b, b_agent),
    )

    gaps = await _get_knowledge_gaps({}, ToolContext(agent_id="auto-agent", tenant_id=tenant_a))

    entity_ids = {e["id"] for e in gaps["orphaned_entities"]} | {
        e["id"] for e in gaps["thin_clusters"]
    }
    assert a_orphan in entity_ids, "tenant A's own orphan is missing"
    assert not entity_ids & {b_orphan, b_thin}, "tenant B's entities leaked into A's gaps"
    assert b_fact not in {f["id"] for f in gaps["low_confidence_facts"]}
    assert "gap-probe-b" not in {t["entity_type"] for t in gaps["type_imbalances"]}
    assert b_agent not in {s["agent_id"] for s in gaps["uncertainty_signals"]}
