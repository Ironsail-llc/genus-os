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
    recall_today            recall as main serves it: facts only (no segments, no graph
                            leg), the reranked head only (no budget fill)
    recall_budget           the same ranking as the head, then search_facts' unreranked
                            fused order until the budget is full (equal-budget recall)
    keyword_only            Postgres full-text over the seeded facts; no vectors, no reranker
    raw_verbatim            dense + keyword over raw transcript turns, plus the knowledge
                            base over the seeded documents, fused by reciprocal rank;
                            no extracted facts, no reranker
    full_context            no retrieval: the newest raw history that fits the budget
    full_context_unbounded  the same, filled to the answer model's context instead
                            (flagged equal_budget=False; never a fair comparison)
    search_memory_tool      product parity: the search_memory tool handler with its own
                            defaults, as the conversation's owner, after the product's
                            retention as of the question date; the answer model reads the
                            tool message itself
    evidence_only           a bound, no retrieval: the question's evidence sessions, whole,
                            in date order (the answering step's ceiling)
    full_history            a bound, no retrieval: every session of the history, whole

Tokens are estimated with the repo's ``block_budget.estimate_tokens``
(chars / 4).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from robothor.memory.block_budget import _CHARS_PER_TOKEN
from robothor.memory.block_budget import estimate_tokens as _estimate_tokens
from robothor.memory.recall import row_text

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
    #: What the context renderer reads besides the text (dates, conversation,
    #: relevance: ``context.render_fields``); empty when the system has none.
    meta: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)


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
    #: The exact text the answer model is handed, when the system produces it
    #: itself (a tool message); None to render the items.
    delivered: str | None = None

    @property
    def tokens(self) -> int:
        return sum(i.tokens for i in self.items)


def fill_budget(
    candidates: Sequence[tuple[str, str, str]], budget: int | None
) -> tuple[ContextItem, ...]:
    """Take ``(id, kind, text)`` candidates best first until the budget is full.

    Packed by the product's own rule, ``robothor.memory.recall.pack_within``
    (a candidate that does not fit is skipped and a later, smaller one may
    still go in), so the measured context is the one the tool ships.
    Duplicate ids keep their first (best) occurrence; ``rank`` is the
    position before packing.
    """
    from robothor.memory import recall

    seen: set[str] = set()
    ranked: list[tuple[int, str, str, str]] = []
    for rank, (item_id, kind, text) in enumerate(candidates, start=1):
        if item_id in seen:
            continue
        seen.add(item_id)
        ranked.append((rank, item_id, kind, text))

    def _cut(c: tuple[int, str, str, str], tokens: int) -> tuple[int, str, str, str]:
        # A token budget over text: the same characters-per-token estimate.
        chars = tokens * _CHARS_PER_TOKEN
        return (c[0], c[1], c[2], recall.truncate_text(c[3], chars))

    packed = recall.pack_within(ranked, budget, lambda c: estimate_tokens(c[3]), _cut)
    return tuple(
        ContextItem(item_id, kind, text, estimate_tokens(text), rank)
        for rank, item_id, kind, text in packed
    )


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
    """The product retrieval path, whatever legs the checked-out code has.

    Searches as the world's eval reader (``SeededWorld.reader_person_id``), so
    the reader's conversation segments are recalled; a segment enters the
    context as the transcript turn it starts with. ``include_segments`` /
    ``include_graph`` switch those legs off; a ``search_facts`` without them
    ignores them.
    """

    equal_budget = True

    def __init__(
        self,
        *,
        include_segments: bool = True,
        include_graph: bool | None = None,
        name: str = "recall",
    ) -> None:
        self.include_segments = include_segments
        self.include_graph = include_graph
        self.name = name

    async def retrieve(
        self, case: Case, *, world_handle: WorldHandle, token_budget: int
    ) -> ContextBundle:
        import inspect

        from robothor.db.connection import tenant_scope
        from robothor.memory.facts import search_facts

        started = time.perf_counter()
        tenant = world_handle.tenant_id
        accepted = set(inspect.signature(search_facts).parameters)
        options: dict[str, Any] = {
            "include_segments": self.include_segments,
            "include_graph": self.include_graph,
            "reader_person_id": getattr(world_handle.seeded, "reader_person_id", None),
        }
        extra = {k: v for k, v in options.items() if k in accepted}
        with tenant_scope(tenant):
            rows = await search_facts(case.question, limit=_CANDIDATES, tenant_id=tenant, **extra)
        cands = _fact_candidates(rows, world_handle)
        return _bundle(self.name, fill_budget(cands, token_budget), token_budget, True, started)


