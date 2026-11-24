"""Associative recall: bounded activation over the memory graph.

When an agent works on a person, company or task, what is *connected* to it
should come to mind without being asked. This module links a piece of text to
the entities it names (``seeds_from_text``) and spreads activation from them
over the graph (``activate``), returning recall :class:`Candidate` facts with a
``graph`` leg score, the hop distance and the path of entity names that reached
each one — so the agent can see why a fact was recalled.

The walk is the database function ``memory_activate`` (migration
``*_memory_graph_activation.sql``): a forward-push approximation of
Personalized PageRank, at most :data:`HARD_MAX_HOPS` hops, fan-out capped per
node, damped per hop, seeds weighted by inverse visible degree, and an
``exclude`` list that is neither seed nor transit. It enforces tenant, reader
visibility and trust itself:

* the walk crosses only edges with at least one supporting fact the reader can
  see (shared, or scoped to the reader), and only those edges count toward a
  node's degree — a path through someone else's private evidence changes no
  score;
* ``trusted_only`` (automatic recall in warm-up) follows only edges supported
  by operator-confirmed, user-stated or CRM facts, never an ``inferred`` or
  ``external`` edge, and returns only facts of those origins. NULL origin —
  every row written before origins existed — is *not* trusted, so over legacy
  data this mode returns nothing; warm-up then falls back to its name-matching
  path. A deliberate search (``trusted_only=False``) walks every origin and
  labels each fact with it.

Every function here is synchronous and does blocking database I/O: an async
caller goes through ``asyncio.to_thread``. Nothing here writes.

Seed linking is a small read-only matcher (:func:`seeds_from_text`): exact full
names and aliases (case-insensitive, never a single token on its own), e-mail
addresses and @handles as strong keys. The identity work builds a fuller
read-only resolver; when it lands, :func:`_link_seeds` is the one place to
swap.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg2.extras import RealDictCursor

from robothor.memory.graph_schema import normalize_origin
from robothor.memory.recall import Candidate

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

logger = logging.getLogger(__name__)

# ── Tunables. Starting values; the memory evaluation tunes them. ────────────

#: Frontier expansions by default: A → B → C.
MAX_HOPS = 2
#: The database function never walks further than this, whatever is asked.
HARD_MAX_HOPS = 3
#: Strongest edges (by weight, then recency) a node spreads along per hop.
FANOUT = 16
#: Share of a node's activation passed on per hop.
DAMPING = 0.7
#: Best facts returned per activated entity.
FACTS_PER_ENTITY = 3
#: Facts returned by one activation.
LIMIT = 20

#: Seed weight of an entity named in the text, and of one mentioned by a top
#: search hit (a step removed from what was asked).
TEXT_SEED_WEIGHT = 1.0
MENTION_SEED_WEIGHT = 0.5
#: Top search hits whose mentioned entities seed the search's graph leg.
SEARCH_SEED_HITS = 5
#: At most this many seeds per activation.
MAX_SEEDS = 12

#: Seed linking: longest name looked up, and phrases tried per text.
MAX_NAME_TOKENS = 4
MAX_PHRASES = 400

_WORD = re.compile(r"[^\W_]+(?:['’.-][^\W_]+)*", re.UNICODE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_][A-Za-z0-9_.]{1,63}")


@dataclass(frozen=True)
class Seed:
    """An entity the activation starts from."""

    entity_id: int
    name: str
    weight: float = TEXT_SEED_WEIGHT
    method: str = "full_name"


def clamp_hops(hops: int) -> int:
    """``hops`` within 0..:data:`HARD_MAX_HOPS` (the function clamps the same way)."""
    return max(0, min(int(hops), HARD_MAX_HOPS))


def candidate_phrases(text: str) -> list[str]:
    """Lower-cased word runs of 2..:data:`MAX_NAME_TOKENS` tokens, shortest first.

    A single token is never a candidate: a first name alone must not resolve to
    a person.
    """
    words = [w.lower() for w in _WORD.findall(text or "")]
    out: list[str] = []
    seen: set[str] = set()
    for size in range(2, MAX_NAME_TOKENS + 1):
        for i in range(len(words) - size + 1):
            phrase = " ".join(words[i : i + size])
            if phrase not in seen:
                seen.add(phrase)
                out.append(phrase)
                if len(out) >= MAX_PHRASES:
                    return out
    return out


def strong_keys(text: str) -> list[str]:
    """E-mail addresses and @handles in ``text``, lower-cased, in order."""
    out: list[str] = []
    for match in [*_EMAIL.findall(text or ""), *_HANDLE.findall(text or "")]:
        key = match.lower().rstrip(".")
        if key not in out:
            out.append(key)
    return out


@contextmanager
def _cursor(connect: Callable[..., Any] | None) -> Iterator[Any]:
    if connect is None:
        # Looked up per call so a patched robothor.db.get_connection applies.
        import robothor.db

        connect = robothor.db.get_connection
    with connect() as conn:
        yield conn.cursor(cursor_factory=RealDictCursor)


def _as_uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value else None
    except (TypeError, ValueError):
        return None


_LINK_SQL = """
    SELECT id, name, method FROM (
        SELECT e.id, e.name, 'full_name' AS method, 1 AS pri
          FROM memory_entities e
         WHERE e.tenant_id = %(t)s AND lower(e.name) = ANY(%(phrases)s::text[])
        UNION ALL
        SELECT e.id, e.name, 'alias', 2
          FROM memory_entities e
         WHERE e.tenant_id = %(t)s AND e.aliases <> '{}'
           AND EXISTS (SELECT 1 FROM unnest(e.aliases) AS a(alias)
                        WHERE lower(a.alias) = ANY(%(phrases)s::text[]))
        UNION ALL
        SELECT e.id, e.name, 'strong_key', 0
          FROM contact_identifiers ci
          JOIN memory_entities e
            ON e.tenant_id = %(t)s
           AND (e.id = ci.memory_entity_id
                OR (ci.memory_entity_id IS NULL AND e.crm_person_id = ci.person_id))
         WHERE ci.tenant_id = %(t)s AND lower(ci.identifier) = ANY(%(keys)s::text[])
        UNION ALL
        SELECT e.id, e.name, 'strong_key', 0
          FROM memory_entities e
         WHERE e.tenant_id = %(t)s AND e.crm_person_id = %(sender)s::uuid
        UNION ALL
        SELECT e.id, e.name, 'context', 3
          FROM memory_entities e
         WHERE e.tenant_id = %(t)s AND e.id = ANY(%(extra)s::int[])
    ) AS s
    ORDER BY pri, id
