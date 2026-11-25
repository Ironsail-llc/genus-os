"""Recall returns the context budget, not only the reranked head.

The reranker scores the head of the fused list (``RERANK_MAX_CANDIDATES``,
16), and recall used to return only what it scored: a caller asking for 30 rows
got 16. Now the reranked head comes first and the remaining fused candidates
follow in fused order, up to the caller's limit; the reranker still scores only
its head. The ``search_memory`` tool fills up to the recall context cap
(``recall.RECALL_CONTEXT_CHARS``) unless the caller names a limit.

Same fake database and reranker as ``test_search_facts_ranking.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.memory import facts
from robothor.memory.tests.test_search_facts_ranking import _Cursor, _dense, _scorer, _search
from robothor.rag import reranker


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for flag in (
        "MEMORY_RERANK_WIDE",
        "MEMORY_EPISODE_MERGE",
        "MEMORY_TEMPORAL_COHERENCE",
        "MEMORY_RERANK_MAX_CANDIDATES",
    ):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("MEMORY_RANK_BLEND", "1")
    monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
    return monkeypatch


@pytest.mark.asyncio
async def test_rows_past_the_reranked_head_follow_in_fused_order(env: pytest.MonkeyPatch) -> None:
    env.setattr(reranker, "rerank_pair", _scorer({}, default=0.6))
    results = await _search(_Cursor(vector=_dense(40), keyword=[]), limit=30)

    ids = [r["id"] for r in results]
    cap = reranker.RERANK_MAX_CANDIDATES
    assert len(ids) == 30
    assert set(ids[:cap]) == set(range(1, cap + 1)), "the head is what the reranker scored"
    assert ids[cap:] == list(range(cap + 1, 31)), "the tail keeps the fused order"
    assert all("relevance" in r for r in results)
    assert all(r.get("rerank_score") is None for r in results[cap:])


@pytest.mark.asyncio
async def test_the_tail_stops_where_the_fused_score_falls_off(env: pytest.MonkeyPatch) -> None:
    """Score-adaptive truncation: a tail row is kept only while its fused score
    is at least ``recall.TAIL_SCORE_FLOOR`` of the weakest head row's. Here the
    head rows are found by both legs and every remaining row by one, at under
    half the score, so nothing follows the head."""
    dense = _dense(40)
    env.setattr(reranker, "rerank_pair", _scorer({}, default=0.6))
    results = await _search(_Cursor(vector=dense, keyword=dense[:16]), limit=30)

    assert len(results) == reranker.RERANK_MAX_CANDIDATES


def test_the_floor_is_half_the_weakest_head_score() -> None:
    from robothor.memory.recall import TAIL_SCORE_FLOOR

    assert TAIL_SCORE_FLOOR == 0.5


@pytest.mark.asyncio
async def test_the_reranker_still_scores_only_its_head(env: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def counting(client: Any, query: str, doc: str) -> float:
        calls.append(doc)
        return 0.5

    env.setattr(reranker, "rerank_pair", counting)
    await _search(_Cursor(vector=_dense(40), keyword=[]), limit=30)

    assert len(calls) <= reranker.RERANK_MAX_CANDIDATES


@pytest.mark.asyncio
async def test_a_limit_inside_the_head_is_unchanged(env: pytest.MonkeyPatch) -> None:
    env.setattr(reranker, "rerank_pair", _scorer({}, default=0.6))
    results = await _search(_Cursor(vector=_dense(40), keyword=[]), limit=5)

    assert len(results) == 5


# --------------------------------------------------------------------------- #
# The tool fills the recall context cap
# --------------------------------------------------------------------------- #


def _rows(n: int, chars: int) -> list[dict[str, Any]]:
    return [
        {"id": i, "kind": "fact", "fact_text": f"{i:04d} " + "x" * (chars - 5), "category": "event"}
        for i in range(1, n + 1)
    ]


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_search(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return _rows(int(kwargs.get("limit") or 10), 100)

    monkeypatch.setattr(facts, "search_facts", fake_search)
    return calls


async def _call(args: dict[str, Any]) -> dict[str, Any]:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers.memory import HANDLERS

    ctx = ToolContext(agent_id="main", run_id="", tenant_id="test-tenant", user_id="")
    return await HANDLERS["search_memory"](args, ctx, record_access=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("routed", [False, True])
async def test_the_tool_fills_the_recall_context_cap(
    tool: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, routed: bool
) -> None:
    from robothor.memory.recall import RECALL_CONTEXT_CHARS

    if routed:
        monkeypatch.setenv("ROBOTHOR_RIP_15_ENABLED", "1")
    else:
        monkeypatch.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)
    out = await _call({"query": "garage locker code"})

    texts = [r["fact"] for r in out["results"]]
    used = sum(len(t) for t in texts)
    assert len(texts) > 10, "more than the old default of ten rows"
    assert used <= RECALL_CONTEXT_CHARS
    assert used + 100 > RECALL_CONTEXT_CHARS, "filled to the cap, not short of it"


@pytest.mark.asyncio
@pytest.mark.parametrize("routed", [False, True])
async def test_a_limit_the_caller_names_still_bounds_the_rows(
    tool: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, routed: bool
) -> None:
    if routed:
        monkeypatch.setenv("ROBOTHOR_RIP_15_ENABLED", "1")
    else:
        monkeypatch.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)
    out = await _call({"query": "garage locker code", "limit": 5})

    assert len(out["results"]) == 5


def test_the_cap_is_the_one_the_router_applied() -> None:
    from robothor.memory import router
    from robothor.memory.recall import RECALL_CONTEXT_CHARS

    assert router.DEFAULT_BUDGET_CHARS == RECALL_CONTEXT_CHARS


@pytest.mark.asyncio
async def test_rerank_unavailable_still_fills_from_the_fused_pool(env: pytest.MonkeyPatch) -> None:
    env.setattr(reranker, "check_reranker_available", AsyncMock(return_value=False))
    with patch.object(reranker, "rerank_pair", AsyncMock(return_value=0.5)):
        results = await _search(_Cursor(vector=_dense(40), keyword=[]), limit=30)
    assert len(results) == 30
