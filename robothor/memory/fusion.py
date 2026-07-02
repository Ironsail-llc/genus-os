"""Reciprocal Rank Fusion — shared by the fact retriever and the memory router.

Extracted from the inline RRF in ``facts.search_facts`` so the router can fuse
ranked result lists from multiple stores (facts, vault, intents, procedures,
episodes) with the same scoring the fact retriever already uses (k=60).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

RRF_K = 60


def rrf_fuse(
    ranked_lists: Sequence[Sequence[dict[str, Any]]],
    *,
    k: int = RRF_K,
    key: Callable[[dict[str, Any]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Fuse several already-ranked result lists into one, by RRF.

    Each input list must be in rank order (best first). An item's fused score
    is ``sum(1 / (k + rank))`` across the lists it appears in. The first dict
    seen for a given key is kept and annotated with ``rrf_score``.

    ``key`` identifies "the same item" across lists; it defaults to
    ``(source, id)`` so a fact and an intent that happen to share an integer id
    do not collide. Items whose key is all-None are treated as distinct.
    """
    if key is None:

        def key(item: dict[str, Any]) -> Any:
            return (item.get("source"), item.get("id"))

    scores: dict[Any, float] = {}
    first: dict[Any, dict[str, Any]] = {}
    distinct_counter = 0

    for ranked in ranked_lists:
        for rank, item in enumerate(ranked):
            kval = key(item)
            if kval is None or all(part is None for part in _as_tuple(kval)):
                # No stable identity — keep as its own entry.
                kval = ("_anon", distinct_counter)
                distinct_counter += 1
            scores[kval] = scores.get(kval, 0.0) + 1.0 / (k + rank)
            first.setdefault(kval, item)

    fused = sorted(first.items(), key=lambda kv: scores[kv[0]], reverse=True)
    out: list[dict[str, Any]] = []
    for kval, item in fused:
        item["rrf_score"] = round(scores[kval], 6)
        out.append(item)
    return out


def _as_tuple(value: Any) -> tuple[Any, ...]:
    return value if isinstance(value, tuple) else (value,)