"""


def _link_seeds(
    cur: Any,
    tenant_id: str,
    text: str,
    *,
    extra_entity_ids: Iterable[int] = (),
    sender_person_id: str | None = None,
) -> list[Seed]:
    """The read-only matcher behind :func:`seeds_from_text`, on an open cursor."""
    phrases = candidate_phrases(text)
    keys = strong_keys(text)
    # A handle may be stored with or without its "@".
    keys += [k[1:] for k in keys if k.startswith("@")]
    extra = sorted({int(i) for i in extra_entity_ids})
    sender = _as_uuid(sender_person_id)
    if not (phrases or keys or extra or sender):
        return []
    cur.execute(
        _LINK_SQL,
        {"t": tenant_id, "phrases": phrases, "keys": keys, "sender": sender, "extra": extra},
    )
    seeds: dict[int, Seed] = {}
    for row in cur.fetchall():
        eid = int(row["id"])
        if eid not in seeds:
            seeds[eid] = Seed(entity_id=eid, name=str(row["name"]), method=str(row["method"]))
    return list(seeds.values())


def seeds_from_text(
    tenant_id: str,
    text: str,
    *,
    extra_entity_ids: Iterable[int] = (),
    connect: Callable[..., Any] | None = None,
) -> list[Seed]:
    """Entities ``text`` names, plus ``extra_entity_ids`` that are in the tenant.

    No model call. Exact full-name and alias matches (case-insensitive, on the
    ``(tenant_id, lower(name))`` index) of word runs of two or more tokens, and
    e-mail addresses or @handles matched against ``contact_identifiers``. A
    single token never resolves on its own.
    """
    extra = list(extra_entity_ids)
    if not candidate_phrases(text) and not strong_keys(text) and not extra:
        return []
    with _cursor(connect) as cur:
        return _link_seeds(cur, tenant_id, text, extra_entity_ids=extra)


def owner_entity_ids(cur: Any, tenant_id: str) -> list[int]:
    """The operator's own entities (via ``get_owner_person``), to exclude from a walk."""
    try:
        from robothor.crm.dal import get_owner_person

        owner_id = _as_uuid((get_owner_person(tenant_id) or {}).get("id"))
    except Exception as e:  # noqa: BLE001 - no operator configured is not an error here
        logger.debug("activation: operator lookup failed: %s", e)
        return []
    if owner_id is None:
        return []
    cur.execute(
        "SELECT id FROM memory_entities WHERE tenant_id = %s AND crm_person_id = %s::uuid",
        (tenant_id, owner_id),
    )
    return [int(r["id"]) for r in cur.fetchall()]


