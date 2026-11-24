"""Seed the synthetic eval world into the eval tenants, run it, remove every trace.

The world (:mod:`robothor.memory.eval_world`) already knows its facts, entities
and relations, so seeding writes them directly through the platform's own
insert functions — no LLM extraction runs. Only embeddings are computed, and
the embedder is injectable so unit and integration tests never call a model.

Tables touched, per tenant (``WORLD_TABLES``):

    memory_facts      facts._insert_fact (the row writer store_facts_batch uses),
                      then dates, validity and supersession set on those rows
    memory_entities   entities.upsert_entity
    memory_relations  entities.add_relations_batch
    crm_companies     crm.dal.create_company
    crm_people        crm.dal.create_person + crm.dal.update_person
    audit_log         written by the CRM DAL for each create/update above
    kb_documents      knowledge.store.upsert_document
    kb_chunks         (same)
    chat_sessions     engine.chat_store.save_exchange
    chat_messages     (same), then the turn timestamps set on those rows
    crm_tenants       the tenant row itself, created if absent

``cleanup_world`` deletes every row of every one of those tables for the given
tenants. Both refuse, before any database call, a tenant that is not an eval
tenant (``memory-eval`` or ``memory-eval-<suffix>``).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from robothor.memory.eval import (
    EVAL_TENANT,
    CaseResult,
    EvalPreconditionError,
    is_eval_tenant,
    score_recall,
    score_temporal,
)

if TYPE_CHECKING:
    from robothor.memory.eval_world import Case, World

logger = logging.getLogger(__name__)

EmbedBatch = Callable[[list[str]], Awaitable[list[list[float]]]]

SHADOW_SUFFIX = "-shadow"
#: The world's own default tenant. Never the suite's ``memory-eval``.
WORLD_TENANT = f"{EVAL_TENANT}-world"
WORLD_COLLECTION = "memory-eval/world"
WORLD_SOURCE_TYPE = "eval_world"
WORLD_CHANNEL = "eval"
WORLD_SUITE_ID = "memory-world-v1"
_EMBED_CHUNK = 64

#: Every table seeding writes to. Cleanup empties each for the eval tenants.
WORLD_TABLES: tuple[str, ...] = (
    "memory_facts",
    "memory_entities",
    "memory_relations",
    "crm_people",
    "crm_companies",
    "audit_log",
    "kb_documents",
    "kb_chunks",
    "chat_sessions",
    "chat_messages",
    "crm_tenants",
)

#: Strata scored by "the current value ranks first" rather than "in top-k".
_RANK_FIRST = frozenset({"knowledge_update", "long_tenure"})
#: Strata retrieval alone cannot score; the answer-and-judge arm does.
_UNSCORED = frozenset({"abstention"})


# --------------------------------------------------------------------------- #
# Tenant guard
# --------------------------------------------------------------------------- #


def shadow_tenant_for(tenant_id: str) -> str:
    """The second (leakage) tenant paired with an eval tenant."""
    return f"{tenant_id}{SHADOW_SUFFIX}"


def _require_eval_tenants(tenant_ids: Sequence[object]) -> list[str]:
    """World tenants only: ``memory-eval-<suffix>``, never anything else.

    ``memory-eval`` itself is refused. It belongs to the nightly suite, whose
    cleanup deletes only ``memory_facts`` and whose distractor corpus a world
    cleanup would wipe; sharing it lets either run break the other.
    """
    if EVAL_TENANT in tenant_ids:
        raise EvalPreconditionError(
            f"refusing to use {EVAL_TENANT!r} for the eval world: that tenant belongs to the "
            f"nightly suite. Use {WORLD_TENANT!r} (the default) or another "
            f"{EVAL_TENANT + '-<suffix>'!r} tenant"
        )
    bad = [t for t in tenant_ids if not is_eval_tenant(t)]
    if bad or not tenant_ids:
        raise EvalPreconditionError(
            f"refusing to touch tenant(s) {bad or list(tenant_ids)!r}: the eval world may only "
            f"be seeded into or removed from {EVAL_TENANT + '-<suffix>'!r} tenants "
            f"(default {WORLD_TENANT!r})"
        )
    return [str(t) for t in tenant_ids]


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #


@dataclass
class SeededWorld:
    """Database ids for what was seeded, keyed by world id."""

    tenant_ids: tuple[str, ...]
    fact_ids: dict[str, int] = field(default_factory=dict)
    entity_ids: dict[str, int] = field(default_factory=dict)
    crm_person_ids: dict[str, str] = field(default_factory=dict)
    crm_company_ids: dict[str, str] = field(default_factory=dict)
    document_ids: list[str] = field(default_factory=list)
    chat_session_keys: list[str] = field(default_factory=list)
    #: world turn id -> chat_messages.id
    turn_message_ids: dict[str, int] = field(default_factory=dict)


async def _default_embed_batch(texts: list[str]) -> list[list[float]]:
    from robothor.llm import ollama as llm_client

    return await llm_client.get_embeddings_batch_async(texts)


async def _embed_all(texts: list[str], embed_batch: EmbedBatch) -> list[list[float]]:
    out: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_CHUNK):
        chunk = texts[start : start + _EMBED_CHUNK]
        vectors = await embed_batch(chunk)
        if len(vectors) != len(chunk):
            raise RuntimeError(f"embedder returned {len(vectors)} vectors for {len(chunk)} texts")
        out.extend(vectors)
    return out


async def seed_world(
    world: World,
    tenant_id: str = WORLD_TENANT,
    *,
    shadow_tenant_id: str | None = None,
    embed_batch: EmbedBatch | None = None,
) -> SeededWorld:
    """Load ``world`` into ``tenant_id`` and its shadow world into the shadow tenant."""
    shadow = shadow_tenant_id if shadow_tenant_id is not None else shadow_tenant_for(tenant_id)
    tenants = [tenant_id] + ([shadow] if world.shadow is not None else [])
    _require_eval_tenants(tenants)
    if world.shadow is not None and shadow == tenant_id:
        raise EvalPreconditionError("the shadow tenant must differ from the primary tenant")

    from robothor.db.connection import tenant_scope

    embed = embed_batch or _default_embed_batch
    seeded = SeededWorld(tenant_ids=tuple(tenants))
    pairs: list[tuple[World, str]] = [(world, tenant_id)]
    if world.shadow is not None:
        pairs.append((world.shadow, shadow))
    for w, tenant in pairs:
        # Scoped so row-level security binds every write (and every CRM audit
        # row) to the tenant being seeded, never to the process's own tenant.
        with tenant_scope(tenant):
            _ensure_world_tenant(tenant)
            await _seed_facts(w, tenant, embed, seeded)
            await _seed_entities(w, tenant, seeded)
            _seed_crm(w, tenant, seeded)
            await _seed_documents(w, tenant, embed, seeded)
            await _seed_chat(w, tenant, embed, seeded)
    logger.info(
        "eval world seeded: %d facts, %d entities, %d crm people, %d documents, %d chat sessions",
        len(seeded.fact_ids),
        len(seeded.entity_ids),
        len(seeded.crm_person_ids),
        len(seeded.document_ids),
        len(seeded.chat_session_keys),
    )
    return seeded


def _ensure_world_tenant(tenant_id: str) -> None:
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        conn.cursor().execute(
            "INSERT INTO crm_tenants (id, display_name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (tenant_id, f"Memory Eval World ({tenant_id})"),
        )


async def _seed_facts(world: World, tenant: str, embed: EmbedBatch, seeded: SeededWorld) -> None:
    from robothor.db.connection import get_connection
    from robothor.memory.drift import compute_fact_hash
    from robothor.memory.facts import _insert_fact

    vectors = await _embed_all([f.text for f in world.facts], embed)
    with get_connection() as conn:
        cur = conn.cursor()
        for fact, vector in zip(world.facts, vectors, strict=True):
            content_hash = compute_fact_hash(fact.text, tenant_id=tenant, category=fact.category)
            metadata = {
                "eval_world": {
                    "seed": world.seed,
                    "size": world.size,
                    "tenant_key": world.tenant_key,
                    "fact_id": fact.id,
                    "origin": fact.origin,
                    "event_date": fact.event_date,
                    "source": {
                        "kind": fact.source.kind,
                        "container_id": fact.source.container_id,
                        "segment_id": fact.source.segment_id,
                    },
                    "mentions": [
                        {"entity_id": m.entity_id, "surface": m.surface} for m in fact.mentions
                    ],
                }
            }
            seeded.fact_ids[fact.id] = _insert_fact(
                cur,
                (
                    fact.text,
                    fact.category,
                    [m.surface for m in fact.mentions],
                    1.0,
                    f"{fact.source.kind}:{fact.source.segment_id}",
                    WORLD_SOURCE_TYPE,
                    vector,
                    json.dumps(metadata),
                    tenant,
                    content_hash,
                ),
                tenant_id=tenant,
                content_hash=content_hash,
            )
        # The world's clock, not the seeding clock: created_at and validity
        # come from the fact's event date, and superseded facts are inactive
        # exactly as conflict resolution leaves them in production.
        for fact in world.facts:
            successor = seeded.fact_ids.get(fact.superseded_by) if fact.superseded_by else None
            cur.execute(
                """
                UPDATE memory_facts
                   SET created_at = %s::timestamptz, updated_at = %s::timestamptz,
                       valid_from = %s::timestamptz, valid_to = %s::timestamptz,
                       superseded_by = %s, is_active = %s
                 WHERE id = %s AND tenant_id = %s
                """,
                (
                    fact.event_date,
                    fact.event_date,
                    fact.valid_from,
                    fact.valid_to,
                    successor,
                    successor is None,
                    seeded.fact_ids[fact.id],
                    tenant,
                ),
            )


_ENTITY_TYPES = {"per": "person", "org": "organization", "prj": "project", "loc": "location"}


async def _seed_entities(world: World, tenant: str, seeded: SeededWorld) -> None:
    from robothor.memory.entities import add_relations_batch, upsert_entity

    rows: list[tuple[str, str, list[str]]] = []
    for p in world.people:
        aliases = [p.email, p.initials, p.full_name.lower()] + ([p.nickname] if p.nickname else [])
        rows.append((p.id, p.full_name, aliases))
    rows += [(o.id, o.name, [o.domain]) for o in world.organisations]
    rows += [(p.id, p.name, []) for p in world.projects]
    rows += [(loc.id, loc.name, []) for loc in world.locations]
    rows += [(e.id, e.name, []) for e in world.events]
    for world_id, name, aliases in rows:
        kind = world_id.removeprefix("sh-").split("-", 1)[0]
        entity_type = _ENTITY_TYPES.get(kind, "event")
        entity_id = await upsert_entity(name, entity_type, aliases, tenant_id=tenant)
        if entity_id is None:
            raise RuntimeError(f"entity {name!r} was refused as junk; the world would be partial")
        seeded.entity_ids[world_id] = entity_id

    ent, fct = seeded.entity_ids, seeded.fact_ids
    relations: list[tuple[int, int, str, int | None, float]] = []
    relations += [(ent[e.person_id], ent[e.org_id], "works_at", fct[e.fact_id], 1.0) for e in world.employments]  # fmt: skip
    relations += [(ent[r.person_id], ent[r.location_id], "lives_in", fct[r.fact_id], 1.0) for r in world.residences]  # fmt: skip
    relations += [(ent[r.person_id], ent[r.manager_id], "reports_to", fct[r.fact_id], 1.0) for r in world.reporting_lines]  # fmt: skip
    relations += [(ent[m.person_id], ent[m.project_id], "member_of", fct[m.fact_id], 1.0) for m in world.memberships]  # fmt: skip
    relations += [(ent[p.id], ent[p.org_id], "run_by", fct[p.fact_id], 1.0) for p in world.projects]  # fmt: skip
    relations += [(ent[p.lead_id], ent[p.id], "leads", fct[p.fact_id], 1.0) for p in world.projects if p.lead_id]  # fmt: skip
    relations += [(ent[o.id], ent[o.hq_location_id], "headquartered_in", fct[o.hq_fact_id], 1.0) for o in world.organisations]  # fmt: skip
    relations += [(ent[e.id], ent[e.location_id], "held_in", fct[e.fact_id], 1.0) for e in world.events]  # fmt: skip
    relations += [(ent[a], ent[e.id], "attended", fct[e.fact_id], 1.0) for e in world.events for a in e.attendee_ids]  # fmt: skip
    # One row per (source, target, type), the latest fact winning: a repeated
    # key in one INSERT ... ON CONFLICT statement is rejected by Postgres.
    unique: dict[tuple[int, int, str], tuple[int, int, str, int | None, float]] = {}
    for row in relations:
        unique[(row[0], row[1], row[2])] = row
    await add_relations_batch(list(unique.values()), tenant_id=tenant)


def _seed_crm(world: World, tenant: str, seeded: SeededWorld) -> None:
    from robothor.crm.dal import create_company, create_person, update_person

    for company in world.crm_companies:
        cid = create_company(
            company.name, domain_name=company.domain, address_city=company.city, tenant_id=tenant
        )
        if cid is None:
            raise RuntimeError(f"CRM refused company {company.name!r}")
        seeded.crm_company_ids[company.org_id] = cid
    for row in world.crm_people:
        pid = create_person(row.first_name, row.last_name, row.email, tenant_id=tenant)
        if pid is None:
            raise RuntimeError(f"CRM refused person {row.first_name} {row.last_name}")
        update_person(
            pid,
            tenant_id=tenant,
            job_title=row.job_title,
            city=row.city,
            company_id=seeded.crm_company_ids.get(row.company_org_id or ""),
        )
        seeded.crm_person_ids[row.person_id] = pid


async def _seed_documents(
    world: World, tenant: str, embed: EmbedBatch, seeded: SeededWorld
) -> None:
    from robothor.knowledge.store import upsert_document

    async def _one(text: str) -> list[float]:
        return (await embed([text]))[0]

    for doc in world.documents:
        text = "\n\n".join(f"{s.heading}\n{s.body}" for s in doc.sections)
        await upsert_document(
            tenant,
            WORLD_COLLECTION,
            doc.id,
            doc.title,
            text,
            source_uri=f"eval-world://{doc.id}",
            metadata={
                "eval_world": {
                    "date": doc.date,
                    "trusted": doc.trusted,
                    "origin": doc.origin,
                    "sections": [s.id for s in doc.sections],
                }
            },
            embed=_one,
        )
        seeded.document_ids.append(doc.id)


async def _seed_chat(world: World, tenant: str, embed: EmbedBatch, seeded: SeededWorld) -> None:
    from robothor.db.connection import get_connection
    from robothor.engine.chat_store import save_exchange

    for conv in world.conversations:
        for session in conv.sessions:
            key = f"eval-world:{session.id}"
            stamps: list[tuple[int, str]] = []
            turns = session.turns
            for i in range(0, len(turns) - 1, 2):
                user, assistant = turns[i], turns[i + 1]
                ids = save_exchange(
                    key,
                    user.text,
                    assistant.text,
                    channel=WORLD_CHANNEL,
                    tenant_id=tenant,
                    user_extras={"eval_world_turn": user.id},
                    assistant_extras={"eval_world_turn": assistant.id},
                    user_at=datetime.fromisoformat(user.at),
                )
                stamps.append((ids[1], assistant.at))
                seeded.turn_message_ids[user.id] = ids[0]
                seeded.turn_message_ids[assistant.id] = ids[1]
            with get_connection() as conn:
                cur = conn.cursor()
                for message_id, at in stamps:
                    cur.execute(
                        "UPDATE chat_messages SET created_at = %s::timestamptz WHERE id = %s",
                        (at, message_id),
                    )
                cur.execute(
                    "UPDATE chat_sessions SET created_at = %s::timestamptz, "
                    "last_active_at = %s::timestamptz WHERE tenant_id = %s AND session_key = %s",
                    (turns[0].at, turns[-1].at, tenant, key),
                )
            seeded.chat_session_keys.append(key)

    # Turns are embedded the way the engine embeds chat turns (the content),
    # so raw-history retrieval has a dense leg to search.
    all_turns = [t for c in world.conversations for s in c.sessions for t in s.turns]
    vectors = await _embed_all([t.text for t in all_turns], embed)
    with get_connection() as conn:
        cur = conn.cursor()
        for turn, vector in zip(all_turns, vectors, strict=True):
            cur.execute(
                "UPDATE chat_messages SET embedding = %s, embedded_at = NOW() WHERE id = %s",
                (vector, seeded.turn_message_ids[turn.id]),
            )


# --------------------------------------------------------------------------- #
# Counting and cleanup
# --------------------------------------------------------------------------- #

_COUNT_SQL: dict[str, str] = {
    "chat_messages": (
        "SELECT count(*) FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id "
        "WHERE s.tenant_id = %s"
    ),
    "crm_tenants": "SELECT count(*) FROM crm_tenants WHERE id = %s",
}

# Children before parents: relations reference facts and entities, messages
# reference sessions, chunks reference documents, people reference companies.
_DELETE_ORDER: tuple[tuple[str, str], ...] = (
    ("memory_relations", "DELETE FROM memory_relations WHERE tenant_id = %s"),
    ("memory_facts", "DELETE FROM memory_facts WHERE tenant_id = %s"),
    ("memory_entities", "DELETE FROM memory_entities WHERE tenant_id = %s"),
    (
        "chat_messages",
        "DELETE FROM chat_messages WHERE session_id IN "
        "(SELECT id FROM chat_sessions WHERE tenant_id = %s)",
    ),
    ("chat_sessions", "DELETE FROM chat_sessions WHERE tenant_id = %s"),
    ("kb_chunks", "DELETE FROM kb_chunks WHERE tenant_id = %s"),
    ("kb_documents", "DELETE FROM kb_documents WHERE tenant_id = %s"),
    ("crm_people", "DELETE FROM crm_people WHERE tenant_id = %s"),
    ("crm_companies", "DELETE FROM crm_companies WHERE tenant_id = %s"),
    ("audit_log", "DELETE FROM audit_log WHERE tenant_id = %s"),
)


def world_row_counts(tenant_ids: Sequence[str]) -> dict[str, int]:
    """Rows each of ``WORLD_TABLES`` holds for the given eval tenants."""
    tenants = _require_eval_tenants(tenant_ids)
    from robothor.db.connection import get_connection, tenant_scope

    counts = dict.fromkeys(WORLD_TABLES, 0)
    for tenant in tenants:
        with tenant_scope(tenant), get_connection() as conn:
            cur = conn.cursor()
            for table in WORLD_TABLES:
                sql = _COUNT_SQL.get(table, f"SELECT count(*) FROM {table} WHERE tenant_id = %s")  # noqa: S608 — fixed table names
                cur.execute(sql, (tenant,))
                counts[table] += int(cur.fetchone()[0])
    return counts


def cleanup_world(tenant_ids: Sequence[str]) -> dict[str, int]:
    """Delete every row the world seeding can write for these eval tenants.

    Returns rows deleted per table. The tenant row itself goes last, and only
    when nothing else still references it.
    """
    tenants = _require_eval_tenants(tenant_ids)
    import psycopg2

    from robothor.db.connection import get_connection, tenant_scope

    deleted = dict.fromkeys(WORLD_TABLES, 0)
    for tenant in tenants:
        with tenant_scope(tenant), get_connection() as conn:
            cur = conn.cursor()
            for table, sql in _DELETE_ORDER:
                cur.execute(sql, (tenant,))
                deleted[table] += max(cur.rowcount, 0)
            cur.execute("SAVEPOINT eval_world_tenant")
            try:
                cur.execute("DELETE FROM crm_tenants WHERE id = %s", (tenant,))
                deleted["crm_tenants"] += max(cur.rowcount, 0)
                cur.execute("RELEASE SAVEPOINT eval_world_tenant")
            except psycopg2.errors.ForeignKeyViolation as exc:
                cur.execute("ROLLBACK TO SAVEPOINT eval_world_tenant")
                logger.info("eval tenant %s still referenced; row kept: %s", tenant, exc)
    return deleted


# --------------------------------------------------------------------------- #
# Scoring and the run
# --------------------------------------------------------------------------- #


def score_world_case(case: Case, top_texts: list[str], k: int = 5) -> CaseResult | None:
    """Score a world case with the existing retrieval scorers.

    ``gold`` lists acceptable answers, so each is scored on its own and the
    best counts. ``None`` for strata retrieval alone cannot judge.
    """
    if case.stratum in _UNSCORED:
        return None
    golds = list(case.gold)
    if case.stratum in _RANK_FIRST:
        passed = any(score_temporal(top_texts, g)[0] for g in golds)
        score = 1.0 if passed else 0.0
        detail = "" if passed else "current value did not rank first"
    else:
        passed = any(score_recall(top_texts, g, k)[0] for g in golds)
        score = 1.0 if passed else 0.0
        detail = "" if passed else f"gold not found in top-{k}"
    if case.stratum == "cross_tenant":
        hay = " ".join(top_texts[:k]).lower()
        leaked = [f for f in case.forbidden if f.lower() in hay]
        if leaked:
            passed, score = False, 0.0
            detail = f"leak: other tenant's answer {leaked[0]!r} in top-{k}"
    return CaseResult(
        case_id=case.id,
        kind=case.stratum,
        passed=passed,
        score=score,
        top_texts=list(top_texts),
        detail=detail,
    )


def build_world_report(
    cases: Sequence[Case],
    results: Sequence[CaseResult | None],
    *,
    suite_id: str = WORLD_SUITE_ID,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_kind: dict[str, dict[str, int]] = {}
    unscored: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for case, result in zip(cases, results, strict=True):
        if result is None:
            unscored[case.stratum] = unscored.get(case.stratum, 0) + 1
            continue
        agg = by_kind.setdefault(case.stratum, {"passed": 0, "total": 0})
        agg["total"] += 1
        agg["passed"] += int(result.passed)
        rows.append({**result.to_dict(), "split": case.split})
    return {
        "suite_id": suite_id,
        **(meta or {}),
        "total": sum(a["total"] for a in by_kind.values()),
        "passed": sum(a["passed"] for a in by_kind.values()),
        "by_kind": by_kind,
        "unscored": unscored,
        "cases": rows,
    }


def format_world_report(report: dict[str, Any], *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(report, indent=2, default=str)
    lines = [
        f"Memory eval world: {report.get('suite_id', '')} "
        f"seed={report.get('seed')} size={report.get('size')} split={report.get('split')}",
        f"  passed: {report.get('passed', 0)}/{report.get('total', 0)}",
    ]
    for stratum, agg in sorted(report.get("by_kind", {}).items()):
        rate = agg["passed"] / agg["total"] if agg["total"] else 0.0
        lines.append(f"  {stratum}: {agg['passed']}/{agg['total']} ({rate:.1%})")
    for stratum, n in sorted(report.get("unscored", {}).items()):
        lines.append(f"  {stratum}: {n} unscored (retrieval cannot judge an abstention)")
    t = report.get("timings")
    if t:
        lines.append(
            f"  time: seed {t['seed_s']:.1f}s, queries {t['query_s']:.1f}s, total {t['total_s']:.1f}s"
        )
    return "\n".join(lines)


async def run_world(
    seed: int,
    size: str,
    split: str,
    *,
    tenant_id: str = WORLD_TENANT,
    cleanup: bool = True,
    embed_batch: EmbedBatch | None = None,
    k: int = 5,
) -> dict[str, Any]:
    """Seed the world once, score its cases through ``search_facts``, clean up."""
    shadow = shadow_tenant_for(tenant_id or "")
    _require_eval_tenants([tenant_id, shadow])

    from robothor.db.connection import tenant_scope
    from robothor.memory.eval_world import generate_world, world_cases
    from robothor.memory.facts import search_facts

    started = time.perf_counter()
    world = generate_world(seed, size)
    cases = world_cases(world, split)
    tenants = [tenant_id, shadow]
    if cleanup:
        cleanup_world(tenants)  # residue from an interrupted run
    results: list[CaseResult | None] = []
    seed_s = query_s = 0.0
    try:
        t0 = time.perf_counter()
        await seed_world(world, tenant_id, shadow_tenant_id=shadow, embed_batch=embed_batch)
        seed_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        for case in cases:
            if case.stratum in _UNSCORED:
                results.append(None)
                continue
            with tenant_scope(tenant_id):
                found = await search_facts(case.question, limit=k, tenant_id=tenant_id)
            results.append(score_world_case(case, [r.get("fact_text", "") for r in found], k))
        query_s = time.perf_counter() - t0
    finally:
        if cleanup:
            cleanup_world(tenants)
    timings = {
        "seed_s": round(seed_s, 3),
        "query_s": round(query_s, 3),
        "total_s": round(time.perf_counter() - started, 3),
    }
    return build_world_report(
        cases,
        results,
        meta={"seed": seed, "size": size, "split": split, "k": k, "timings": timings},
    )
