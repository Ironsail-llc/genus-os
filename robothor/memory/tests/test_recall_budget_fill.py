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
        {
            "id": i,
            "kind": "fact",
            "fact_text": f"{i:04d} " + "x" * (chars - 5),
            "category": "event",
            "confidence": 0.9,
        }
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


@pytest.mark.asyncio
@pytest.mark.parametrize(("args", "rows"), [({}, None), ({"limit": 5}, 5)])
async def test_the_mcp_tool_follows_the_same_rule(
    tool: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, args: dict[str, Any], rows: Any
) -> None:
    """MCP ``search_memory`` is recall too: it fills the same cap, and a limit
    the client names still bounds the rows."""
    from robothor.api import mcp
    from robothor.memory.recall import RECALL_CONTEXT_CHARS

    async def _tenant(_arguments: dict[str, Any]) -> str:
        return "test-tenant"

    monkeypatch.setattr(mcp, "_session_tenant", _tenant)
    out = await mcp.handle_tool_call("search_memory", {"query": "garage locker code", **args})

    texts = [r["fact"] for r in out["results"]]
    if rows is None:
        assert len(texts) > 10
        assert sum(len(t) for t in texts) <= RECALL_CONTEXT_CHARS
    else:
        assert len(texts) == rows


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


# --------------------------------------------------------------------------- #
# One packing rule, shipped and measured
# --------------------------------------------------------------------------- #


def _row(i: int, chars: int) -> dict[str, Any]:
    return {"id": i, "kind": "fact", "fact_text": "x" * chars, "category": "event"}


def test_an_oversized_row_is_skipped_and_the_cap_keeps_packing() -> None:
    """A 3,950-character segment in the head must not end the context: the
    rows after it that fit still go in whole (the rule the eval measured), and
    the big one is cut to the room they leave (``test_recall_packing.py``)."""
    from robothor.memory.recall import RECALL_CONTEXT_CHARS, TRUNCATION_MARKER, within_context_cap

    rows = [_row(1, 100), _row(2, 3950), *[_row(i, 100) for i in range(3, 33)]]
    kept = within_context_cap(rows)

    ids = [r["id"] for r in kept]
    assert ids[0] == 1
    assert [i for i in ids if i != 2] == [1, *range(3, 33)], "every 100-character row fits"
    assert next(r for r in kept if r["id"] == 2)["fact_text"].endswith(TRUNCATION_MARKER)
    assert sum(len(r["fact_text"]) for r in kept) <= RECALL_CONTEXT_CHARS


def test_the_eval_packs_with_the_products_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The eval's context is the shipped packing function, not its own copy."""
    from robothor.memory import eval_systems, recall

    calls: list[Any] = []
    real = recall.pack_within

    def spy(items: Any, cap: Any, size: Any, *rest: Any, **kw: Any) -> Any:
        calls.append(cap)
        return real(items, cap, size, *rest, **kw)

    monkeypatch.setattr(recall, "pack_within", spy)
    items = eval_systems.fill_budget([("a", "fact", "x" * 40), ("b", "fact", "y" * 40)], 15)
    assert calls == [15]
    assert [i.id for i in items] == ["a"]


@pytest.mark.asyncio
async def test_the_routed_path_skips_an_oversized_row_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from robothor.memory import router
    from robothor.memory.recall import TRUNCATION_MARKER

    rows = [_row(1, 100), _row(2, 3950), *[_row(i, 100) for i in range(3, 13)]]

    async def fake_search(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in rows]

    monkeypatch.setattr(facts, "search_facts", fake_search)
    out = await router.recall("garage locker code", tenant_id="test-tenant", limit=50)
    ids = [r["id"] for r in out["results"]]
    assert ids[0] == 1
    assert 12 in ids
    big = next(r for r in out["results"] if r["id"] == 2)
    assert big["text"].endswith(TRUNCATION_MARKER), "cut to the room left, not whole"