class RecallTodaySystem:
    """Recall as main serves it, for a before/after row on the same data.

    Main has no conversation segments, no graph leg and no budget fill, so it
    returns the reranked head only. This is the same ``search_facts`` with the
    segment and graph legs off, keeping only the rows the fill did not add
    (``budget_tail``). It reads extracted facts only: a store with no facts
    gives it nothing to return.
    """

    name = "recall_today"
    equal_budget = True

    async def retrieve(
        self, case: Case, *, world_handle: WorldHandle, token_budget: int
    ) -> ContextBundle:
        from robothor.db.connection import tenant_scope
        from robothor.memory.facts import search_facts

        started = time.perf_counter()
        tenant = world_handle.tenant_id
        reader = getattr(world_handle.seeded, "reader_person_id", None)
        with tenant_scope(tenant):
            rows = await search_facts(
                case.question,
                limit=_CANDIDATES,
                tenant_id=tenant,
                include_segments=False,
                include_graph=False,
                reader_person_id=reader,
            )
        head = [r for r in rows if not r.get("budget_tail")]
        cands = _fact_candidates(head, world_handle)
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
        # As ``recall``: the world's conversations are its eval reader's.
        reader = getattr(world_handle.seeded, "reader_person_id", None)
        with tenant_scope(tenant):
            head = await search_facts(
                case.question, limit=_CANDIDATES, tenant_id=tenant, reader_person_id=reader
            )
            tail = await search_facts(
                case.question,
                limit=_CANDIDATES,
                tenant_id=tenant,
                use_reranker=False,
                reader_person_id=reader,
            )
        cands = _fact_candidates([*head, *tail], world_handle)
        return _bundle(self.name, fill_budget(cands, token_budget), token_budget, True, started)


def _fact_candidates(
    rows: Sequence[dict[str, Any]], world_handle: WorldHandle
) -> list[tuple[str, str, str]]:
    """World ids for ``search_facts`` rows: a fact as its fact, a conversation
    segment as the transcript turn it starts with (a segment's ``id`` is from
    another table and is never read as a fact id)."""
    cands: list[tuple[str, str, str]] = []
    for row in rows:
        if row.get("kind") == "segment":
            turn = world_handle.turn_id(int(row.get("start_ref") or -1))
            if turn:
                cands.append((turn, "turn", str(row.get("fact_text", ""))))
            continue
        fid = world_handle.fact_id(row.get("id", -1))
        if fid is None:
            meta = row.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            fid = (meta.get("eval_world") or {}).get("fact_id")
        if fid:
            # As the model would read it in production (a graph-only row
            # names the path that relates it to the question).
            cands.append((fid, "fact", row_text(row)))
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


def _utc_now() -> datetime:
    """The wall clock the product's retention is measured from (patched in tests)."""
    return datetime.now(UTC)


def _question_date(case: Any) -> datetime | None:
    from robothor.memory.eval_bench_data import parse_longmemeval_date

    raw = getattr(case, "as_of", None)
    try:
        return parse_longmemeval_date(raw) if raw else None
    except ValueError:
        return None


