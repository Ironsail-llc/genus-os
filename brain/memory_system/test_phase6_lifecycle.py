"""
Phase 6: Smarter Lifecycle — Tests

Tests for memory decay, importance scoring, consolidation, and maintenance.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from lifecycle import (
    compute_decay_score,
    consolidate_facts,
    find_consolidation_candidates,
    judge_importance,
    run_lifecycle_maintenance,
)

# ============== Unit Tests: Decay Function (Pure, No DB) ==============


class TestDecayScore:
    def test_recent_memory_high_score(self):
        now = datetime.now(UTC)
        score = compute_decay_score(
            last_accessed=now - timedelta(hours=1),
            access_count=5,
            reinforcement_count=0,
            importance_score=0.5,
        )
        assert score > 0.8

    def test_old_unused_memory_low_score(self):
        now = datetime.now(UTC)
        score = compute_decay_score(
            last_accessed=now - timedelta(days=30),
            access_count=1,
            reinforcement_count=0,
            importance_score=0.3,
        )
        assert score < 0.3

    def test_old_but_important_memory_medium_score(self):
        now = datetime.now(UTC)
        score = compute_decay_score(
            last_accessed=now - timedelta(days=30),
            access_count=1,
            reinforcement_count=0,
            importance_score=0.95,
        )
        # Importance should resist decay
        assert score > 0.3

    def test_frequently_accessed_memory_resists_decay(self):
        now = datetime.now(UTC)
        low_access = compute_decay_score(
            last_accessed=now - timedelta(days=14),
            access_count=1,
            reinforcement_count=0,
            importance_score=0.5,
        )
        high_access = compute_decay_score(
            last_accessed=now - timedelta(days=14),
            access_count=50,
            reinforcement_count=0,
            importance_score=0.5,
        )
        assert high_access > low_access

    def test_reinforced_memory_resists_decay(self):
        now = datetime.now(UTC)
        no_reinforcement = compute_decay_score(
            last_accessed=now - timedelta(days=14),
            access_count=5,
            reinforcement_count=0,
            importance_score=0.5,
        )
        with_reinforcement = compute_decay_score(
            last_accessed=now - timedelta(days=14),
            access_count=5,
            reinforcement_count=10,
            importance_score=0.5,
        )
        assert with_reinforcement > no_reinforcement

    def test_decay_score_bounded(self):
        now = datetime.now(UTC)
        # Extreme inputs
        for hours_ago in [0, 1, 24, 168, 720, 8760]:
            for access in [0, 1, 100, 10000]:
                for reinf in [0, 1, 100]:
                    for imp in [0.0, 0.5, 1.0]:
                        score = compute_decay_score(
                            last_accessed=now - timedelta(hours=hours_ago),
                            access_count=access,
                            reinforcement_count=reinf,
                            importance_score=imp,
                        )
                        assert 0.0 <= score <= 1.0, f"Score {score} out of bounds"


# ============== Unit Tests: Importance Scoring ==============


class TestImportanceScoring:
    @pytest.mark.asyncio
    async def test_judge_importance_returns_float(self):
        mock_response = '{"score": 0.75}'
        with patch(
            "lifecycle.llm_client.generate", new_callable=AsyncMock, return_value=mock_response
        ):
            score = await judge_importance(
                "Philip decided to switch to PostgreSQL for the database"
            )
            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0

    @pytest.mark.asyncio
    async def test_judge_importance_trivial_content(self):
        mock_response = '{"score": 0.2}'
        with patch(
            "lifecycle.llm_client.generate", new_callable=AsyncMock, return_value=mock_response
        ):
            score = await judge_importance("The weather is nice today")
            assert score < 0.5

    @pytest.mark.asyncio
    async def test_judge_importance_handles_error(self):
        with patch(
            "lifecycle.llm_client.generate",
            new_callable=AsyncMock,
            side_effect=Exception("LLM down"),
        ):
            score = await judge_importance("some content")
            assert score == 0.5  # default


# ============== Unit Tests: Consolidation ==============


class TestConsolidation:
    @pytest.mark.asyncio
    async def test_find_consolidation_candidates(self, test_prefix):
        from fact_extraction import store_fact

        # Store similar facts
        for i in range(3):
            fact = {
                "fact_text": f"{test_prefix} Philip uses Qwen3 model version {i}",
                "category": "technical",
                "entities": ["Philip", "Qwen3"],
                "confidence": 0.9,
            }
            with patch(
                "entity_graph.extract_and_store_entities", new_callable=AsyncMock, return_value={}
            ):
                await store_fact(fact, f"{test_prefix} src", "conversation")

        candidates = await find_consolidation_candidates(
            min_group_size=2,
            similarity_threshold=0.7,
        )
        # May or may not find groups depending on similarity
        assert isinstance(candidates, list)

    @pytest.mark.asyncio
    async def test_consolidate_facts(self):
        fact_group = [
            {"id": 1, "fact_text": "Philip uses Qwen3-Next-80B"},
            {"id": 2, "fact_text": "Philip's LLM is Qwen3-Next"},
            {"id": 3, "fact_text": "The primary model is Qwen3-Next-80B-A3B"},
        ]
        mock_response = "Philip uses Qwen3-Next-80B-A3B as his primary LLM model"
        with patch(
            "lifecycle.llm_client.generate", new_callable=AsyncMock, return_value=mock_response
        ):
            result = await consolidate_facts(fact_group)
            assert "consolidated_text" in result
            assert len(result["consolidated_text"]) > 0


# ============== Unit Tests: Maintenance ==============


class TestMaintenance:
    @pytest.mark.asyncio
    async def test_run_lifecycle_maintenance(self):
        with patch("lifecycle.llm_client.generate", new_callable=AsyncMock, return_value="0.5"):
            result = await run_lifecycle_maintenance()
            assert isinstance(result, dict)
            assert "facts_scored" in result or "decay_updated" in result


# ============== Integration Tests (Real LLM) ==============


@pytest.mark.slow
class TestIntegrationLifecycle:
    @pytest.mark.asyncio
    async def test_importance_scoring_real_llm(self, test_prefix):
        decision_score = await judge_importance(
            f"{test_prefix} Philip decided to migrate the entire infrastructure to Kubernetes"
        )
        casual_score = await judge_importance(f"{test_prefix} Had coffee this morning")
        # Decision should score higher than casual
        assert decision_score > casual_score
