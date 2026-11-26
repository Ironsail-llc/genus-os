"""The graph leg is off by default; recall and warm-up run it only when asked.

Ranked by the meaning of the question, the leg met its retrieval gate on the
memory evaluation's world (dev split, top 5: reworded multi-hop 21 to 56 of
126, no stratum worse), and in the answer-and-judge arm it raised multi-hop
answers (reworded 1 to 12 of 40). But it lowered cross-tenant answers from 30
to 26 of 40 (4 worse, none better, p = 0.125): for "which city is X based
in?" its neighbour facts (an employer's headquarters) drew the answer to the
wrong city. Any stratum worse at p < 0.20 blocks a gate, so ``search_facts``
runs the leg only on an explicit ``include_graph=True``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.memory.recall import Candidate


def _one_fact_legs(*_a: Any, **_k: Any) -> dict[str, list[Candidate]]:
    c = Candidate(id=1, kind="fact", text="Alice Smith runs the depot", payload={
        "id": 1, "fact_text": "Alice Smith runs the depot", "category": "event",
        "entities": ["Alice Smith"], "age_seconds": 10.0, "importance_score": 0.5,
    })  # fmt: skip
    return {"vector": [c], "keyword": []}


@pytest.fixture
def no_io(monkeypatch):
    from robothor.memory import activation, facts

    calls: list[str] = []

    def _walk(*_a: Any, **_k: Any) -> list[Candidate]:
        calls.append("graph")
        return []

    async def _expansion(*_a: Any, **_k: Any) -> list[Candidate]:
        calls.append("entity_expansion")
        return []

    monkeypatch.setattr(facts, "_embed_query", AsyncMock(return_value=[0.1] * 4))
    monkeypatch.setattr(facts, "_fact_legs", _one_fact_legs)
    monkeypatch.setattr(activation, "graph_candidates_for_query", _walk)
    monkeypatch.setattr(activation, "question_for_graph", lambda _t, q, **_k: q)
    monkeypatch.setattr(facts, "_entity_expansion_leg", _expansion)
    return calls


async def test_search_facts_runs_no_graph_leg_by_default(no_io) -> None:
    from robothor.memory import facts

    assert facts.GRAPH_LEG_DEFAULT is False
    await facts.search_facts("Who runs the depot?", tenant_id="test-tenant", use_reranker=False)
    assert "graph" not in no_io


async def test_expand_entities_means_the_one_hop_leg_not_the_graph(no_io) -> None:
    from robothor.memory import facts

    await facts.search_facts(
        "Who runs the depot?", tenant_id="test-tenant", use_reranker=False, expand_entities=True
    )
    assert no_io == ["entity_expansion"]


async def test_the_one_hop_expansion_answers_when_the_graph_finds_nothing(no_io) -> None:
    from robothor.memory import facts

    await facts.search_facts(
        "Who runs the depot?",
        tenant_id="test-tenant",
        use_reranker=False,
        expand_entities=True,
        include_graph=True,
    )
    assert no_io == ["graph", "entity_expansion"]


async def test_an_explicit_include_graph_runs_the_walk(no_io) -> None:
    from robothor.memory import facts

    await facts.search_facts(
        "Who runs the depot?", tenant_id="test-tenant", use_reranker=False, include_graph=True
    )
    assert "graph" in no_io


def test_warm_up_walks_the_graph_only_when_asked(monkeypatch) -> None:
    from robothor.engine import warmup

    walked: list[bool] = []
    monkeypatch.setattr(warmup, "_graph_entity_lines", lambda *a, **k: walked.append(True) or [])
    monkeypatch.setattr(warmup, "_fetch_entity_facts_batched", lambda *a, **k: [])
    # Warm-up opens a connection before it fetches; a unit test has no database.
    monkeypatch.setattr("robothor.db.get_connection", lambda *a, **k: MagicMock())

    warmup._build_entity_context("Did Alice Smith sign it?", tenant_id="test-tenant")
    assert walked == []
    warmup._build_entity_context(
        "Did Alice Smith sign it?", tenant_id="test-tenant", include_graph=True
    )
    assert walked == [True]
