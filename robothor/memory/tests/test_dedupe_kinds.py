"""The near-duplicate rule never removes a row of a different kind.

A fact is a lossy summary of the conversation it came from, so its words are
often a subset of a segment's. The subset test then dropped whichever came
second: the segment that holds the date, the speaker and the detail, or the
fact. Near-duplicates are now looked for only among rows of the same kind, at
every place the rule runs: the blend, the head, the reserved picks and the
budget fill. Same-kind behaviour is unchanged.
"""

from __future__ import annotations

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


def _fact(fid: int, text: str) -> dict[str, Any]:
    return {"kind": "fact", "id": fid, "fact_text": text}


def _seg(sid: int, text: str) -> dict[str, Any]:
    return {"kind": "segment", "id": sid, "fact_text": text}


@pytest.mark.parametrize("order", ["fact first", "segment first"])
def test_a_fact_inside_a_segment_keeps_both(order: str) -> None:
    rows = [_fact(1, FACT), _seg(2, SEGMENT)]
    if order == "segment first":
        rows.reverse()
    kept = facts._dedupe(rows)
    assert sorted(r["kind"] for r in kept) == ["fact", "segment"]


def test_a_fact_inside_another_fact_is_still_a_duplicate() -> None:
    kept = facts._dedupe([_fact(1, FACT + " She walks him daily."), _fact(2, FACT)])
    assert [r["id"] for r in kept] == [1]


def test_a_short_segment_inside_a_long_one_is_still_a_duplicate() -> None:
    kept = facts._dedupe(
        [_seg(1, "User: yes I want the ok plan\nAssistant: ok"), _seg(2, "yes ok")]
    )
    assert [r["id"] for r in kept] == [1]


def test_the_fill_keeps_a_segment_whose_words_cover_a_head_fact(
    env: pytest.MonkeyPatch,
) -> None:
    head = [_fact(1, FACT)]
    pool = [_fact(1, FACT), _seg(2, SEGMENT), _fact(3, FACT + " Indeed.")]
    out = facts._fill_from_pool(head, pool, limit=5)
    assert [(r["kind"], r["id"]) for r in out] == [("fact", 1), ("segment", 2)]


@pytest.mark.asyncio
async def test_search_returns_the_fact_and_the_segment_it_came_from(
    env: pytest.MonkeyPatch,
) -> None:
    """Blend and head: both rows reach the caller, whichever ranks first."""
    fact = _row(1, FACT, similarity=0.9)
    cur = _Cursor(vector=[fact, *_dense(5)], keyword=[dict(fact, keyword_score=0.5)])
    seg = _segment_row(9001, SEGMENT)
    seg_cur = _SegmentCursor(
        dense=[dict(seg, similarity=0.8)], keyword=[dict(seg, keyword_rank=0.4)]
    )
    with patch.object(reranker, "rerank_pair", _scorer({"Rex": 0.9})):
        rows = await _search_with_segments(cur, seg_cur, limit=5)

    kinds = {r["kind"] for r in rows if "Rex" in r["fact_text"]}
    assert kinds == {"fact", "segment"}


@pytest.mark.asyncio
async def test_a_reserved_fact_is_not_dropped_for_a_segment_in_the_head(
    env: pytest.MonkeyPatch,
) -> None:
    """Reserved picks: the expansion leg's fact keeps its place beside the
    segment whose words cover it."""
    hop = _row(700, FACT, entities=["Bob"], importance_score=0.9)
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
        rows = await _search_with_segments(cur, seg_cur, limit=5, expand_entities=True)

    assert rows[0]["kind"] == "segment"
    assert 700 in [r["id"] for r in rows if r["kind"] == "fact"]
