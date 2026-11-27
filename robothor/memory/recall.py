"""The recall-leg contract: named legs in, one fused candidate list out.

A **leg** is anything that turns a query into a ranked list of
:class:`Candidate` objects, best first: the vector and keyword legs over
``memory_facts``, conversation segments (``segment``, which the segment store
fuses from its own dense and keyword legs), graph activation, entity-graph
expansion, insights, episodes and chat turns. A leg must return:

- ``id`` and ``kind`` — together the identity of the item. The same fact found
  by two legs is merged; a fact and an episode that share an integer id are not.
- ``text`` — what the reranker reads.
- ``similarity`` if the leg has a cosine similarity (``None`` otherwise — the
  ranking never uses it, it is passed through for callers and logs).
- ``leg_scores[<leg name>]`` — the leg's own score on its own scale. Used only
  to choose which of that leg's candidates take a reserved place.
- ``payload`` — the row the caller will receive (``fact_text``, ``category``,
  ``created_at`` …), passed through untouched.

The leg does not set ``leg_ranks``: :func:`fuse` records each candidate's
position in every leg that returned it.

Ranking after fusion, in ``search_facts``:

1. :func:`fuse` — reciprocal rank fusion over every leg (``k`` = :data:`RRF_K`).
2. The reranker scores the head of the fused list on 0..1 (``rerank_score``).
3. The blend uses one relevance scale (``relevance``): the rerank score when
   there is one, else :func:`relevance_from_rank` of the fused rank.
4. :func:`apply_reserved` — a leg named in ``reserved`` keeps at least that
   many places in the final top-k whenever it returned anything, chosen by its
   own ``leg_scores`` (the graph leg, :data:`BEST_ROW_LEGS`, keeps its best
   that many rows), capped at :func:`max_reserved_picks` (``(limit-1)//2``)
   in total so the reranked head is never displaced. A multi-hop fact is by
   definition not similar to the query, so a query-document reranker would
   otherwise always discard it. To
   request places for a new leg, add it to ``RESERVED_PLACES`` in
   ``robothor/memory/facts.py`` (or pass ``reserved=`` when calling
   :func:`apply_reserved` directly).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from robothor.memory.fusion import RRF_K

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

# Fused rank at which the rank-derived relevance falls to 0.5. Ten keeps the
# first page of a fused list clearly separated while the tail flattens out.
RANK_RELEVANCE_HALF = 10

_T = TypeVar("_T")

# The recall context cap's default: characters of memory text one recall
# returns. Recall fills it, reranked head first, then the remaining fused
# candidates in fused order; the reranker scores only its head
# (RERANK_MAX_CANDIDATES), so its cost does not grow. It was the router's
# DEFAULT_BUDGET_CHARS and keeps its value. The setting
# ROBOTHOR_MEMORY_RECALL_CONTEXT_CHARS (recall_context_chars) sets the cap.
RECALL_CONTEXT_CHARS = 4000


def recall_context_chars() -> int:
    """The recall context cap in characters, from the settings (read once per process)."""
    from robothor.settings import get_settings

    return int(get_settings().engine.recall_context_chars)


# The most rows a recall asks for when the caller names no limit; the
# character cap above normally binds first.
RECALL_FILL_ROWS = 50


def recall_limit(named: Any) -> int:
    """Rows to ask recall for: the caller's limit, else enough to fill the cap.

    A caller that names a limit gets at most that many rows; one that names
    none gets up to ``RECALL_FILL_ROWS``, cut by ``within_context_cap``. A named
    limit is bounded by ``RECALL_FILL_ROWS`` too: the cap holds no more than
    that, and the fill's work grows with the limit.
    """
    return min(int(named), RECALL_FILL_ROWS) if named else RECALL_FILL_ROWS


#: Ends a row cut to fit the cap, so a reader can see it was cut.
TRUNCATION_MARKER = " [truncated]"


#: Least room, in characters, worth filling with a cut row. About 75 tokens:
#: two or three sentences, room for a fact with its context or one short
#: exchange of a conversation. Less than that rarely holds a whole statement,
#: and the 12-character marker would be a large share of it.
RECALL_MIN_CUT_CHARS = 300


def pack_within(
    items: Sequence[_T],
    cap: int | None,
    size: Callable[[_T], int],
    truncate: Callable[[_T, int], _T] | None = None,
    min_cut: int = RECALL_MIN_CUT_CHARS,
) -> list[_T]:
    """The one packing rule: ``items`` in order, each kept while it still fits.

    An item that does not fit in what is left of ``cap`` is skipped and a
    later, smaller one may still go in, so the cap is never exceeded and the
    kept items stay in rank order. ``cap`` None keeps everything. Skipping,
    not stopping: one long conversation segment early in the head must not end
    a context that has room for the rows after it.

    Then, if at least ``min_cut`` (in ``size``'s units) of the cap is still
    empty, the best-ranked item that was skipped is cut to exactly that room
    by ``truncate`` (which marks the cut) and kept in its rank position. Every
    item the skipping kept stays, whole; at most one item is cut. The first,
    top-ranked item is never skipped: if it alone is larger than the cap it is
    kept cut to the cap, so a result is never empty because its best row was
    long. Without ``truncate`` nothing is cut. The recall tool, the routed path
    and the eval all pack with this function, so the measured context is the
    shipped one.
    """
    if cap is None:
        return list(items)
    kept: list[tuple[int, _T]] = []
    used = 0
    skipped: int | None = None
    for index, item in enumerate(items):
        n = size(item)
        if used + n > cap:
            if index == 0 and truncate is not None and cap > 0:
                cut = truncate(item, cap)
                if 0 < size(cut) <= cap:
                    kept.append((index, cut))
                    used += size(cut)
            elif skipped is None:
                skipped = index
            continue
        kept.append((index, item))
        used += n
    room = cap - used
    if truncate is not None and skipped is not None and room >= max(1, min_cut):
        cut = truncate(items[skipped], room)
        if 0 < size(cut) <= room:
            kept.append((skipped, cut))
            kept.sort(key=lambda pair: pair[0])
    return [item for _, item in kept]


def row_chars(row: dict[str, Any]) -> int:
    """Characters of a recall row's text, as the context counts them."""
    return len(row.get("fact_text") or row.get("insight_text") or row.get("text") or "")


def truncate_text(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, ending in ``TRUNCATION_MARKER``.

    A limit too small to hold the marker gets the cut text alone.
    """
    if len(text) <= limit:
        return text
    if limit <= len(TRUNCATION_MARKER):
        return text[: max(0, limit)]
    return text[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def truncate_row(row: dict[str, Any], limit: int) -> dict[str, Any]:
    """A copy of ``row`` whose text is cut to ``limit`` characters, marked."""
    out = dict(row)
    for key in ("fact_text", "insight_text", "text"):
        if out.get(key):
            out[key] = truncate_text(str(out[key]), limit)
            break
    out["truncated"] = True
    return out


def within_context_cap(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows in order packed into :func:`recall_context_chars` by :func:`pack_within`."""
    return pack_within(rows, recall_context_chars(), row_chars, truncate_row)


@dataclass
class Candidate:
    """One recall candidate. See the module docstring for what a leg must set."""

    id: Any
    kind: str
    text: str
    similarity: float | None = None
    leg_ranks: dict[str, int] = field(default_factory=dict)
    leg_scores: dict[str, float] = field(default_factory=dict)
    rrf_score: float = 0.0
    fused_rank: int | None = None
    rerank_score: float | None = None
    relevance: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, Any]:
        return (self.kind, self.id)

    def to_row(self) -> dict[str, Any]:
        """The caller-facing row: the payload plus every contract field."""
        row = dict(self.payload)
        row.setdefault("content", self.text)
        if self.similarity is not None:
            row["similarity"] = self.similarity
        row.update(
            id=self.id,
            kind=self.kind,
            leg_ranks=dict(self.leg_ranks),
            leg_scores=dict(self.leg_scores),
            rrf_score=round(self.rrf_score, 6),
            fused_rank=self.fused_rank,
            rerank_score=self.rerank_score,
        )
        if self.relevance is not None:
            row["relevance"] = self.relevance
        return row


def fuse(
    legs: Mapping[str, Sequence[Candidate]],
    k: int = RRF_K,
    weights: Mapping[str, float] | None = None,
) -> list[Candidate]:
    """Reciprocal rank fusion over any number of named legs.

    ``score = sum(weight[leg] / (k + rank))`` over the legs that returned the
    candidate, ``rank`` 0-based within the leg; a leg missing from ``weights``
    counts 1.0. Candidates with the same ``(kind, id)`` merge: the first one
    seen keeps its text and payload, ``leg_ranks``/``leg_scores`` are unioned
    and the first non-null ``similarity`` is kept. The result is ordered by
    fused score (ties keep first-seen order) and carries ``fused_rank``.
    """
    weights = weights or {}
    merged: dict[tuple[str, Any], Candidate] = {}
    for leg, ranked in legs.items():
        weight = float(weights.get(leg, 1.0))
        for rank, cand in enumerate(ranked):
            into = merged.get(cand.key)
            if into is None:
                into = Candidate(
                    id=cand.id,
                    kind=cand.kind,
                    text=cand.text,
                    similarity=cand.similarity,
                    leg_scores=dict(cand.leg_scores),
                    payload=dict(cand.payload),
                )
                merged[cand.key] = into
            else:
                if into.similarity is None:
                    into.similarity = cand.similarity
                for name, score in cand.leg_scores.items():
                    into.leg_scores.setdefault(name, score)
            if leg not in into.leg_ranks:
                into.leg_ranks[leg] = rank
                into.rrf_score += weight / (k + rank)
    ordered = sorted(merged.values(), key=lambda c: c.rrf_score, reverse=True)
    for pos, cand in enumerate(ordered):
        cand.fused_rank = pos
    return ordered


def relevance_from_rank(rank: int, half: int = RANK_RELEVANCE_HALF) -> float:
    """Relevance (0..1] for a candidate known only by its fused rank.

    ``half / (half + rank)``: 1.0 at rank 0, 0.5 at rank ``half``, never 0.
    The fallback when no rerank score exists, on the same scale as one.
    """
    return half / (half + max(0, int(rank)))


def graph_only(row: Mapping[str, Any]) -> bool:
    """Whether the graph leg alone found this row.

    Such a row is related to what was asked, not a match for it: it is
    labelled with its path (:func:`row_text`) and follows the ranked head
    wherever rows are re-ordered.
    """
    return set(row.get("leg_ranks") or {}) == {"graph"}


def row_text(row: Mapping[str, Any]) -> str:
    """The text a model reads for a recall row: the one formatter every
    surface that renders rows uses (the engine's ``search_memory`` tool, MCP,
    deep reason, the memory evaluation's context).

    A row only the graph leg found is about an entity *related* to the one
    asked about, so it is shown with the walk's path to it: ``Related via
    Alice Smith → Example Co: Example Co is headquartered in Brelford.`` (the
    answer model otherwise took an employer's address for the person's). The
    path is the walk's first-reach path, every node of it visible to the
    reader. Any row another leg also found keeps its plain text.
    """
    text = str(row.get("fact_text") or row.get("insight_text") or row.get("text") or "")
    path = [str(n) for n in (row.get("graph_path") or []) if n]
    if path and graph_only(row):
        return f"Related via {' → '.join(path)}: {text}"
    return text


def _row_key(row: Mapping[str, Any]) -> tuple[Any, Any]:
    return (row.get("kind"), row.get("id"))


def _leg_order(leg: str) -> Callable[[Mapping[str, Any]], tuple[float, int]]:
    def key(row: Mapping[str, Any]) -> tuple[float, int]:
        score = (row.get("leg_scores") or {}).get(leg)
        rank = (row.get("leg_ranks") or {}).get(leg, 1 << 30)
        return (-(float(score) if score is not None else float("-inf")), int(rank))

    return key


#: Legs whose reserved places hold their best rows by their own score: a
#: weaker row of the leg that the head holds anyway does not use a place, a
#: best row the head holds counts, and the rest are added after the head
#: without evicting any of it. Only the graph leg; every other reserved leg
#: keeps the earlier rule (its picks take the head's last places).
BEST_ROW_LEGS: frozenset[str] = frozenset({"graph"})


def _best_rows_held(
    leg: str,
    want: int,
    out: Sequence[dict[str, Any]],
    pool: Sequence[dict[str, Any]],
    exclude: Callable[[dict[str, Any]], bool] | None,
) -> tuple[list[dict[str, Any]], int]:
    """The rows of ``out`` among the leg's best ``want`` and how many best rows
    there are. ``exclude`` keeps out only rows that would come in from outside
    the head."""
    in_head = {_row_key(r) for r in out}
    best = {
        _row_key(r)
        for r in sorted(
            (
                r
                for r in pool
                if leg in (r.get("leg_ranks") or {})
                and (_row_key(r) in in_head or not (exclude is not None and exclude(r)))
            ),
            key=_leg_order(leg),
        )[:want]
    }
    return [r for r in out if _row_key(r) in best], len(best)


def max_reserved_picks(limit: int) -> int:
    """Most places reserved picks may take in a result of ``limit``: (limit-1)//2.

    Strictly fewer than half, so the reranked head — rank 0 included — is never
    displaced by rows chosen on another leg's own score.
    """
    return max(0, (int(limit) - 1) // 2)


def apply_reserved(
    ranked: Sequence[dict[str, Any]],
    pool: Sequence[dict[str, Any]],
    limit: int,
    reserved: Mapping[str, int],
    exclude: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Cut ``ranked`` to ``limit`` while honouring each leg's reserved places.

    ``ranked`` is the final order (reranked and blended); ``pool`` is every
    fused candidate as a row (``Candidate.to_row``), including those the
    reranker never saw. For each ``leg -> n`` in ``reserved``: if fewer than
    ``n`` rows in the top ``limit`` came from that leg, the missing ones are
    taken from ``pool`` in the order of that leg's own ``leg_scores`` (then its
    rank), and they replace the lowest-ranked rows that do not themselves hold
    a reserved place. A leg in :data:`BEST_ROW_LEGS` (the graph) keeps its
    best ``n`` rows instead: another of its rows that the head holds anyway
    uses up no place, and the missing ones are added *after* the head,
    evicting nothing. The result is then up to ``limit`` + that leg's ``n``
    rows long, and its first ``limit`` rows are ``ranked[:limit]`` exactly. Reserved picks go at the end of the list — they are
    guaranteed a place, not a position. A leg with no candidates reserves
    nothing, and a pool row for which ``exclude(row)`` is true is never picked
    (``search_facts`` uses it to keep out near-duplicates of ranked rows).

    Picks of the other legs take at most :func:`max_reserved_picks` places in
    total, so the ranked head always keeps the majority: no such picks at
    ``limit`` 1 or 2.
    """
    out = list(ranked[:limit])
    protected: set[tuple[Any, Any]] = set()
    picks: list[dict[str, Any]] = []
    # The graph's rows (BEST_ROW_LEGS) go after the head and evict nothing.
    appended: list[dict[str, Any]] = []
    budget = max_reserved_picks(limit)
    for leg, want in reserved.items():
        if want <= 0:
            continue
        if leg in BEST_ROW_LEGS:
            have, need_total = _best_rows_held(leg, want, out, pool, exclude)
        else:
            # Every other leg: any of its rows in the head holds a place.
            have = [r for r in out if leg in (r.get("leg_ranks") or {})][:want]
            need_total = want
        protected.update(_row_key(r) for r in have)
        if leg in BEST_ROW_LEGS:
            need = need_total - len(have)
        else:
            need = min(need_total - len(have), budget - len(picks))
        if need <= 0:
            continue
        taken = {_row_key(r) for r in (*out, *picks, *appended)}
        extra = sorted(
            (
                r
                for r in pool
                if leg in (r.get("leg_ranks") or {})
                and _row_key(r) not in taken
                and not (exclude is not None and exclude(r))
            ),
            key=_leg_order(leg),
        )[:need]
        for r in extra:
            r["reserved_leg"] = leg
            protected.add(_row_key(r))
        (appended if leg in BEST_ROW_LEGS else picks).extend(extra)

    for pick in picks:
        while len(out) + 1 > limit:
            victim = next(
                # Stops at index 1: the top ranked row is never a victim, by
                # construction rather than by the budget arithmetic.
                (i for i in range(len(out) - 1, 0, -1) if _row_key(out[i]) not in protected),
                None,
            )
            if victim is None:
                break
            out.pop(victim)
        if len(out) < limit:
            out.append(pick)
    return [*out, *appended]