_ACTIVATE_SQL = """
    SELECT kind, entity_id, entity_name, score, hops, via_edge_id, path_names,
           fact_id, fact_text, category, origin, importance_score, created_at,
           person_id, entities, access_count
      FROM memory_activate(
          %(t)s, %(seeds)s::int[], %(weights)s::float8[], %(reader)s, %(hops)s,
          %(fanout)s, %(damping)s, %(exclude)s::int[], %(trusted)s, %(limit)s,
          %(per)s, %(restricted)s, %(person)s::uuid)
     WHERE kind = 'fact'
"""


def _candidates(rows: Sequence[dict[str, Any]]) -> list[Candidate]:
    now = datetime.now(UTC)
    out: list[Candidate] = []
    for r in rows:
        created = r.get("created_at")
        raw_origin = r.get("origin")
        payload = {
            "id": r["fact_id"],
            "fact_text": r.get("fact_text") or "",
            "category": r.get("category"),
            "entities": list(r.get("entities") or []),
            "importance_score": r.get("importance_score"),
            "access_count": r.get("access_count"),
            "created_at": created,
            "age_seconds": (now - created).total_seconds() if created is not None else None,
            "person_id": r.get("person_id"),
            "origin": normalize_origin(raw_origin),
            "origin_recorded": raw_origin,
            "source": "graph",
            "graph_score": float(r["score"]),
            "graph_hops": int(r["hops"]),
            "graph_path": list(r.get("path_names") or [r.get("entity_name")]),
            "graph_entity": r.get("entity_name"),
            "graph_entity_id": r.get("entity_id"),
            "graph_edge_id": r.get("via_edge_id"),
        }
        out.append(
            Candidate(
                id=r["fact_id"],
                kind="fact",
                text=payload["fact_text"],
                leg_scores={"graph": float(r["score"])},
                payload=payload,
            )
        )
    return out


