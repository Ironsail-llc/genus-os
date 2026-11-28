"""Systems under test for the memory eval's answer arm.

Every system turns a case into a :class:`ContextBundle`: ordered text items
with world ids, the token budget they were filled to, and timing. The answer
step only ever sees a bundle, so systems compete at an equal context size,
and adding a system means implementing :class:`MemorySystem` and calling
:func:`register_system`. Neither the runner nor the judge changes. The
observation-log memory and the tool-using raw-history agent that later
stories add plug in the same way.

Built in:

    recall                  the product path: search_facts, budget filled in rank order
    recall_budget           the same ranking as the head, then search_facts' unreranked
                            fused order until the budget is full (equal-budget recall)
    keyword_only            Postgres full-text over the seeded facts; no vectors, no reranker
    raw_verbatim            dense + keyword over raw transcript turns, plus the knowledge
                            base over the seeded documents, fused by reciprocal rank;
                            no extracted facts, no reranker
    full_context            no retrieval: the newest raw history that fits the budget
    full_context_unbounded  the same, filled to the answer model's context instead
                            (flagged equal_budget=False; never a fair comparison)

Tokens are estimated with the repo's ``block_budget.estimate_tokens``
(chars / 4).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from robothor.memory.block_budget import estimate_tokens as _estimate_tokens

if TYPE_CHECKING:
    from robothor.memory.eval_world import Case, World
    from robothor.memory.eval_world_seed import SeededWorld

EmbedOne = Callable[[str], Awaitable[list[float]]]

#: How much the unbounded full-context variant may hold. qwen3:8b trains on
#: 40,960 tokens; this leaves room for the prompt and the answer.
UNBOUNDED_TOKENS = 24_000

_CANDIDATES = 60
_RRF_K = 60


def estimate_tokens(text: str) -> int:
    return _estimate_tokens(text)


@dataclass(frozen=True)
class ContextItem:
    #: World id: a fact id, a turn id, or ``<document id>#c<chunk>``.
    id: str
    #: ``fact``, ``turn`` or ``document``.
    kind: str
    text: str
    tokens: int
    #: Position in the system's ranking (1 = best), before budget filling.
    rank: int


@dataclass(frozen=True)
class ContextBundle:
    system: str
    items: tuple[ContextItem, ...]
    #: Token budget the items were filled to; ``None`` when unbounded.
    budget: int | None
    #: False for variants that ignore the shared budget.
    equal_budget: bool
    #: The system itself declined (nothing retrieved).
    abstained: bool
    retrieval_ms: float

    @property
    def tokens(self) -> int:
        return sum(i.tokens for i in self.items)


def fill_budget(
    candidates: Sequence[tuple[str, str, str]], budget: int | None
) -> tuple[ContextItem, ...]:
    """Take ``(id, kind, text)`` candidates best first until the budget is full.

    A candidate that does not fit is skipped and a later, smaller one may
    still go in, so the budget is used, never exceeded, and the kept items stay
    in rank order. Duplicate ids keep their first (best) occurrence.
    """
    out: list[ContextItem] = []
    used = 0
    seen: set[str] = set()
    for rank, (item_id, kind, text) in enumerate(candidates, start=1):
        if item_id in seen:
            continue
        seen.add(item_id)
        tokens = estimate_tokens(text)
        if budget is not None and used + tokens > budget:
            continue
        out.append(ContextItem(item_id, kind, text, tokens, rank))
        used += tokens
    return tuple(out)


@dataclass
class WorldHandle:
    """What a system needs to retrieve against a seeded world."""

    world: World
    seeded: SeededWorld
    tenant_id: str
    shadow_tenant_id: str
    embed_query: EmbedOne
    _fact_by_db: dict[int, str] = field(default_factory=dict, init=False, repr=False)
    _turn_by_msg: dict[int, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        from robothor.memory.eval_world_seed import _require_eval_tenants

        _require_eval_tenants([self.tenant_id, self.shadow_tenant_id])
        self._fact_by_db = {v: k for k, v in self.seeded.fact_ids.items()}
        self._turn_by_msg = {v: k for k, v in self.seeded.turn_message_ids.items()}

    def fact_id(self, db_id: int) -> str | None:
        return self._fact_by_db.get(int(db_id))

    def turn_id(self, message_id: int) -> str | None:
        return self._turn_by_msg.get(int(message_id))


@runtime_checkable
class MemorySystem(Protocol):
    name: str
    equal_budget: bool

    async def retrieve(
        self, case: Case, *, world_handle: WorldHandle, token_budget: int
    ) -> ContextBundle: ...


def _bundle(
    name: str,
    items: tuple[ContextItem, ...],
    budget: int | None,
    equal: bool,
    started: float,
) -> ContextBundle:
    return ContextBundle(
        system=name,
        items=items,
        budget=budget,
        equal_budget=equal,
        abstained=not items,
        retrieval_ms=(time.perf_counter() - started) * 1000,
    )


_WORD = re.compile(r"[a-z0-9]+")


def or_tsquery(question: str) -> str:
    """``a | b | c`` over the question's words, for ``to_tsquery``."""
    words: list[str] = []
    for w in _WORD.findall(question.lower()):
        if len(w) >= 2 and w not in words:
            words.append(w)
    return " | ".join(words[:32])