class SearchMemoryToolSystem:
    """Product parity: what an agent's ``search_memory`` call returns, as it reads it.

    The tool handler is called with the query alone, so its own defaults
    apply: the limit (``recall.recall_limit``), the context cap
    (``RECALL_CONTEXT_CHARS``), the legs and fan-out the process flags give,
    and the reader rules, with the eval reader passed as the person whose
    conversation this is (``ctx.private_reader``), the one key to a private
    segment. Before the call the product's own nightly retention
    (``segments.cleanup_stale_segments``) runs as of the question's date, so
    segments older than ``SEGMENT_RETENTION_DAYS`` before it are gone. The
    answer model is handed the tool message an agent session builds from the
    result (``AgentSession.record_tool_call``: redacted, JSON, wrapped as
    untrusted content), unchanged. The token budget is ignored.

    It deletes segments, so the runner retrieves it after every harness cell
    of the same history, and questions on one history in date order.
    """

    name = "search_memory_tool"
    equal_budget = False
    product_parity = True

    async def retrieve(
        self, case: Case, *, world_handle: WorldHandle, token_budget: int
    ) -> ContextBundle:
        import asyncio

        from robothor.db.connection import tenant_scope
        from robothor.engine.private_recall import PrivateReader
        from robothor.engine.session import AgentSession
        from robothor.engine.tools.dispatch import ToolContext
        from robothor.engine.tools.handlers import memory as tool_memory
        from robothor.memory import segments

        started = time.perf_counter()
        tenant = world_handle.tenant_id
        as_of = _question_date(case)
        if as_of is not None:
            days = segments.SEGMENT_RETENTION_DAYS + max(0, (_utc_now() - as_of).days)
            with tenant_scope(tenant):
                await asyncio.to_thread(segments.cleanup_stale_segments, days, tenant)
        reader = getattr(world_handle.seeded, "reader_person_id", None)
        ctx = ToolContext(
            agent_id="memory-eval",
            tenant_id=tenant,
            is_benchmark=True,
            private_reader=PrivateReader(person_id=reader, user_id=None) if reader else None,
        )
        args = {"query": case.question}
        with tenant_scope(tenant):
            result = await tool_memory.HANDLERS["search_memory"](args, ctx, record_access=False)
        session = AgentSession("memory-eval", tenant_id=tenant)
        session.record_tool_call("search_memory", args, result, tool_call_id="call-0")
        delivered = str(session.messages[-1]["content"])
        items: list[ContextItem] = []
        for n, row in enumerate(result.get("results") or [], start=1):
            text = str(row.get("fact") or "")
            item_id: str | None
            if row.get("kind") == "segment":
                item_id, kind = world_handle.turn_id(int(row.get("start_ref") or -1)), "turn"
            else:
                rid = row.get("id")
                item_id, kind = (world_handle.fact_id(rid) if rid is not None else None), "fact"
            meta = {
                k: row[k] for k in ("source_ref", "started_at", "ended_at", "source") if k in row
            }
            items.append(
                ContextItem(item_id or f"row:{n}", kind, text, estimate_tokens(text), n, meta)
            )
        return ContextBundle(
            system=self.name,
            items=tuple(items),
            budget=None,
            equal_budget=False,
            abstained=not items,
            retrieval_ms=(time.perf_counter() - started) * 1000,
            delivered=delivered,
        )


class BenchmarkBoundSystem:
    """A bound built from the benchmark itself: no ingest, no retrieval, no database.

    ``evidence_only`` is the question's evidence sessions (LongMemEval
    ``answer_session_ids``), ``full_history`` every session of its history;
    both whole, oldest first, one item per session, dated in the
    benchmark's format. Evidence-only is the ceiling of the answering step:
    the context a perfect retrieval would deliver. The token budget is
    ignored.
    """

    equal_budget = False
    needs_ingest = False

    def __init__(self, *, evidence_only: bool) -> None:
        self.evidence_only = evidence_only
        self.name = "evidence_only" if evidence_only else "full_history"

    def from_benchmark(self, bench: Any, q: Any) -> ContextBundle:
        from robothor.memory.eval_bench import session_date_format

        started = time.perf_counter()
        fmt = session_date_format(bench.name)
        want = set(q.evidence_sessions)
        sessions = sorted(bench.histories[q.history_id].sessions, key=lambda s: s.at)
        items = []
        for n, session in enumerate(
            (s for s in sessions if not self.evidence_only or s.id in want), start=1
        ):
            text = "\n".join(f"{t.role.capitalize()}: {t.text}" for t in session.turns)
            items.append(
                ContextItem(
                    f"{session.id}#0u",
                    "turn",
                    text,
                    estimate_tokens(text),
                    n,
                    {"date": fmt(session.at), "session": session.id, "position": 0},
                )
            )
        return _bundle(self.name, tuple(items), None, False, started)

    async def retrieve(
        self, case: Case, *, world_handle: WorldHandle, token_budget: int
    ) -> ContextBundle:
        raise RuntimeError(f"{self.name} is built from the benchmark (from_benchmark)")


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

_REGISTRY: dict[str, Callable[[], MemorySystem]] = {
    "recall": RecallSystem,
    "recall_budget": RecallBudgetSystem,
    "recall_today": RecallTodaySystem,
    "recall_no_segments": lambda: RecallSystem(include_segments=False, name="recall_no_segments"),
    "recall_no_graph": lambda: RecallSystem(include_graph=False, name="recall_no_graph"),
    "recall_with_graph": lambda: RecallSystem(include_graph=True, name="recall_with_graph"),
    "keyword_only": KeywordOnlySystem,
    "raw_verbatim": RawVerbatimSystem,
    "full_context": FullContextSystem,
    "full_context_unbounded": lambda: FullContextSystem(unbounded=True),
    "search_memory_tool": SearchMemoryToolSystem,
    "evidence_only": lambda: BenchmarkBoundSystem(evidence_only=True),
    "full_history": lambda: BenchmarkBoundSystem(evidence_only=False),
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