def _activate_on(
    cur: Any,
    tenant_id: str,
    seeds: Sequence[Seed],
    *,
    reader_scope_id: str | None,
    trusted_only: bool,
    max_hops: int,
    fanout: int,
    damping: float,
    limit: int,
    facts_per_entity: int,
    exclude_entity_ids: Iterable[int],
    person_restricted: bool,
    person_id: str | None,
    statement_timeout_ms: int | None,
) -> list[Candidate]:
    if not seeds:
        return []
    if statement_timeout_ms:
        # Transaction-local: the pooled connection keeps its own setting.
        cur.execute(
            "SELECT set_config('statement_timeout', %s, true)", (f"{int(statement_timeout_ms)}ms",)
        )
    seeds = list(seeds)[:MAX_SEEDS]
    cur.execute(
        _ACTIVATE_SQL,
        {
            "t": tenant_id,
            "seeds": [s.entity_id for s in seeds],
            "weights": [float(s.weight) for s in seeds],
            "reader": reader_scope_id,
            "hops": clamp_hops(max_hops),
            "fanout": int(fanout),
            "damping": float(damping),
            "exclude": sorted({int(i) for i in exclude_entity_ids}),
            "trusted": bool(trusted_only),
            "limit": int(limit),
            "per": int(facts_per_entity),
            "restricted": bool(person_restricted),
            "person": _as_uuid(person_id),
        },
    )
    return _candidates(cur.fetchall())


def activate(
    tenant_id: str,
    seeds: Sequence[Seed],
    *,
    trusted_only: bool,
    reader_scope_id: str | None = None,
    max_hops: int = MAX_HOPS,
    fanout: int = FANOUT,
    damping: float = DAMPING,
    limit: int = LIMIT,
    facts_per_entity: int = FACTS_PER_ENTITY,
    exclude_entity_ids: Iterable[int] = (),
    person_restricted: bool = False,
    person_id: str | None = None,
    statement_timeout_ms: int | None = None,
    connect: Callable[..., Any] | None = None,
) -> list[Candidate]:
    """Spread activation from ``seeds``; the facts it reaches, best first.

    Each is a ``Candidate`` of kind ``fact`` with ``leg_scores["graph"]`` and a
    payload carrying ``graph_hops``, ``graph_path`` (entity names from a seed to
    the entity the fact is about), ``origin`` (normalised; NULL reads as
    ``agent_generated``) and ``origin_recorded`` (as stored). ``reader_scope_id``
    is the reader's person id: the walk sees shared evidence and evidence scoped
    to that reader, nothing else. ``person_restricted``/``person_id`` apply the
    "own data + shared" person scope on top. Deterministic for the same data.
    """
    if not seeds:
        return []
    with _cursor(connect) as cur:
        return _activate_on(
            cur,
            tenant_id,
            seeds,
            reader_scope_id=reader_scope_id,
            trusted_only=trusted_only,
            max_hops=max_hops,
            fanout=fanout,
            damping=damping,
            limit=limit,
            facts_per_entity=facts_per_entity,
            exclude_entity_ids=exclude_entity_ids,
            person_restricted=person_restricted,
            person_id=person_id,
            statement_timeout_ms=statement_timeout_ms,
        )


def _mentioned_by(cur: Any, tenant_id: str, fact_ids: Sequence[int]) -> list[Seed]:
    """Entities the given facts mention, in the order of the facts."""
    if not fact_ids:
        return []
    cur.execute(
        """
        SELECT DISTINCT ON (mm.entity_id) mm.entity_id, e.name, o.ord
          FROM unnest(%s::int[]) WITH ORDINALITY AS o(fact_id, ord)
          JOIN memory_mentions mm ON mm.fact_id = o.fact_id AND mm.tenant_id = %s
          JOIN memory_entities e ON e.id = mm.entity_id AND e.tenant_id = %s
         ORDER BY mm.entity_id, o.ord
        """,
        (list(fact_ids), tenant_id, tenant_id),
    )
    rows = sorted(cur.fetchall(), key=lambda r: (int(r["ord"]), int(r["entity_id"])))
    return [
        Seed(
            entity_id=int(r["entity_id"]),
            name=str(r["name"]),
            weight=MENTION_SEED_WEIGHT,
            method="mention",
        )
        for r in rows
    ]