def _turn_text(at: str, role: str, text: str) -> str:
    return f"[{at[:10]}] {role}: {text}"


# --------------------------------------------------------------------------- #
# Systems
# --------------------------------------------------------------------------- #


class RecallSystem:
    """The product retrieval path, whatever legs the checked-out code has."""

    name = "recall"
    equal_budget = True

    async def retrieve(
        self, case: Case, *, world_handle: WorldHandle, token_budget: int
    ) -> ContextBundle:
        from robothor.db.connection import tenant_scope
        from robothor.memory.facts import search_facts

        started = time.perf_counter()
        tenant = world_handle.tenant_id
        with tenant_scope(tenant):
            rows = await search_facts(case.question, limit=_CANDIDATES, tenant_id=tenant)
        cands: list[tuple[str, str, str]] = []
        for row in rows:
            fid = world_handle.fact_id(row.get("id", -1))
            if fid is None:
                meta = row.get("metadata") or {}
                if isinstance(meta, str):
                    meta = json.loads(meta)
                fid = (meta.get("eval_world") or {}).get("fact_id")
            if fid:
                cands.append((fid, "fact", str(row.get("fact_text", ""))))
        return _bundle(self.name, fill_budget(cands, token_budget), token_budget, True, started)


class RecallBudgetSystem:
    """The product path, then more of its own candidates until the budget is full.

    ``search_facts`` reranks and returns at most ``MEMORY_RERANK_MAX_CANDIDATES``
    rows (16 by default), so the product fills about an eighth of a 2,048-token
    budget. This variant keeps the product's ranking as the head and appends
    ``search_facts``' own unreranked fused order behind it, so the table can show
    both what the product returns today and what the same retrieval holds at an
    equal budget. ``search_facts`` itself is unchanged: its legs fetch about 30
    candidates each, so the tail is bounded by what those legs return.
    """

    name = "recall_budget"
    equal_budget = True

    async def retrieve(
        self, case: Case, *, world_handle: WorldHandle, token_budget: int
    ) -> ContextBundle:
        from robothor.db.connection import tenant_scope
        from robothor.memory.facts import search_facts

        started = time.perf_counter()
        tenant = world_handle.tenant_id
        with tenant_scope(tenant):
            head = await search_facts(case.question, limit=_CANDIDATES, tenant_id=tenant)
            tail = await search_facts(
                case.question, limit=_CANDIDATES, tenant_id=tenant, use_reranker=False
            )
        cands = _fact_candidates([*head, *tail], world_handle)
        return _bundle(self.name, fill_budget(cands, token_budget), token_budget, True, started)


