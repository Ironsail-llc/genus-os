"""A segment judged in beside a head of facts reaches the context.

It takes the head's last place, but it is long and the head's facts have
already used much of the cap. Skipped there, the short rows after it filled
the room, the end-of-pack cut never fired, and a segment judged relevant
never reached the model. Now a row marked ``judged_in`` that does not fit
whole is cut to fit at the point it is reached, when at least the minimum
cut remains; every other row keeps the packing rule it had.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.memory import eval_systems, facts, recall, segments
from robothor.memory.recall import RECALL_MIN_CUT_CHARS, TRUNCATION_MARKER
from robothor.memory.tests.test_search_facts_ranking import _conn, _Cursor, _row, _scorer
from robothor.memory.tests.test_segment_fusion import _both_legs, _segment_row
from robothor.rag import reranker

CAP = recall.RECALL_CONTEXT_CHARS
ANSWER = "User: which locker does Alice use?\nAssistant: Alice uses locker 4417. " + " ".join(
    f"Note {i}: the bike rack by entrance {i} is shared." for i in range(60)
)


def _rows(sizes: list[int], judged_at: int | None = None) -> list[dict[str, Any]]:
    out = []
    for i, n in enumerate(sizes):
        row = {"id": i, "kind": "fact", "fact_text": "x" * n}
        if i == judged_at:
            row = {"id": i, "kind": "segment", "fact_text": "s" * n, "judged_in": True}
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# The packing rule
# --------------------------------------------------------------------------- #


def test_a_judged_row_that_does_not_fit_is_cut_where_it_is_reached() -> None:
    rows = _rows([150] * 16 + [2500] + [150] * 20, judged_at=16)
    kept = recall.within_context_cap(rows)

    assert kept[16]["id"] == 16
    assert kept[16]["fact_text"].endswith(TRUNCATION_MARKER)
    assert len(kept[16]["fact_text"]) == CAP - 16 * 150
    assert sum(len(r["fact_text"]) for r in kept) == CAP
    assert [r["id"] for r in kept[:16]] == list(range(16)), "the head stays whole"


def test_an_unjudged_row_keeps_the_old_rule() -> None:
    rows = _rows([150] * 16 + [2500] + [150] * 20)
    kept = recall.within_context_cap(rows)

    assert 16 not in [r["id"] for r in kept]


def test_a_judged_row_is_skipped_when_less_than_the_minimum_is_left() -> None:
    head = (CAP - RECALL_MIN_CUT_CHARS + 1) // 150 + 1
    rows = _rows([150] * head + [2500] + [10] * 5, judged_at=head)
    kept = recall.within_context_cap(rows)

    assert CAP - head * 150 < RECALL_MIN_CUT_CHARS
    assert head not in [r["id"] for r in kept]


def test_the_eval_packer_cuts_a_judged_candidate_where_it_is_reached() -> None:
    cands = [(f"f{i}", "fact", "x" * 600) for i in range(16)]
    cands += [
        ("t-judged", "turn", "s" * 10_000),
        *((f"g{i}", "fact", "y" * 600) for i in range(20)),
    ]
    items = eval_systems.fill_budget(cands, 4000, cut_now=frozenset({"t-judged"}))

    assert [i.id for i in items][16] == "t-judged"
    assert items[16].text.endswith(TRUNCATION_MARKER)
    assert sum(i.tokens for i in items) <= 4000


# --------------------------------------------------------------------------- #
# Acceptance: the search_memory tool returns the judged segment
# --------------------------------------------------------------------------- #


def _realistic_two_leg_facts(n: int = 100) -> _Cursor:
    """``n`` facts of about 150 characters about Alice, found by both legs."""
    rows = [
        _row(
            i,
            (
                f"Alice note {i}: item {i * 7} was filed in cabinet {i * 3} by clerk "
                f"{i * 11} after review {i * 13}, with copy {i * 17} sent to desk {i * 19} "
                f"and box {i * 23} on shelf {i * 29} of row {i * 31}."
            ),
            similarity=0.9 - i / 1000,
        )
        for i in range(1, n + 1)
    ]
    keyword = [dict(r, keyword_score=0.5 - i / 1000) for i, r in enumerate(rows)]
    return _Cursor(vector=rows, keyword=keyword)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for flag in (
        "MEMORY_RERANK_WIDE",
        "MEMORY_EPISODE_MERGE",
        "MEMORY_TEMPORAL_COHERENCE",
        "MEMORY_RERANK_MAX_CANDIDATES",
        "MEMORY_DEDUP",
        "MEMORY_NARROW_SEARCH",
    ):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("MEMORY_RANK_BLEND", "1")
    monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
    return monkeypatch


def test_the_fixture_facts_are_about_150_characters() -> None:
    lengths = [len(r["fact_text"]) for r in _realistic_two_leg_facts().vector]
    assert min(lengths) >= 130 and max(lengths) <= 175
    assert 2400 <= len(ANSWER) <= 3200


@pytest.mark.asyncio
@pytest.mark.parametrize("routed", [False, True])
async def test_search_memory_returns_the_judged_segment_cut_to_fit(
    env: pytest.MonkeyPatch, routed: bool
) -> None:
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers.memory import HANDLERS

    if routed:
        env.setenv("ROBOTHOR_RIP_15_ENABLED", "1")
    else:
        env.delenv("ROBOTHOR_RIP_15_ENABLED", raising=False)
    cur = _realistic_two_leg_facts()
    seg_cur = _both_legs(_segment_row(9001, ANSWER))
    ctx = ToolContext(agent_id="main", run_id="", tenant_id="test-tenant", user_id="")
    with (
        patch.object(reranker, "rerank_pair", _scorer({"4417": 0.95})),
        patch.object(facts.llm_client, "get_embedding_async", AsyncMock(return_value=[0.1] * 8)),
        patch.object(facts, "get_connection", return_value=_conn(cur)),
        patch.object(facts, "apply_hnsw_session"),
        patch.object(segments, "get_connection", return_value=_conn(seg_cur)),
        patch.object(segments, "apply_hnsw_session"),
    ):
        out = await HANDLERS["search_memory"](
            {"query": "which locker does Alice use"}, ctx, record_access=False
        )

    texts = [str(r.get("fact") or r.get("text") or "") for r in out["results"]]
    holding = [t for t in texts if "4417" in t]
    assert holding, f"{len(texts)} rows, none holds the judged segment"
    assert holding[0].endswith(TRUNCATION_MARKER)
    assert sum(len(t) for t in texts) <= CAP
