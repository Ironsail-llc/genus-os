"""The graph leg is on in recall; warm-up walks the graph only when asked.

On the memory evaluation's world, dev split, top 5, the graph leg ranked by
the question (``activation.activate_for_question``), with no weight in the
fused order and its reserved places held by its best rows, took multi-hop from
20 to 82 of 126 (62 better, none worse) with no stratum significantly worse,
so ``search_facts`` runs it on every call. ``include_graph=False`` remains only so
the replay's ``today`` preset and the evaluation's ``recall_no_graph`` can
measure recall without it. Warm-up's automatic recall ranks by activation, not
by the message, and nothing has measured it, so it stays off unless asked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

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

    monkeypatch.setattr(facts, "_embed_query", AsyncMock(return_value=None))
    monkeypatch.setattr(facts, "_fact_legs", _one_fact_legs)
    monkeypatch.setattr(activation, "graph_candidates_for_query", _walk)
    monkeypatch.setattr(facts, "_entity_expansion_leg", _expansion)
    return calls


async def test_search_facts_runs_the_graph_leg_by_default(no_io) -> None:
    from robothor.memory import facts

    assert not hasattr(facts, "GRAPH_LEG_DEFAULT"), "no module switch turns the leg off"
    await facts.search_facts("Who runs the depot?", tenant_id="test-tenant", use_reranker=False)
    assert "graph" in no_io


async def test_include_graph_false_measures_recall_without_the_walk(no_io) -> None:
    from robothor.memory import facts

    await facts.search_facts(
        "Who runs the depot?", tenant_id="test-tenant", use_reranker=False, include_graph=False
    )
    assert "graph" not in no_io


async def test_the_one_hop_expansion_answers_when_the_graph_finds_nothing(no_io) -> None:
    from robothor.memory import facts

    await facts.search_facts(
        "Who runs the depot?", tenant_id="test-tenant", use_reranker=False, expand_entities=True
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

    warmup._build_entity_context("Did Alice Smith sign it?", tenant_id="test-tenant")
    assert walked == []
    warmup._build_entity_context(
        "Did Alice Smith sign it?", tenant_id="test-tenant", include_graph=True
    )
    assert walked == [True]
