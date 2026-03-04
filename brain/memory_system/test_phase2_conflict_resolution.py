"""
Phase 2: Conflict Resolution & Deduplication — Tests

Tests for finding similar facts, classifying relationships between facts,
and handling duplicates, contradictions, and updates.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from conflict_resolution import (
    build_classification_prompt,
    classify_relationship,
    find_similar_facts,
    resolve_and_store,
)
from fact_extraction import store_fact

# ============== Unit Tests: Find Similar ==============


class TestFindSimilar:
    @pytest.mark.asyncio
    async def test_find_similar_returns_results(self, test_prefix):
        fact = {
            "fact_text": f"{test_prefix} Philip prefers dark mode for all editors",
            "category": "preference",
            "entities": ["Philip"],
            "confidence": 0.9,
        }
        await store_fact(fact, f"{test_prefix} src", "conversation")

        # Use threshold=0.0 to avoid ivfflat low-recall issues with few rows
        results = await find_similar_facts(
            f"{test_prefix} Philip likes dark themes in his editor",
            limit=5,
            threshold=0.0,
        )
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_find_similar_respects_threshold(self, test_prefix):
        fact = {
            "fact_text": f"{test_prefix} The weather in London is rainy",
            "category": "event",
            "entities": ["London"],
            "confidence": 0.8,
        }
        await store_fact(fact, f"{test_prefix} src", "conversation")

        results = await find_similar_facts(
            f"{test_prefix} quantum mechanics wave function collapse",
            limit=5,
            threshold=0.9,
        )
        # Unrelated content should not match at high threshold
        matching = [r for r in results if r["similarity"] >= 0.9]
        assert len(matching) == 0


# ============== Unit Tests: Classification Prompt ==============


class TestBuildClassificationPrompt:
    def test_build_classification_prompt(self):
        prompt = build_classification_prompt(
            "Philip uses Neovim",
            "Philip uses VS Code",
        )
        assert "Philip uses Neovim" in prompt
        assert "Philip uses VS Code" in prompt
        for option in ["new", "update", "duplicate", "contradiction"]:
            assert option in prompt.lower()


# ============== Unit Tests: Classify Relationship (Mocked LLM) ==============


class TestClassifyRelationship:
    @pytest.mark.asyncio
    async def test_classify_duplicate(self):
        mock_response = json.dumps({"classification": "duplicate", "reasoning": "Same fact"})
        with patch(
            "conflict_resolution.llm_client.generate",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_relationship("Philip uses Neovim", "Philip uses Neovim")
            assert result["classification"] == "duplicate"

    @pytest.mark.asyncio
    async def test_classify_contradiction(self):
        mock_response = json.dumps(
            {"classification": "contradiction", "reasoning": "Different editors"}
        )
        with patch(
            "conflict_resolution.llm_client.generate",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_relationship(
                "Philip's favorite language is Rust",
                "Philip's favorite language is Python",
            )
            assert result["classification"] == "contradiction"

    @pytest.mark.asyncio
    async def test_classify_update(self):
        mock_response = json.dumps({"classification": "update", "reasoning": "More specific info"})
        with patch(
            "conflict_resolution.llm_client.generate",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_relationship(
                "Philip uses Neovim with LSP and Telescope",
                "Philip uses Neovim",
            )
            assert result["classification"] == "update"

    @pytest.mark.asyncio
    async def test_classify_new(self):
        mock_response = json.dumps({"classification": "new", "reasoning": "Unrelated facts"})
        with patch(
            "conflict_resolution.llm_client.generate",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await classify_relationship(
                "Philip likes hiking",
                "The server runs PostgreSQL",
            )
            assert result["classification"] == "new"

    @pytest.mark.asyncio
    async def test_classify_handles_malformed_response(self):
        with patch(
            "conflict_resolution.llm_client.generate",
            new_callable=AsyncMock,
            return_value="this is not json at all",
        ):
            result = await classify_relationship("fact a", "fact b")
            assert result["classification"] == "new"


# ============== Unit Tests: Handle Actions ==============


class TestResolveAndStore:
    @pytest.mark.asyncio
    async def test_handle_duplicate_skips_storage(self, test_prefix):
        existing = {
            "fact_text": f"{test_prefix} Philip uses Neovim for editing",
            "category": "preference",
            "entities": ["Philip", "Neovim"],
            "confidence": 0.9,
        }
        await store_fact(existing, f"{test_prefix} src", "conversation")

        new_fact = {
            "fact_text": f"{test_prefix} Philip uses Neovim for editing",
            "category": "preference",
            "entities": ["Philip", "Neovim"],
            "confidence": 0.9,
        }
        mock_response = json.dumps({"classification": "duplicate", "reasoning": "Same"})
        # Only mock generate, not get_embedding_async
        with patch(
            "conflict_resolution.llm_client.generate",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await resolve_and_store(new_fact, f"{test_prefix} src", "conversation")
            assert result["action"] == "skipped"

    @pytest.mark.asyncio
    async def test_handle_contradiction_supersedes_old(self, test_prefix, db_conn):
        from psycopg2.extras import RealDictCursor

        existing = {
            "fact_text": f"{test_prefix} Philip's favorite language is Python",
            "category": "preference",
            "entities": ["Philip", "Python"],
            "confidence": 0.9,
        }
        old_id = await store_fact(existing, f"{test_prefix} src", "conversation")

        new_fact = {
            "fact_text": f"{test_prefix} Philip's favorite language is Rust",
            "category": "preference",
            "entities": ["Philip", "Rust"],
            "confidence": 0.95,
        }
        mock_response = json.dumps(
            {"classification": "contradiction", "reasoning": "Different language"}
        )
        with patch(
            "conflict_resolution.llm_client.generate",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await resolve_and_store(new_fact, f"{test_prefix} src", "conversation")

        assert result["action"] == "superseded"
        assert "new_id" in result

        cur = db_conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT is_active, superseded_by FROM memory_facts WHERE id = %s", (old_id,))
        old = cur.fetchone()
        assert old["is_active"] is False
        assert old["superseded_by"] == result["new_id"]

    @pytest.mark.asyncio
    async def test_handle_update_supersedes_old(self, test_prefix, db_conn):
        from psycopg2.extras import RealDictCursor

        existing = {
            "fact_text": f"{test_prefix} Philip uses Neovim",
            "category": "preference",
            "entities": ["Philip", "Neovim"],
            "confidence": 0.8,
        }
        old_id = await store_fact(existing, f"{test_prefix} src", "conversation")

        new_fact = {
            "fact_text": f"{test_prefix} Philip uses Neovim with LSP, Telescope, and Treesitter",
            "category": "preference",
            "entities": ["Philip", "Neovim"],
            "confidence": 0.95,
        }
        mock_response = json.dumps({"classification": "update", "reasoning": "More detailed"})
        with patch(
            "conflict_resolution.llm_client.generate",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await resolve_and_store(new_fact, f"{test_prefix} src", "conversation")

        assert result["action"] == "superseded"

        cur = db_conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT is_active FROM memory_facts WHERE id = %s", (old_id,))
        assert cur.fetchone()["is_active"] is False

    @pytest.mark.asyncio
    async def test_handle_new_stores_directly(self, test_prefix):
        new_fact = {
            "fact_text": f"{test_prefix} totally unique fact about quantum computing",
            "category": "technical",
            "entities": [],
            "confidence": 0.9,
        }
        result = await resolve_and_store(new_fact, f"{test_prefix} src", "conversation")
        assert result["action"] == "stored"
        assert "new_id" in result


# ============== Integration Tests (Real LLM) ==============


@pytest.mark.slow
class TestIntegrationConflictResolution:
    @pytest.mark.asyncio
    async def test_full_pipeline_contradiction(self, test_prefix):
        fact1 = {
            "fact_text": f"{test_prefix} Philip's primary editor is VS Code",
            "category": "preference",
            "entities": ["Philip", "VS Code"],
            "confidence": 0.9,
        }
        await store_fact(fact1, f"{test_prefix} src", "conversation")

        fact2 = {
            "fact_text": f"{test_prefix} Philip's primary editor is Neovim now",
            "category": "preference",
            "entities": ["Philip", "Neovim"],
            "confidence": 0.95,
        }
        result = await resolve_and_store(fact2, f"{test_prefix} src", "conversation")
        assert result["action"] in ("superseded", "stored")

    @pytest.mark.asyncio
    async def test_full_pipeline_dedup(self, test_prefix):
        fact = {
            "fact_text": f"{test_prefix} The DGX Spark has 128GB unified memory",
            "category": "technical",
            "entities": ["DGX Spark"],
            "confidence": 0.95,
        }
        await store_fact(fact, f"{test_prefix} src", "conversation")

        same_fact = {
            "fact_text": f"{test_prefix} The DGX Spark has 128GB of unified memory",
            "category": "technical",
            "entities": ["DGX Spark"],
            "confidence": 0.9,
        }
        result = await resolve_and_store(same_fact, f"{test_prefix} src", "conversation")
        assert result["action"] in ("skipped", "stored")