def _fact_candidates(
    rows: Sequence[dict[str, Any]], world_handle: WorldHandle
) -> list[tuple[str, str, str]]:
    cands: list[tuple[str, str, str]] = []
    for row in rows:
        fid = world_handle.fact_id(row.get("id", -1))
        if fid is None:
            meta = row.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            fid = (meta.get("eval_world") or {}).get("fact_id")
        if fid:
            cands.append((fid, "fact", str(row.get("fact_text", ""))))
    return cands


class KeywordOnlySystem:
    """Postgres full-text over the seeded facts: no vectors, no reranker."""

    name = "keyword_only"
    equal_budget = True

    async def retrieve(
        self, case: Case, *, world_handle: WorldHandle, token_budget: int
    ) -> ContextBundle:
        import asyncio

        started = time.perf_counter()
        query = or_tsquery(case.question)
        rows = await asyncio.to_thread(_keyword_facts, world_handle.tenant_id, query)
        cands = [
            (fid, "fact", text)
            for db_id, text in rows
            if (fid := world_handle.fact_id(db_id)) is not None
        ]
        return _bundle(self.name, fill_budget(cands, token_budget), token_budget, True, started)


def _keyword_facts(tenant: str, query: str) -> list[tuple[int, str]]:
    if not query:
        return []
    from robothor.db.connection import get_connection, tenant_scope

    with tenant_scope(tenant), get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, fact_text
              FROM memory_facts, to_tsquery('english', %s) q
             WHERE tenant_id = %s AND is_active = TRUE AND tsv @@ q
             ORDER BY ts_rank_cd(tsv, q) DESC, id
             LIMIT %s
            """,
            (query, tenant, _CANDIDATES),
        )
        return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


class RawVerbatimSystem:
    """Raw turns and document chunks, dense + keyword, fused by reciprocal rank."""

    name = "raw_verbatim"
    equal_budget = True

    async def retrieve(
        self, case: Case, *, world_handle: WorldHandle, token_budget: int
    ) -> ContextBundle:
        import asyncio

        from robothor.db.connection import tenant_scope
        from robothor.engine.chat_store import search_chat_turns
        from robothor.knowledge.search import search as kb_search
        from robothor.memory.eval_world_seed import WORLD_COLLECTION

        started = time.perf_counter()
        tenant = world_handle.tenant_id
        vector = await world_handle.embed_query(case.question)

        def _dense() -> list[dict[str, Any]]:
            with tenant_scope(tenant):
                return search_chat_turns(vector, limit=_CANDIDATES, tenant_id=tenant)

        dense = await asyncio.to_thread(_dense)
        keyword = await asyncio.to_thread(_keyword_turns, tenant, or_tsquery(case.question))

        async def _embed(text: str) -> list[float]:
            return vector if text == case.question else await world_handle.embed_query(text)

        with tenant_scope(tenant):
            docs = await kb_search(tenant, WORLD_COLLECTION, case.question, limit=10, embed=_embed)

        texts: dict[str, tuple[str, str]] = {}
        scores: dict[str, float] = {}

        def _add(rank: int, item_id: str | None, kind: str, text: str) -> None:
            if not item_id:
                return
            texts.setdefault(item_id, (kind, text))
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (_RRF_K + rank)

        for rank, row in enumerate(dense, start=1):
            tid = world_handle.turn_id(row["id"])
            at = row["created_at"].isoformat() if row.get("created_at") else ""
            _add(rank, tid, "turn", _turn_text(at, row.get("role", "?"), row.get("content", "")))
        for rank, (mid, at, role, content) in enumerate(keyword, start=1):
            _add(rank, world_handle.turn_id(mid), "turn", _turn_text(at, role, content))
        for rank, hit in enumerate(docs, start=1):
            _add(
                rank,
                f"{hit['doc_id']}#c{hit['chunk_ix']}",
                "document",
                f"{hit.get('title', '')}: {hit.get('text', '')}",
            )
        ordered = sorted(scores, key=lambda k: (-scores[k], k))
        cands = [(k, texts[k][0], texts[k][1]) for k in ordered]
        return _bundle(self.name, fill_budget(cands, token_budget), token_budget, True, started)


def _keyword_turns(tenant: str, query: str) -> list[tuple[int, str, str, str]]:
    if not query:
        return []
    from robothor.db.connection import get_connection, tenant_scope

    with tenant_scope(tenant), get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.created_at, m.message->>'role', m.message->>'content'
              FROM chat_messages m
              JOIN chat_sessions s ON s.id = m.session_id,
                   to_tsquery('english', %s) q
             WHERE s.tenant_id = %s
               AND to_tsvector('english', coalesce(m.message->>'content', '')) @@ q
             ORDER BY ts_rank_cd(to_tsvector('english', coalesce(m.message->>'content', '')), q)
                      DESC, m.id
             LIMIT %s
            """,
            (query, tenant, _CANDIDATES),
        )
        return [
            (int(r[0]), r[1].isoformat(), str(r[2] or "?"), str(r[3] or "")) for r in cur.fetchall()
        ]


