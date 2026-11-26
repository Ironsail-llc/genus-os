"""Tests for `robothor.memory.router` — query-classed recall (RIP 15)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.memory import router


class TestClassifyQuery:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("How do I rotate the SOPS key?", "how_to"),
            ("What's the FakeVendorCo support phone number?", "exact_lookup"),
            ("What did Alice decide most recently about storage?", "temporal"),
            ("Who is the security lead at FakeVendorCo?", "who_is"),
            ("What am I working toward this quarter?", "intent"),
            ("Tell me about the Helios project", "default"),
        ],
    )
    def test_classification(self, query: str, expected: str) -> None:
        assert router.classify_query(query) == expected


def _facts(*rows: dict) -> AsyncMock:
    return AsyncMock(return_value=list(rows))


class TestRecallRouting:
    @pytest.mark.asyncio
    async def test_default_only_hits_facts_with_insights(self) -> None:
        with (
            patch(
                "robothor.memory.facts.search_facts", new=_facts({"id": 1, "fact_text": "x"})
            ) as sf,
            patch("robothor.memory.vault.search_vault", new=AsyncMock()) as sv,
            patch("robothor.memory.intents.search_intents", new=AsyncMock()) as si,
        ):
            out = await router.recall("Tell me about Helios", tenant_id="t1")

        assert out["query_class"] == "default"
        kwargs = sf.call_args.kwargs
        assert kwargs["include_insights"] is True
        assert kwargs["expand_entities"] is False
        assert kwargs["include_episodes"] is False
        sv.assert_not_called()  # no fan-out to the vault on a default query
        si.assert_not_called()

    @pytest.mark.asyncio
    async def test_exact_lookup_hits_vault(self) -> None:
        with (
            patch("robothor.memory.facts.search_facts", new=_facts()),
            patch(
                "robothor.memory.vault.search_vault",
                new=AsyncMock(
                    return_value=[{"id": 9, "caption": "support line", "similarity": 0.9}]
                ),
            ) as sv,
        ):
            out = await router.recall("what is the support phone number", tenant_id="t1")

        assert out["query_class"] == "exact_lookup"
        sv.assert_awaited_once()
        assert any(r["source"] == "vault" for r in out["results"])

    @pytest.mark.asyncio
    async def test_who_is_expands_entities(self) -> None:
        with patch(
            "robothor.memory.facts.search_facts", new=_facts({"id": 1, "fact_text": "x"})
        ) as sf:
            await router.recall("who is the security lead", tenant_id="t1")
        assert sf.call_args.kwargs["expand_entities"] is True

    @pytest.mark.asyncio
    async def test_intent_hits_intent_store(self) -> None:
        with (
            patch("robothor.memory.facts.search_facts", new=_facts()),
            patch(
                "robothor.memory.intents.search_intents",
                new=AsyncMock(
                    return_value=[
                        {
                            "id": 3,
                            "title": "Grow ARR",
                            "description": "more revenue",
                            "similarity": 0.8,
                        }
                    ]
                ),
            ) as si,
        ):
            out = await router.recall("what am I working toward", tenant_id="t1")
        si.assert_awaited_once()
        assert any(r["source"] == "intent" for r in out["results"])

    @pytest.mark.asyncio
    async def test_temporal_reorders_by_recency(self) -> None:
        # older fact has the higher base score; recency sort must still float the newer one
        rows = [
            {"id": 1, "fact_text": "older decision", "created_at": 100, "rrf_score": 0.9},
            {"id": 2, "fact_text": "newer decision", "created_at": 200, "rrf_score": 0.1},
        ]
        with (
            patch("robothor.memory.facts.search_facts", new=AsyncMock(return_value=rows)) as sf,
            patch("robothor.memory.episodes.search_episodes", new=AsyncMock(return_value=[])),
        ):
            out = await router.recall("what did Alice decide most recently", tenant_id="t1")
        assert sf.call_args.kwargs["include_episodes"] is True
        assert out["results"][0]["id"] == 2  # newest first

    @pytest.mark.asyncio
    async def test_budget_caps_results(self) -> None:
        big = [{"id": i, "fact_text": "y" * 500} for i in range(20)]
        with patch("robothor.memory.facts.search_facts", new=AsyncMock(return_value=big)):
            out = await router.recall(
                "Tell me about Helios", tenant_id="t1", limit=20, budget_chars=1000
            )
        # 500 chars each, 1000 budget → ~2-3 before cap
        assert len(out["results"]) < 20


class TestHandlerRouting:
    @pytest.mark.asyncio
    async def test_search_memory_uses_router_when_rip15_on(self) -> None:
        from robothor.engine.tools.handlers import memory as h

        ctx = type("Ctx", (), {"tenant_id": "t1", "run_id": None, "agent_id": "main"})()
        with (
            patch("robothor.engine.feature_flags.is_rip_enabled", return_value=True),
            patch(
                "robothor.memory.router.recall",
                new=AsyncMock(
                    return_value={
                        "query_class": "default",
                        "results": [
                            {"id": 1, "source": "fact", "text": "hi", "category": "x", "score": 0.5}
                        ],
                    }
                ),
            ) as rec,
        ):
            out = await h._search_memory({"query": "hi"}, ctx)

        rec.assert_awaited_once()
        assert out["query_class"] == "default"
        assert out["results"][0]["fact"] == "hi"


async def test_a_graph_only_row_is_labelled_on_the_routed_path() -> None:
    """The routed path (RIP 15) normalises rows; it must keep what the shared
    formatter needs, so a row only the graph found reads "Related via ..."
    there too, and a row the head legs found keeps its text."""
    from robothor.engine.tools.handlers.memory import _format_results
    from robothor.memory import router

    graph_only = {
        "id": 7, "fact_text": "Example Co is headquartered in Brelford.", "category": "event",
        "leg_ranks": {"graph": 0}, "graph_path": ["Alice Smith", "Example Co"],
        "origin": "user_stated",
    }  # fmt: skip
    head = {"id": 8, "fact_text": "Alice Smith works at Example Co.", "category": "event",
            "leg_ranks": {"vector": 0, "graph": 1}, "graph_path": ["Alice Smith"]}  # fmt: skip
    rows = AsyncMock(return_value=[graph_only, head])
    with patch("robothor.memory.facts.search_facts", new=rows):
        out = await router.recall("Where is Alice Smith's employer based?", tenant_id="test-tenant")
    facts_out = {
        r["id"]: r["fact"] for r in _format_results(out["results"], out["query_class"])["results"]
    }
    assert (
        facts_out[7]
        == "Related via Alice Smith → Example Co: Example Co is headquartered in Brelford."
    )
    assert facts_out[8] == "Alice Smith works at Example Co."
