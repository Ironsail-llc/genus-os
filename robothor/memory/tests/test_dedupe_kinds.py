"""The near-duplicate rule is kind-blind: whichever row ranks second is dropped.

A fact is a summary of the conversation it came from, so its words are often
a subset of a segment's, and the subset test drops whichever of the two ranks
second. Two other rules were measured on the memory evaluation's world and
rejected: comparing rows only within their kind, and dropping only the
contained row across kinds. Both kept a fact and the conversation that says
it, and the duplicates pushed the conversation evidence for point-in-time and
multi-hop questions out of the 1,000-token context (held-out point_in_time 31
to 26 and 25 of 40). The rule is the same at every place it runs: the blend,
the head, the reserved picks and the budget fill.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.memory import facts
from robothor.memory.tests.test_search_facts_ranking import _Cursor, _dense, _row, _scorer
from robothor.memory.tests.test_segment_fusion import (
    _search_with_segments,
    _segment_row,
    _SegmentCursor,
)
from robothor.rag import reranker

FACT = "Alice adopted a dog named Rex in May."
SEGMENT = (
    "User: guess what, Alice adopted a dog named Rex in May! I'm thrilled.\n"
    "Assistant: That's wonderful news, congratulations to Alice and Rex."
)
SEGMENT_ENDED = datetime(2025, 6, 1, tzinfo=UTC)
OLDER = SEGMENT_ENDED - timedelta(days=30)
NEWER = SEGMENT_ENDED + timedelta(days=30)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for flag in (
        "MEMORY_RERANK_WIDE",
        "MEMORY_EPISODE_MERGE",
        "MEMORY_TEMPORAL_COHERENCE",
        "MEMORY_RERANK_MAX_CANDIDATES",
        "MEMORY_DEDUP",
    ):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("MEMORY_RANK_BLEND", "1")
    monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
    return monkeypatch


def _fact(fid: int, text: str, created_at: datetime | None = OLDER) -> dict[str, Any]:
    return {"kind": "fact", "id": fid, "fact_text": text, "created_at": created_at}


def _seg(sid: int, text: str) -> dict[str, Any]:
    return {"kind": "segment", "id": sid, "fact_text": text, "ended_at": SEGMENT_ENDED}


def _ids(rows: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return [(r["kind"], r["id"]) for r in rows]


@pytest.mark.parametrize("created_at", [OLDER, NEWER, None])
def test_a_segment_after_a_fact_it_contains_is_dropped(created_at: datetime | None) -> None:
    kept = facts._dedupe([_fact(1, FACT, created_at), _seg(2, SEGMENT)])
    assert _ids(kept) == [("fact", 1)]


@pytest.mark.parametrize("created_at", [OLDER, NEWER, None])
def test_a_fact_after_a_segment_that_contains_it_is_dropped(created_at: datetime | None) -> None:
    kept = facts._dedupe([_seg(2, SEGMENT), _fact(1, FACT, created_at)])
    assert _ids(kept) == [("segment", 2)]


def test_a_fact_inside_another_fact_is_still_a_duplicate() -> None:
    kept = facts._dedupe([_fact(1, FACT + " She walks him daily."), _fact(2, FACT, NEWER)])
    assert [r["id"] for r in kept] == [1]


def test_a_short_segment_inside_a_long_one_is_still_a_duplicate() -> None:
    kept = facts._dedupe(
        [_seg(1, "User: yes I want the ok plan\nAssistant: ok"), _seg(2, "yes ok")]
    )
    assert [r["id"] for r in kept] == [1]


def test_the_fill_drops_a_segment_that_contains_a_head_fact(env: pytest.MonkeyPatch) -> None:
    head = [_fact(1, FACT)]
    pool = [_fact(1, FACT), _seg(2, SEGMENT), _fact(4, "Bob likes tea.")]
    out = facts._fill_from_pool(head, pool, limit=5)
    assert _ids(out) == [("fact", 1), ("fact", 4)]


@pytest.mark.parametrize("created_at", [OLDER, NEWER])
def test_the_fill_drops_a_fact_a_head_segment_contains(
    env: pytest.MonkeyPatch, created_at: datetime
) -> None:
    head = [_seg(2, SEGMENT)]
    pool = [_seg(2, SEGMENT), _fact(1, FACT, created_at), _fact(4, "Bob likes tea.")]
    out = facts._fill_from_pool(head, pool, limit=5)
    assert _ids(out) == [("segment", 2), ("fact", 4)]


async def _reserved_search(hop_created: datetime) -> list[dict[str, Any]]:
    hop = _row(700, FACT, entities=["Bob"], importance_score=0.9, created_at=hop_created)
    cur = _Cursor(vector=_dense(20), keyword=[], expansion=[hop])
    seg = _segment_row(9001, SEGMENT)
    seg_cur = _SegmentCursor(
        dense=[dict(seg, similarity=0.8)], keyword=[dict(seg, keyword_rank=0.4)]
    )
    get_entity = AsyncMock(return_value={"relations": [{"target_name": "Bob"}]})
    # The segment is the best answer; the expansion fact scores lowest.
    scorer = _scorer({"thrilled": 0.95, "Rex": 0.01}, default=0.6)
    with (
        patch.object(reranker, "rerank_pair", scorer),
        patch("robothor.memory.activation.graph_candidates_for_query", return_value=[]),
        patch("robothor.memory.entities.get_entity", get_entity),
    ):
        return await _search_with_segments(cur, seg_cur, limit=5, expand_entities=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [-400, 1])
async def test_a_reserved_pick_never_repeats_a_head_segment(
    env: pytest.MonkeyPatch, days: int
) -> None:
    rows = await _reserved_search(datetime.now(UTC) + timedelta(days=days))
    assert rows[0]["kind"] == "segment"
    assert 700 not in [r["id"] for r in rows if r["kind"] == "fact"]