class FullContextSystem:
    """No retrieval: the newest raw history that fits, in chronological order."""

    def __init__(self, *, unbounded: bool = False, max_tokens: int = UNBOUNDED_TOKENS) -> None:
        self.unbounded = unbounded
        self.max_tokens = max_tokens
        self.name = "full_context_unbounded" if unbounded else "full_context"
        self.equal_budget = not unbounded

    async def retrieve(
        self, case: Case, *, world_handle: WorldHandle, token_budget: int
    ) -> ContextBundle:
        import asyncio

        started = time.perf_counter()
        budget = self.max_tokens if self.unbounded else token_budget
        rows = await asyncio.to_thread(_newest_turns, world_handle.tenant_id)
        newest_first = [
            (tid, "turn", _turn_text(at, role, content))
            for mid, at, role, content in rows
            if (tid := world_handle.turn_id(mid)) is not None
        ]
        picked = fill_budget(newest_first, budget)
        chronological = tuple(
            ContextItem(i.id, i.kind, i.text, i.tokens, n)
            for n, i in enumerate(reversed(picked), start=1)
        )
        return _bundle(self.name, chronological, budget, self.equal_budget, started)


def _newest_turns(tenant: str) -> list[tuple[int, str, str, str]]:
    from robothor.db.connection import get_connection, tenant_scope

    with tenant_scope(tenant), get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.created_at, m.message->>'role', m.message->>'content'
              FROM chat_messages m JOIN chat_sessions s ON s.id = m.session_id
             WHERE s.tenant_id = %s
             ORDER BY m.created_at DESC, m.id DESC
            """,
            (tenant,),
        )
        return [
            (int(r[0]), r[1].isoformat(), str(r[2] or "?"), str(r[3] or "")) for r in cur.fetchall()
        ]


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, Callable[[], MemorySystem]] = {
    "recall": RecallSystem,
    "recall_budget": RecallBudgetSystem,
    "keyword_only": KeywordOnlySystem,
    "raw_verbatim": RawVerbatimSystem,
    "full_context": FullContextSystem,
    "full_context_unbounded": lambda: FullContextSystem(unbounded=True),
}

DEFAULT_SYSTEMS: tuple[str, ...] = (
    "recall",
    "recall_budget",
    "keyword_only",
    "raw_verbatim",
    "full_context",
)


def register_system(name: str, factory: Callable[[], MemorySystem]) -> None:
    """Add a system. The runner and judge pick it up with no other change."""
    _REGISTRY[name] = factory


def unregister_system(name: str) -> None:
    _REGISTRY.pop(name, None)


def available_systems() -> list[str]:
    return sorted(_REGISTRY)


def make_systems(names: Sequence[str]) -> list[MemorySystem]:
    unknown = [n for n in names if n not in _REGISTRY]
    if unknown:
        raise ValueError(
            f"unknown system(s) {unknown} (available: {', '.join(available_systems())})"
        )
    return [_REGISTRY[n]() for n in names]
