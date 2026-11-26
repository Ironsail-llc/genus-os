"""The recall-leg contract: named legs in, one fused candidate list out.

A **leg** is anything that turns a query into a ranked list of
:class:`Candidate` objects, best first: today the vector and keyword legs over
``memory_facts``, entity-graph expansion, insights, episodes and chat turns;
tomorrow raw conversation segments and graph activation. A leg must return:

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
4. :func:`apply_reserved` — a leg named in ``reserved`` keeps its best that
   many rows, by its own ``leg_scores``, in the final top-k whenever it
   returned anything, capped at :func:`max_reserved_picks` (``(limit-1)//2``)
   in total so the reranked head is never displaced. A multi-hop fact is by
   definition not similar to the query, so a query-document reranker would
   otherwise always discard it. To
   request places for a new leg, add it to ``RESERVED_PLACES`` in
   ``robothor/memory/facts.py`` (or pass ``reserved=`` when calling
   :func:`apply_reserved` directly).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from robothor.memory.fusion import RRF_K

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

# Fused rank at which the rank-derived relevance falls to 0.5. Ten keeps the
# first page of a fused list clearly separated while the tail flattens out.
RANK_RELEVANCE_HALF = 10


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


def _row_key(row: Mapping[str, Any]) -> tuple[Any, Any]:
    return (row.get("kind"), row.get("id"))


def _leg_order(leg: str) -> Callable[[Mapping[str, Any]], tuple[float, int]]:
    def key(row: Mapping[str, Any]) -> tuple[float, int]:
        score = (row.get("leg_scores") or {}).get(leg)
        rank = (row.get("leg_ranks") or {}).get(leg, 1 << 30)
        return (-(float(score) if score is not None else float("-inf")), int(rank))

    return key


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
    reranker never saw. For each ``leg -> n`` in ``reserved``: the leg's best
    ``n`` rows in ``pool``, in the order of its own ``leg_scores`` (then its
    rank), are kept in the top ``limit``; those missing from it replace the
    lowest-ranked rows that do not themselves hold a reserved place. Another
    row of the leg that the head holds anyway uses up no place. Reserved picks go at the end of the list — they are
    guaranteed a place, not a position. A leg with no candidates reserves
    nothing, and a pool row for which ``exclude(row)`` is true is never picked
    (``search_facts`` uses it to keep out near-duplicates of ranked rows).

    Picks take at most :func:`max_reserved_picks` places in total, so the
    ranked head always keeps the majority: no picks at ``limit`` 1 or 2.
    """
    out = list(ranked[:limit])
    protected: set[tuple[Any, Any]] = set()
    picks: list[dict[str, Any]] = []
    budget = max_reserved_picks(limit)
    for leg, want in reserved.items():
        if want <= 0:
            continue
        # The places are for the leg's best rows by its own score; a weaker
        # row of the leg that the head holds anyway does not use one up, and
        # a best row the head holds counts (``exclude`` keeps out only rows
        # that would come in from outside the head).
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
        have = [r for r in out if _row_key(r) in best]
        protected.update(_row_key(r) for r in have)
        need = min(len(best) - len(have), budget - len(picks))
        if need <= 0:
            continue
        taken = {_row_key(r) for r in out} | {_row_key(r) for r in picks}
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
        picks.extend(extra)

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
    return out