@pytest.mark.asyncio
async def test_a_temporal_query_keeps_the_head_ahead_of_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routed path re-sorts temporal queries by recency. Only the head may
    be re-sorted: newer unscored tail rows must not push the reranked head
    out of the context or ahead of it. Through search_memory's routed path."""
    from datetime import UTC, datetime, timedelta

    from robothor.memory import router
    from robothor.memory.facts import _fill_from_pool

    old, new = datetime(2020, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)

    def _dated(i: int, when: datetime) -> dict[str, Any]:
        return {
            "id": i,
            "kind": "fact",
            "fact_text": f"H{i:03d} " + "x" * 145 if i <= 10 else f"T{i:03d} " + "x" * 145,
            "category": "event",
            "confidence": 0.9,
            "created_at": when,
        }

    head = [dict(_dated(i, old + timedelta(days=i)), rerank_score=0.9) for i in range(1, 11)]
    tail = [_dated(i, new + timedelta(days=i)) for i in range(11, 51)]

    async def fake_search(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        pool = [dict(r) for r in head] + [dict(r) for r in tail]
        return _fill_from_pool(pool[:10], pool, int(kwargs.get("limit") or 50))

    monkeypatch.setattr(facts, "search_facts", fake_search)
    monkeypatch.setenv("ROBOTHOR_RIP_15_ENABLED", "1")
    query = "what is the latest garage locker code"
    assert router.classify_query(query) == "temporal"
    out = await _call({"query": query})

    labels = [r["fact"][:4] for r in out["results"]]
    assert len(labels) > 10, "the tail still fills after the head"
    assert sorted(labels[:10]) == [f"H{i:03d}" for i in range(1, 11)], labels[:12]
    assert all(lab.startswith("T") for lab in labels[10:])


# --------------------------------------------------------------------------- #
# Bounded work
# --------------------------------------------------------------------------- #


def test_a_named_limit_is_bounded_by_the_fill_rows() -> None:
    from robothor.memory.recall import RECALL_FILL_ROWS, recall_limit

    assert recall_limit(1000) == RECALL_FILL_ROWS
    assert recall_limit(5) == 5
    assert recall_limit(None) == RECALL_FILL_ROWS


@pytest.mark.asyncio
async def test_the_tool_never_asks_recall_for_more_than_the_bound(
    tool: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from robothor.memory.recall import RECALL_FILL_ROWS

    monkeypatch.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)
    await _call({"query": "garage locker code", "limit": 1000})
    assert tool
    assert all(int(c["limit"]) <= RECALL_FILL_ROWS for c in tool)


def test_the_fill_tokenizes_each_row_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The near-duplicate check is incremental: each kept row's tokens are
    computed once, not again for every candidate considered."""
    calls = {"n": 0}
    real = facts._norm_tokens

    def counting(text: str) -> Any:
        calls["n"] += 1
        return real(text)

    monkeypatch.setattr(facts, "_norm_tokens", counting)
    pool = [
        {"id": i, "kind": "fact", "fact_text": f"distinct note {i} about subject {i * 7}"}
        for i in range(60)
    ]
    out = facts._fill_from_pool(pool[:10], pool, 50)
    assert len(out) == 50
    assert calls["n"] <= len(pool) + 10


# --------------------------------------------------------------------------- #
# The top-ranked row is never dropped
# --------------------------------------------------------------------------- #


def test_a_single_row_over_the_cap_comes_back_truncated_and_marked() -> None:
    """Chat turns, insights and episodes have no length cap: a top-ranked row
    longer than the cap is kept, cut to the cap with a visible marker."""
    from robothor.memory.recall import (
        RECALL_CONTEXT_CHARS,
        TRUNCATION_MARKER,
        within_context_cap,
    )

    kept = within_context_cap([_row(1, RECALL_CONTEXT_CHARS * 2)])

    assert [r["id"] for r in kept] == [1]
    text = kept[0]["fact_text"]
    assert len(text) <= RECALL_CONTEXT_CHARS
    assert text.endswith(TRUNCATION_MARKER)


def test_an_all_oversized_result_is_never_empty() -> None:
    from robothor.memory.recall import RECALL_CONTEXT_CHARS, TRUNCATION_MARKER, within_context_cap

    rows = [_row(i, RECALL_CONTEXT_CHARS + 500) for i in range(1, 4)]
    kept = within_context_cap(rows)

    assert [r["id"] for r in kept] == [1], "the first, truncated; the rest skipped"
    assert kept[0]["fact_text"].endswith(TRUNCATION_MARKER)
    assert rows[0]["fact_text"] == "x" * (RECALL_CONTEXT_CHARS + 500), "the input is untouched"


def test_ordinary_packing_is_unchanged() -> None:
    from robothor.memory.recall import within_context_cap

    rows = [_row(i, 100) for i in range(1, 33)]
    kept = within_context_cap(rows)
    assert [r["id"] for r in kept] == list(range(1, 33))
    assert all("[truncated" not in r["fact_text"] for r in kept)


def test_the_eval_keeps_an_oversized_first_candidate_too() -> None:
    from robothor.memory import eval_systems
    from robothor.memory.recall import TRUNCATION_MARKER

    items = eval_systems.fill_budget([("a", "turn", "y" * 400), ("b", "fact", "z" * 20)], 50)
    assert [i.id for i in items] == ["a"]
    assert items[0].text.endswith(TRUNCATION_MARKER)
    assert items[0].tokens <= 50
