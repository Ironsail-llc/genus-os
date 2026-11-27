"""Conversation segments compete with facts on equal terms in fusion.

A fact enters fusion as two legs (``vector`` and ``keyword``). A segment used
to enter as one leg whose own dense and keyword fusion had already happened,
so any fact found by both fact legs in their top 60 outranked the best
segment: with many facts about the person a question names, the segment that
holds the answer sat at fused rank 61, past the reranker's head, and never
came back. These tests seed exactly that world.

Same fake fact database and reranker as ``test_search_facts_ranking.py``; the
segment store answers from its own fake cursor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.memory import segments
from robothor.memory.fusion import RRF_K
from robothor.memory.tests.test_search_facts_ranking import (
    _conn,
    _Cursor,
    _row,
    _scorer,
    _search,
)
from robothor.rag import reranker

ANSWER = "User: which locker does Alice use?\nAssistant: Alice uses locker 4417 by the door."


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


def _segment_row(sid: int, text: str, **extra: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    row: dict[str, Any] = {
        "id": sid,
        "text": text,
        "topic": "locker",
        "source_kind": "conversation",
        "source_ref": "agent:main:session:one",
        "start_ref": 1,
        "end_ref": 2,
        "part": 0,
        "started_at": now,
        "ended_at": now,
        "scope_kind": "shared",
    }
    row.update(extra)
    return row


class _SegmentCursor:
    """Answers the segment store's dense and keyword queries; records them."""

    def __init__(self, dense: list[dict[str, Any]], keyword: list[dict[str, Any]]) -> None:
        self.dense, self.keyword = dense, keyword
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        sql = self.executed[-1][0] if self.executed else ""
        if "memory_segments" not in sql:
            return []
        if "<=>" in sql:
            return [dict(r) for r in self.dense]
        if "tsv @@" in sql:
            return [dict(r) for r in self.keyword]
        return []


def _two_leg_facts(n: int = 100) -> _Cursor:
    """``n`` facts about Alice, every one found by both fact legs, same order."""
    facts = [
        _row(i, f"Alice note {i}: item {i * 7} was filed", similarity=0.9 - i / 1000)
        for i in range(1, n + 1)
    ]
    keyword = [dict(r, keyword_score=0.5 - i / 1000) for i, r in enumerate(facts)]
    return _Cursor(vector=facts, keyword=keyword)


async def _search_with_segments(
    facts_cur: _Cursor, seg_cur: _SegmentCursor, **kw: Any
) -> list[dict[str, Any]]:
    with (
        patch.object(segments, "get_connection", return_value=_conn(seg_cur)),
        patch.object(segments, "apply_hnsw_session"),
    ):
        return await _search(facts_cur, query="which locker does Alice use", **kw)


def _scored(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("kind") == "segment"]


@pytest.mark.asyncio
async def test_a_segment_found_by_both_its_legs_is_not_buried_by_two_leg_facts(
    env: pytest.MonkeyPatch,
) -> None:
    """100 facts found by both fact legs, one answer segment found by both
    segment legs: the segment reaches the reranker and comes back."""
    answer = _segment_row(9001, ANSWER)
    seg_cur = _SegmentCursor(
        dense=[dict(answer, similarity=0.7)], keyword=[dict(answer, keyword_rank=0.4)]
    )
    with patch.object(reranker, "rerank_pair", _scorer({"4417": 0.95})):
        rows = await _search_with_segments(_two_leg_facts(), seg_cur, limit=10)

    hits = _scored(rows)
    assert hits, [r.get("kind") for r in rows]
    assert "4417" in hits[0]["fact_text"]
    assert hits[0]["rerank_score"] is not None, "the reranker scored it"
    assert rows[0]["kind"] == "segment", "scored highest, it ranks first"


@pytest.mark.asyncio
async def test_a_segment_carries_its_rank_in_each_of_its_legs(env: pytest.MonkeyPatch) -> None:
    """A segment found by both of its legs gets credit from each, like a fact."""
    answer = _segment_row(9001, ANSWER)
    seg_cur = _SegmentCursor(
        dense=[dict(answer, similarity=0.7)], keyword=[dict(answer, keyword_rank=0.4)]
    )
    with patch.object(reranker, "rerank_pair", _scorer({"4417": 0.95})):
        rows = await _search_with_segments(_two_leg_facts(), seg_cur, limit=10)

    hit = _scored(rows)[0]
    assert len(hit["leg_ranks"]) == 2, hit["leg_ranks"]
    assert set(hit["leg_ranks"].values()) == {0}
    # A fact first in both fact legs scores 2 / RRF_K; so does this segment.
    assert hit["rrf_score"] == pytest.approx(2 / RRF_K, abs=1e-6)


@pytest.mark.asyncio
async def test_the_reranker_head_holds_a_segment_that_ranks_first_on_one_leg(
    env: pytest.MonkeyPatch,
) -> None:
    """Found only by the dense segment leg, the answer still fuses below every
    two-leg fact; the reranker's head must still hold it, or it is never
    scored."""
    answer = _segment_row(9001, ANSWER)
    seg_cur = _SegmentCursor(dense=[dict(answer, similarity=0.7)], keyword=[])
    scored_docs: list[str] = []
    inner = _scorer({"4417": 0.95})

    async def recording(client: Any, query: str, doc: str) -> float:
        scored_docs.append(doc)
        return await inner(client, query, doc)

    with patch.object(reranker, "rerank_pair", recording):
        rows = await _search_with_segments(_two_leg_facts(), seg_cur, limit=10)

    assert len(scored_docs) == reranker.RERANK_MAX_CANDIDATES
    assert any("4417" in d for d in scored_docs), "the segment was in the scored head"
    assert rows[0]["kind"] == "segment"


@pytest.mark.asyncio
async def test_the_segment_legs_read_with_the_same_visibility_filter(
    env: pytest.MonkeyPatch,
) -> None:
    """Splitting the segment leg changes ranking only: every segment query
    still carries the reader's visibility clause and parameters."""
    answer = _segment_row(9001, ANSWER)
    seg_cur = _SegmentCursor(
        dense=[dict(answer, similarity=0.7)], keyword=[dict(answer, keyword_rank=0.4)]
    )
    with patch.object(reranker, "rerank_pair", _scorer({"4417": 0.95})):
        await _search_with_segments(
            _two_leg_facts(5),
            seg_cur,
            limit=10,
            reader_person_id="person-alice",
            room_ids=["11111111-1111-1111-1111-111111111111"],
        )

    where, params = segments._filters(
        reader_person_id="person-alice",
        reader_user_id=None,
        since=None,
        until=None,
        room_ids=["11111111-1111-1111-1111-111111111111"],
    )
    queries = [(s, p) for s, p in seg_cur.executed if "memory_segments" in s]
    assert len(queries) == 2, "one dense and one keyword query, no more"
    for sql, sent in queries:
        assert where in sql
        assert "test-tenant" in sent
        flat = list(sent)
        assert all(p in flat for p in params)
