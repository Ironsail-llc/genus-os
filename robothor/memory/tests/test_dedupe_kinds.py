"""Across kinds, the near-duplicate rule removes only the row that is contained.

A fact is a lossy summary of the conversation it came from, so its words are
often a subset of a segment's. The subset test dropped whichever came second:
sometimes the segment that holds the date, the speaker and the detail. Now,
between rows of different kinds, a row is dropped only when its words are all
inside a row already kept (the kept row says everything it says), never when
it contains a kept row, and never when it is newer than the row that contains
it (a current fact must not hide behind an older conversation that happens to
use its words). Within one kind the rule is unchanged. It holds at every place
the rule runs: the blend, the head, the reserved picks and the budget fill.
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


def test_a_segment_is_never_dropped_for_a_fact_it_contains() -> None:
    kept = facts._dedupe([_fact(1, FACT), _seg(2, SEGMENT)])
    assert _ids(kept) == [("fact", 1), ("segment", 2)]


def test_an_older_fact_inside_a_kept_segment_is_dropped() -> None:
    """The update case: the fact still states what the newer conversation
    says, and the conversation says more."""
    kept = facts._dedupe([_seg(2, SEGMENT), _fact(1, FACT, OLDER)])
    assert _ids(kept) == [("segment", 2)]


def test_a_newer_fact_inside_an_older_segment_is_kept() -> None:
    kept = facts._dedupe([_seg(2, SEGMENT), _fact(1, FACT, NEWER)])
    assert _ids(kept) == [("segment", 2), ("fact", 1)]


def test_a_fact_of_unknown_time_inside_a_segment_is_dropped() -> None:
    kept = facts._dedupe([_seg(2, SEGMENT), _fact(1, FACT, None)])
    assert _ids(kept) == [("segment", 2)]


def test_a_lexical_near_duplicate_of_another_kind_that_is_not_contained_is_kept() -> None:
    """Across kinds only containment counts, not word overlap."""
    fact = _fact(1, "Alice adopted a dog named Rex in May at the shelter.")
    kept = facts._dedupe([_seg(2, SEGMENT), fact])
    assert _ids(kept) == [("segment", 2), ("fact", 1)]


def test_a_fact_inside_another_fact_is_still_a_duplicate() -> None:
    kept = facts._dedupe([_fact(1, FACT + " She walks him daily."), _fact(2, FACT, NEWER)])
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
    assert _ids(out) == [("fact", 1), ("segment", 2)]


@pytest.mark.parametrize(("created_at", "kept"), [(OLDER, False), (NEWER, True)])
def test_the_fill_drops_only_an_older_fact_a_head_segment_contains(
    env: pytest.MonkeyPatch, created_at: datetime, kept: bool
) -> None:
    head = [_seg(2, SEGMENT)]
    pool = [_seg(2, SEGMENT), _fact(1, FACT, created_at), _fact(4, "Bob likes tea.")]
    out = facts._fill_from_pool(head, pool, limit=5)
    assert (("fact", 1) in _ids(out)) is kept
    assert ("fact", 4) in _ids(out)


@pytest.mark.asyncio
async def test_search_keeps_a_newer_fact_beside_the_segment_holding_its_words(
    env: pytest.MonkeyPatch,
) -> None:
    """Blend and head: both rows reach the caller, whichever ranks first."""
    fact = _row(1, FACT, similarity=0.9, created_at=datetime.now(UTC) + timedelta(days=1))
    cur = _Cursor(vector=[fact, *_dense(5)], keyword=[dict(fact, keyword_score=0.5)])
    seg = _segment_row(9001, SEGMENT)
    seg_cur = _SegmentCursor(
        dense=[dict(seg, similarity=0.8)], keyword=[dict(seg, keyword_rank=0.4)]
    )
    with patch.object(reranker, "rerank_pair", _scorer({"Rex": 0.9})):
        rows = await _search_with_segments(cur, seg_cur, limit=5)

    kinds = {r["kind"] for r in rows if "Rex" in r["fact_text"]}
    assert kinds == {"fact", "segment"}


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
@pytest.mark.parametrize(("days", "kept"), [(-400, False), (1, True)])
async def test_a_reserved_fact_inside_a_head_segment_is_kept_only_if_newer(
    env: pytest.MonkeyPatch, days: int, kept: bool
) -> None:
    rows = await _reserved_search(datetime.now(UTC) + timedelta(days=days))
    assert rows[0]["kind"] == "segment"
    assert (700 in [r["id"] for r in rows if r["kind"] == "fact"]) is kept