def graph_candidates_for_query(
    tenant_id: str,
    query: str,
    seed_fact_ids: Sequence[int] = (),
    *,
    reader_scope_id: str | None = None,
    person_restricted: bool = False,
    person_id: str | None = None,
    limit: int = LIMIT,
    connect: Callable[..., Any] | None = None,
) -> list[Candidate]:
    """The ``graph`` leg of ``search_facts``: one connection, read-only.

    Seeds are the entities the query names and the entities the top hits of
    the other legs (``seed_fact_ids``) mention. The operator's own entity is
    excluded as seed and transit. A deliberate search, so every origin is
    walked (``trusted_only=False``) and each row carries its origin.

    It runs under a savepoint, so a failure (a statement timeout, a database
    not yet migrated) rolls back only this leg and never aborts a transaction
    the connection shares with the caller.
    """
    with _cursor(connect) as cur:
        cur.execute("SAVEPOINT memory_graph_leg")
        try:
            found = _query_graph(
                cur,
                tenant_id,
                query,
                seed_fact_ids,
                reader_scope_id=reader_scope_id,
                person_restricted=person_restricted,
                person_id=person_id,
                limit=limit,
            )
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT memory_graph_leg")
            raise
        cur.execute("RELEASE SAVEPOINT memory_graph_leg")
        return found


def _query_graph(
    cur: Any,
    tenant_id: str,
    query: str,
    seed_fact_ids: Sequence[int],
    *,
    reader_scope_id: str | None,
    person_restricted: bool,
    person_id: str | None,
    limit: int,
) -> list[Candidate]:
    seeds = _link_seeds(cur, tenant_id, query)
    known = {s.entity_id for s in seeds}
    seeds += [s for s in _mentioned_by(cur, tenant_id, seed_fact_ids) if s.entity_id not in known]
    if not seeds:
        return []
    return _activate_on(
        cur,
        tenant_id,
        seeds,
        reader_scope_id=reader_scope_id,
        trusted_only=False,
        max_hops=MAX_HOPS,
        fanout=FANOUT,
        damping=DAMPING,
        limit=limit,
        facts_per_entity=FACTS_PER_ENTITY,
        exclude_entity_ids=owner_entity_ids(cur, tenant_id),
        person_restricted=person_restricted,
        person_id=person_id,
        statement_timeout_ms=None,
    )


def recall_for_message(
    tenant_id: str,
    message: str,
    *,
    sender_person_id: str | None = None,
    reader_scope_id: str | None = None,
    exclude_names: Iterable[str] | None = None,
    person_restricted: bool = False,
    person_id: str | None = None,
    trusted_only: bool = True,
    limit: int = LIMIT,
    statement_timeout_ms: int | None = None,
    connect: Callable[..., Any] | None = None,
) -> list[Candidate]:
    """Automatic recall for one inbound message (warm-up). No model call.

    Seeds: entities the message names (except ``exclude_names``) and the
    sender's own entity, linked by its CRM person. The operator's entity is
    excluded as seed and transit. ``trusted_only`` by default: only
    operator-confirmed, user-stated and CRM evidence.
    """
    excluded = {n.lower() for n in (exclude_names or ()) if n}
    sender = _as_uuid(sender_person_id)
    if not candidate_phrases(message) and not strong_keys(message) and sender is None:
        return []
    with _cursor(connect) as cur:
        seeds = [
            s
            for s in _link_seeds(cur, tenant_id, message, sender_person_id=sender)
            if s.method == "strong_key" or s.name.lower() not in excluded
        ]
        if not seeds:
            return []
        return _activate_on(
            cur,
            tenant_id,
            seeds,
            reader_scope_id=reader_scope_id,
            trusted_only=trusted_only,
            max_hops=MAX_HOPS,
            fanout=FANOUT,
            damping=DAMPING,
            limit=limit,
            facts_per_entity=FACTS_PER_ENTITY,
            exclude_entity_ids=owner_entity_ids(cur, tenant_id),
            person_restricted=person_restricted,
            person_id=person_id,
            statement_timeout_ms=statement_timeout_ms,
        )
