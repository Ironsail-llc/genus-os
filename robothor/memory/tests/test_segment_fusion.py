"""Conversation segments compete with facts on equal terms in fusion.

A fact enters fusion as two legs (``vector`` and ``keyword``); a segment as
the one ``segment`` leg the segment store fuses from its own dense and keyword
legs, as it always has. So any fact found by both fact legs in their top 60
outranks the best segment: with many facts about the subject a question
names, the segment holding the answer used to sit past the reranker's head
and never come back. So when that head holds no segment at all, the best
segment is judged beside it, and kept only when judged relevant.

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


def _recording(scores: dict[str, float]) -> tuple[list[str], Any]:
    seen: list[str] = []
    inner = _scorer(scores)

    async def pair(client: Any, query: str, doc: str) -> float:
        seen.append(doc)
        return await inner(client, query, doc)

    return seen, pair


def _both_legs(*rows: dict[str, Any]) -> _SegmentCursor:
    return _SegmentCursor(
        dense=[dict(r, similarity=0.7 - i / 100) for i, r in enumerate(rows)],
        keyword=[dict(r, keyword_rank=0.4 - i / 100) for i, r in enumerate(rows)],
    )


def _keys(rows: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    return [(r["kind"], r["id"]) for r in rows]


@pytest.mark.asyncio
async def test_an_answer_segment_buried_under_two_leg_facts_is_returned_when_relevant(
    env: pytest.MonkeyPatch,
) -> None:
    """100 facts found by both fact legs fill the reranker's head; the best
    segment is judged beside them and, judged relevant, takes the head's last
    place: never first, whatever its score (read first, it made the answer
    model abstain on the memory evaluation's world), and never after the
    head, where its fused rank (past every row the head holds) would put it
    beyond what the context can hold."""
    seg_cur = _both_legs(_segment_row(9001, ANSWER))
    seen, pair = _recording({"4417": 0.95})
    with patch.object(reranker, "rerank_pair", pair):
        rows = await _search_with_segments(_two_leg_facts(), seg_cur, limit=10)

    assert any("4417" in d for d in seen), "the reranker judged the segment"
    hits = _scored(rows)
    assert hits and "4417" in hits[0]["fact_text"], [r.get("kind") for r in rows]
    assert hits[0]["rerank_score"] == pytest.approx(0.95)
    head = [r for r in rows if r.get("rerank_score") is not None]
    assert head[-1]["kind"] == "segment", [r["kind"] for r in head]
    assert rows.index(head[-1]) == len(head) - 1
    assert rows[0]["kind"] == "fact"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [10, 30])
async def test_an_irrelevant_segment_displaces_nothing(env: pytest.MonkeyPatch, limit: int) -> None:
    """Judged below the relevance line, the segment leaves the result exactly
    as it is without it: the same rows, in the same order, scored alike."""
    seg_cur = _both_legs(_segment_row(9001, "User: what did we have for lunch?\nAssistant: Soup."))
    scores = {"Alice note 3:": 0.9, "Alice note 7:": 0.7, "lunch": 0.1}
    with patch.object(reranker, "rerank_pair", _scorer(scores)):
        with_segment = await _search_with_segments(_two_leg_facts(), seg_cur, limit=limit)
        without = await _search_with_segments(
            _two_leg_facts(), _SegmentCursor(dense=[], keyword=[]), limit=limit
        )

    assert _keys(with_segment) == _keys(without)
    assert [r.get("rerank_score") for r in with_segment] == [r.get("rerank_score") for r in without]


@pytest.mark.asyncio
@pytest.mark.parametrize("found_by", ["both", "dense", "keyword"])
async def test_a_segment_fuses_as_the_single_segment_leg_it_was(
    env: pytest.MonkeyPatch, found_by: str
) -> None:
    """The best segment fuses at 1/RRF_K whichever of the store's legs found
    it: the store fuses its own legs and enters as one ``segment`` leg, so the
    default ranking of every recall is what it was. (Two half-weight legs
    halved a segment one leg found, and conversation evidence fell out of the
    context tail of fact questions.)"""
    seg = _segment_row(9001, ANSWER)
    seg_cur = _SegmentCursor(
        dense=[dict(seg, similarity=0.7)] if found_by != "keyword" else [],
        keyword=[dict(seg, keyword_rank=0.4)] if found_by != "dense" else [],
    )
    with patch.object(reranker, "rerank_pair", _scorer({"4417": 0.95})):
        rows = await _search_with_segments(_two_leg_facts(5), seg_cur, limit=10)

    hit = _scored(rows)[0]
    assert hit["leg_ranks"] == {"segment": 0}
    assert hit["rrf_score"] == pytest.approx(1 / RRF_K, abs=1e-6)


@pytest.mark.asyncio
async def test_no_extra_pair_when_the_head_already_holds_a_segment(
    env: pytest.MonkeyPatch,
) -> None:
    """Facts one leg found fuse below a two-leg segment, which is then in the
    head on its own: the reranker scores exactly its usual head."""
    facts = [
        _row(i, f"Alice note {i}: item {i * 7} was filed", similarity=0.9 - i / 1000)
        for i in range(1, 101)
    ]
    seg_cur = _both_legs(_segment_row(9001, ANSWER))
    seen, pair = _recording({"4417": 0.95})
    with patch.object(reranker, "rerank_pair", pair):
        rows = await _search_with_segments(_Cursor(vector=facts, keyword=[]), seg_cur, limit=10)

    assert len(seen) == reranker.RERANK_MAX_CANDIDATES
    assert rows[0]["kind"] == "segment", "a segment the head held anyway keeps its rank"


@pytest.mark.asyncio
async def test_only_the_best_segment_is_judged_beside_a_head_of_facts(
    env: pytest.MonkeyPatch,
) -> None:
    """One more pair, for the segment its own legs rank first; the next
    segment is not judged."""
    best = _segment_row(9001, ANSWER)
    other = _segment_row(9002, "User: Bob's bike is blue.\nAssistant: Noted.")
    seen, pair = _recording({"4417": 0.95})
    with patch.object(reranker, "rerank_pair", pair):
        await _search_with_segments(_two_leg_facts(), _both_legs(best, other), limit=10)

    assert len(seen) == reranker.RERANK_MAX_CANDIDATES + 1
    assert any("4417" in d for d in seen)
    assert not any("bike is blue" in d for d in seen)


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


def test_the_judged_segment_is_marked_when_it_takes_the_last_place() -> None:
    from robothor.memory.facts import _last_in_head

    rows = [
        {"kind": "fact", "id": 1},
        {"kind": "segment", "id": 7},
        {"kind": "fact", "id": 2},
    ]
    out = _last_in_head(rows, ("segment", 7))
    assert [(r["kind"], r["id"]) for r in out] == [("fact", 1), ("fact", 2), ("segment", 7)]
    assert out[-1].get("judged_in") is True
    assert all("judged_in" not in r for r in out[:-1])


# --------------------------------------------------------------------------- #
# Without a working reranker the base rule stands
# --------------------------------------------------------------------------- #


def _thirty_two_leg_then_forty_one_leg() -> _Cursor:
    both = _two_leg_facts(30)
    more = [
        _row(200 + i, f"Bob note {i}: crate {i * 3} was shipped", similarity=0.5 - i / 1000)
        for i in range(40)
    ]
    return _Cursor(vector=[*both.vector, *more], keyword=both.keyword)


async def _limit_50(env: pytest.MonkeyPatch, *, judge: bool) -> list[dict[str, Any]]:
    """30 facts both fact legs found, then an answer segment found by both of
    the segment store's legs (fused rank 30: past the 16-row head, inside
    50), then 40 facts one leg found, so the pool is longer than the limit."""
    from robothor.memory import facts

    if not judge:
        env.setattr(facts, "_segment_to_judge", lambda pool, head: None)
    seg_cur = _both_legs(_segment_row(9001, ANSWER))
    return await _search_with_segments(_thirty_two_leg_then_forty_one_leg(), seg_cur, limit=50)


@pytest.mark.asyncio
async def test_with_the_reranker_unavailable_the_fused_order_stands(
    env: pytest.MonkeyPatch,
) -> None:
    env.setattr(reranker, "check_reranker_available", AsyncMock(return_value=False))
    with patch.object(reranker, "rerank_pair", _scorer({"4417": 0.95})):
        rows = await _limit_50(env, judge=True)
        base = await _limit_50(env, judge=False)

    assert ("segment", 9001) in _keys(base), "the base rule returns the segment"
    assert _keys(rows) == _keys(base)


@pytest.mark.asyncio
async def test_with_every_rerank_pair_failing_the_fused_order_stands(
    env: pytest.MonkeyPatch,
) -> None:
    async def failing(client: Any, query: str, doc: str) -> float:
        raise RuntimeError("reranker down")

    with patch.object(reranker, "rerank_pair", failing):
        rows = await _limit_50(env, judge=True)
        base = await _limit_50(env, judge=False)

    assert ("segment", 9001) in _keys(base), "the base rule returns the segment"
    assert _keys(rows) == _keys(base)
