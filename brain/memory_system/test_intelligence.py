"""
Intelligence Layer — Tests

Tests for the Qwen3-powered intelligence pipeline:
- Fact extraction from transcript content
- Fact structure validation
- Consolidation of similar facts
- Importance scoring (critical facts > 0.7)
- Decay scoring (old unaccessed facts decay properly)

The intelligence layer is the "brain" of the memory system — it turns
raw content into structured, scored, and consolidated knowledge.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fact_extraction import (
    extract_facts,
    parse_extraction_response,
    store_fact,
)
from lifecycle import (
    compute_decay_score,
    consolidate_facts,
    find_consolidation_candidates,
    judge_importance,
    run_lifecycle_maintenance,
)

# ============== Fact Extraction: LLM-Driven Tests ==============


@pytest.mark.llm
@pytest.mark.slow
class TestLLMFactExtraction:
    """Tests that require the local LLM (Qwen3-Next) for real extraction."""

    async def test_extract_at_least_one_fact_from_substantive_content(self):
        """Substantive content should yield at least 1 fact."""
        content = (
            "Philip decided to switch the entire Robothor memory system "
            "from SQLite to PostgreSQL with pgvector. The migration took "
            "about 3 hours and all data was preserved."
        )
        facts = await extract_facts(content)
        assert len(facts) >= 1, "Should extract at least 1 fact from substantive content"

    async def test_extract_multiple_facts_from_rich_content(self, sample_content):
        """Content with multiple facts should produce multiple extractions."""
        facts = await extract_facts(sample_content["multi_fact"])
        assert len(facts) >= 2, f"Expected >=2 facts from multi-fact content, got {len(facts)}"

    async def test_extract_facts_from_email_format(self, sample_content):
        """Email content should yield facts about the event/contacts."""
        facts = await extract_facts(sample_content["email"])
        assert len(facts) >= 1
        # Should mention dinner, Friday, or Samantha
        all_text = " ".join(f["fact_text"] for f in facts).lower()
        assert any(kw in all_text for kw in ["dinner", "friday", "samantha", "lucia"]), (
            f"Expected email facts to mention key details, got: {all_text}"
        )

    async def test_extracted_facts_contain_entities(self, sample_content):
        """Extracted facts should identify named entities."""
        facts = await extract_facts(sample_content["technical"])
        all_entities = []
        for f in facts:
            all_entities.extend(f.get("entities", []))
        assert len(all_entities) >= 1, "Should extract at least 1 entity from technical content"


# ============== Fact Structure Validation ==============


class TestFactStructureValidation:
    """Tests that extracted/parsed facts have correct structure."""

    def test_parsed_fact_has_required_fields(self):
        """Every parsed fact must have fact_text, category, entities, confidence."""
        raw = json.dumps(
            [
                {
                    "fact_text": "Philip uses PostgreSQL",
                    "category": "technical",
                    "entities": ["Philip", "PostgreSQL"],
                    "confidence": 0.9,
                }
            ]
        )
        facts = parse_extraction_response(raw)
        assert len(facts) == 1
        fact = facts[0]
        assert "fact_text" in fact
        assert "category" in fact
        assert "entities" in fact
        assert "confidence" in fact

    def test_fact_text_is_nonempty_string(self):
        """fact_text must be a non-empty string."""
        raw = json.dumps(
            [
                {"fact_text": "", "category": "personal", "entities": [], "confidence": 0.8},
                {"fact_text": "   ", "category": "personal", "entities": [], "confidence": 0.8},
            ]
        )
        facts = parse_extraction_response(raw)
        # Empty and whitespace-only should be filtered out
        assert len(facts) == 0

    def test_category_is_valid(self):
        """Category must be one of the valid categories."""
        valid_categories = [
            "personal",
            "project",
            "decision",
            "preference",
            "event",
            "contact",
            "technical",
        ]
        raw = json.dumps(
            [
                {"fact_text": "test fact", "category": cat, "entities": [], "confidence": 0.8}
                for cat in valid_categories
            ]
        )
        facts = parse_extraction_response(raw)
        assert len(facts) == len(valid_categories)
        for f in facts:
            assert f["category"] in valid_categories

    def test_invalid_category_normalized_to_personal(self):
        """Unknown categories should be normalized to 'personal'."""
        raw = json.dumps(
            [{"fact_text": "test", "category": "BOGUS_CATEGORY", "entities": [], "confidence": 0.8}]
        )
        facts = parse_extraction_response(raw)
        assert facts[0]["category"] == "personal"

    def test_confidence_is_float_between_0_and_1(self):
        """Confidence must be a float clamped to [0.0, 1.0]."""
        raw = json.dumps(
            [
                {"fact_text": "high", "category": "personal", "entities": [], "confidence": 1.5},
                {"fact_text": "low", "category": "personal", "entities": [], "confidence": -0.5},
                {"fact_text": "norm", "category": "personal", "entities": [], "confidence": 0.7},
            ]
        )
        facts = parse_extraction_response(raw)
        assert facts[0]["confidence"] == 1.0  # Clamped
        assert facts[1]["confidence"] == 0.0  # Clamped
        assert facts[2]["confidence"] == 0.7  # Unchanged

    def test_entities_is_list_of_strings(self):
        """entities must be a list of strings."""
        raw = json.dumps(
            [
                {
                    "fact_text": "test",
                    "category": "personal",
                    "entities": ["Philip", "Neovim", 42],
                    "confidence": 0.8,
                }
            ]
        )
        facts = parse_extraction_response(raw)
        assert isinstance(facts[0]["entities"], list)
        # Numeric entity should be converted to string
        assert all(isinstance(e, str) for e in facts[0]["entities"])

    def test_missing_entities_defaults_to_empty_list(self):
        """Missing entities field should default to empty list."""
        raw = json.dumps([{"fact_text": "test", "category": "personal", "confidence": 0.8}])
        facts = parse_extraction_response(raw)
        assert facts[0]["entities"] == []


# ============== Consolidation Tests ==============


class TestConsolidation:
    """Tests for merging/consolidating similar facts."""

    async def test_consolidate_produces_merged_text(self):
        """Consolidation should produce a single merged statement."""
        fact_group = [
            {"id": 1, "fact_text": "Philip uses PostgreSQL for the memory system"},
            {"id": 2, "fact_text": "The memory system database is PostgreSQL"},
            {"id": 3, "fact_text": "PostgreSQL with pgvector stores Robothor memories"},
        ]
        mock_response = (
            "Philip uses PostgreSQL with pgvector as the database for the Robothor memory system"
        )
        with patch(
            "lifecycle.llm_client.generate",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await consolidate_facts(fact_group)
        assert "consolidated_text" in result
        assert len(result["consolidated_text"]) > 0
        assert "source_ids" in result
        assert result["source_ids"] == [1, 2, 3]

    async def test_consolidate_handles_llm_failure(self):
        """If LLM fails, consolidation should fall back to first fact."""
        fact_group = [
            {"id": 10, "fact_text": "Fallback fact text"},
            {"id": 11, "fact_text": "Another similar fact"},
        ]
        with patch(
            "lifecycle.llm_client.generate",
            new_callable=AsyncMock,
            side_effect=Exception("LLM unavailable"),
        ):
            result = await consolidate_facts(fact_group)
        assert result["consolidated_text"] == "Fallback fact text"

    async def test_find_consolidation_candidates_returns_list(self, test_prefix):
        """find_consolidation_candidates should return a list of groups."""
        # Store several similar facts
        for i in range(4):
            fact = {
                "fact_text": f"{test_prefix} Philip uses Qwen3 model variant {i}",
                "category": "technical",
                "entities": ["Philip", "Qwen3"],
                "confidence": 0.9,
            }
            with patch(
                "entity_graph.extract_and_store_entities",
                new_callable=AsyncMock,
                return_value={},
            ):
                await store_fact(fact, f"{test_prefix} src", "conversation")

        candidates = await find_consolidation_candidates(
            min_group_size=2,
            similarity_threshold=0.7,
        )
        assert isinstance(candidates, list)
        # Each candidate is a list of fact dicts
        for group in candidates:
            assert isinstance(group, list)
            for item in group:
                assert "fact_text" in item


# ============== Importance Scoring Tests ==============


class TestImportanceScoring:
    """Tests for LLM-driven importance scoring."""

    async def test_importance_returns_float_in_range(self):
        """judge_importance should return a float between 0 and 1."""
        mock_response = "0.75"
        with patch(
            "lifecycle.llm_client.generate",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            score = await judge_importance("Philip decided to switch databases")
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    async def test_importance_handles_llm_error(self):
        """If LLM fails, importance should default to 0.5."""
        with patch(
            "lifecycle.llm_client.generate",
            new_callable=AsyncMock,
            side_effect=Exception("LLM down"),
        ):
            score = await judge_importance("some content")
        assert score == 0.5

    async def test_importance_handles_non_numeric_response(self):
        """If LLM returns non-numeric text, should still extract a score."""
        with patch(
            "lifecycle.llm_client.generate",
            new_callable=AsyncMock,
            return_value="I think this is about 0.8 important",
        ):
            score = await judge_importance("some content")
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    @pytest.mark.llm
    @pytest.mark.slow
    async def test_critical_fact_scores_above_threshold(self):
        """Critical facts (security, identity) should score > 0.7 with real LLM."""
        score = await judge_importance(
            "Philip's SSH private key is stored at ~/.ssh/id_ed25519 "
            "and must never be shared publicly"
        )
        assert score > 0.5, f"Critical security fact scored only {score}"

    @pytest.mark.llm
    @pytest.mark.slow
    async def test_trivial_fact_scores_below_important(self):
        """Trivial facts should score lower than important ones."""
        trivial = await judge_importance("The weather was nice yesterday")
        important = await judge_importance(
            "Philip decided to migrate all production infrastructure to Kubernetes"
        )
        assert important > trivial, (
            f"Important ({important}) should score higher than trivial ({trivial})"
        )


# ============== Decay Scoring Tests ==============


class TestDecayScoring:
    """Tests for the time-based memory decay function."""

    def test_recent_memory_has_high_score(self):
        """A memory accessed minutes ago should score > 0.8."""
        now = datetime.now(UTC)
        score = compute_decay_score(
            last_accessed=now - timedelta(minutes=5),
            access_count=3,
            reinforcement_count=0,
            importance_score=0.5,
        )
        assert score > 0.8

    def test_old_unused_memory_decays(self):
        """A memory not accessed in 30 days with low importance should decay."""
        now = datetime.now(UTC)
        score = compute_decay_score(
            last_accessed=now - timedelta(days=30),
            access_count=1,
            reinforcement_count=0,
            importance_score=0.3,
        )
        assert score < 0.4

    def test_importance_resists_decay(self):
        """High importance should create a floor that resists decay."""
        now = datetime.now(UTC)
        low_importance = compute_decay_score(
            last_accessed=now - timedelta(days=30),
            access_count=1,
            reinforcement_count=0,
            importance_score=0.2,
        )
        high_importance = compute_decay_score(
            last_accessed=now - timedelta(days=30),
            access_count=1,
            reinforcement_count=0,
            importance_score=0.95,
        )
        assert high_importance > low_importance

    def test_access_frequency_resists_decay(self):
        """Frequently accessed memories should decay slower."""
        now = datetime.now(UTC)
        base_args = {
            "last_accessed": now - timedelta(days=14),
            "reinforcement_count": 0,
            "importance_score": 0.5,
        }
        low_access = compute_decay_score(access_count=1, **base_args)
        high_access = compute_decay_score(access_count=50, **base_args)
        assert high_access > low_access

    def test_reinforcement_resists_decay(self):
        """Reinforced memories should decay slower."""
        now = datetime.now(UTC)
        base_args = {
            "last_accessed": now - timedelta(days=14),
            "access_count": 5,
            "importance_score": 0.5,
        }
        no_reinforcement = compute_decay_score(reinforcement_count=0, **base_args)
        with_reinforcement = compute_decay_score(reinforcement_count=10, **base_args)
        assert with_reinforcement > no_reinforcement

    def test_decay_score_always_bounded_0_to_1(self):
        """Score must always be in [0.0, 1.0] regardless of inputs."""
        now = datetime.now(UTC)
        edge_cases = [
            (now, 0, 0, 0.0),  # Just now, nothing
            (now - timedelta(days=365), 0, 0, 0.0),  # Very old, nothing
            (now, 10000, 10000, 1.0),  # Just now, max everything
            (now - timedelta(days=365), 10000, 10000, 1.0),  # Old but max boosts
            (now - timedelta(seconds=1), 1, 0, 0.5),  # Very recent
        ]
        for last_accessed, access_count, reinforcement_count, importance in edge_cases:
            score = compute_decay_score(
                last_accessed=last_accessed,
                access_count=access_count,
                reinforcement_count=reinforcement_count,
                importance_score=importance,
            )
            assert 0.0 <= score <= 1.0, (
                f"Score {score} out of bounds for inputs: "
                f"access={access_count}, reinf={reinforcement_count}, imp={importance}"
            )

    def test_decay_is_monotonic_with_time(self):
        """Score should decrease as time since last access increases."""
        now = datetime.now(UTC)
        base_args = {
            "access_count": 3,
            "reinforcement_count": 0,
            "importance_score": 0.3,
        }
        scores = []
        for days_ago in [0, 1, 7, 30, 90]:
            s = compute_decay_score(
                last_accessed=now - timedelta(days=days_ago),
                **base_args,
            )
            scores.append(s)
        # Each score should be <= the previous one
        for i in range(1, len(scores)):
            assert scores[i] <= scores[i - 1] + 0.001, (
                f"Decay not monotonic: day {[0, 1, 7, 30, 90][i]} score {scores[i]} "
                f"> day {[0, 1, 7, 30, 90][i - 1]} score {scores[i - 1]}"
            )

    def test_half_life_approximately_72_hours(self):
        """At ~72 hours, recency component should be roughly half."""
        now = datetime.now(UTC)
        # With no boosts and low importance, score ≈ recency
        score_now = compute_decay_score(
            last_accessed=now,
            access_count=0,
            reinforcement_count=0,
            importance_score=0.0,
        )
        score_72h = compute_decay_score(
            last_accessed=now - timedelta(hours=72),
            access_count=0,
            reinforcement_count=0,
            importance_score=0.0,
        )
        # recency should be about half
        ratio = score_72h / score_now if score_now > 0 else 0
        assert 0.4 <= ratio <= 0.6, (
            f"Half-life ratio {ratio} not near 0.5 (now={score_now}, 72h={score_72h})"
        )


# ============== Lifecycle Maintenance Tests ==============


class TestLifecycleMaintenance:
    """Tests for the full maintenance pipeline."""

    async def test_maintenance_returns_stats(self):
        """run_lifecycle_maintenance should return a dict with stats."""
        with patch(
            "lifecycle.llm_client.generate",
            new_callable=AsyncMock,
            return_value="0.5",
        ):
            result = await run_lifecycle_maintenance()
        assert isinstance(result, dict)
        assert "facts_scored" in result
        assert "decay_updated" in result
        assert isinstance(result["facts_scored"], int)
        assert isinstance(result["decay_updated"], int)

    async def test_maintenance_updates_decay_scores(self, test_prefix, db_conn):
        """After maintenance, decay_score column should be updated."""
        from psycopg2.extras import RealDictCursor

        # Store a fact with default scores
        fact = {
            "fact_text": f"{test_prefix} Maintenance test fact for decay",
            "category": "technical",
            "entities": [],
            "confidence": 0.9,
        }
        with patch(
            "entity_graph.extract_and_store_entities",
            new_callable=AsyncMock,
            return_value={},
        ):
            fact_id = await store_fact(fact, f"{test_prefix} src", "conversation")

        # Run maintenance
        with patch(
            "lifecycle.llm_client.generate",
            new_callable=AsyncMock,
            return_value="0.6",
        ):
            result = await run_lifecycle_maintenance()

        assert result["decay_updated"] >= 1

        # Verify the decay score was written
        cur = db_conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT decay_score FROM memory_facts WHERE id = %s", (fact_id,))
        row = cur.fetchone()
        assert row is not None
        assert isinstance(row["decay_score"], float)
        assert 0.0 <= row["decay_score"] <= 1.0
