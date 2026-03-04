"""
Phase 1: Fact Extraction Layer — Tests

Tests for extracting structured facts from unstructured content,
parsing LLM responses, and storing facts with embeddings.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fact_extraction import (
    build_extraction_prompt,
    extract_facts,
    parse_extraction_response,
    search_facts,
    store_fact,
)

# ============== Unit Tests: Prompt Building ==============


class TestBuildExtractionPrompt:
    def test_build_extraction_prompt_includes_content(self):
        content = "Philip uses Neovim for Python development."
        prompt = build_extraction_prompt(content)
        assert "Philip uses Neovim for Python development." in prompt

    def test_build_extraction_prompt_requests_facts(self):
        """Prompt should ask for facts extraction (JSON handled by Ollama format param)."""
        prompt = build_extraction_prompt("anything")
        assert "fact" in prompt.lower()

    def test_build_extraction_prompt_mentions_entities(self):
        """Prompt should mention extracting entities."""
        prompt = build_extraction_prompt("anything")
        assert "entities" in prompt.lower()

    def test_build_extraction_prompt_is_concise(self):
        """Prompt should be reasonably short (schema enforced via Ollama format)."""
        prompt = build_extraction_prompt("test content")
        # Prompt + content should be under 500 chars for simple content
        assert len(prompt) < 500


# ============== Unit Tests: Response Parsing ==============


class TestParseExtractionResponse:
    def test_parse_valid_json_array(self):
        raw = json.dumps(
            [
                {
                    "fact_text": "Philip uses Neovim",
                    "category": "preference",
                    "entities": ["Philip", "Neovim"],
                    "confidence": 0.9,
                }
            ]
        )
        result = parse_extraction_response(raw)
        assert len(result) == 1
        assert result[0]["fact_text"] == "Philip uses Neovim"

    def test_parse_json_with_markdown_fences(self):
        raw = '```json\n[{"fact_text": "test fact", "category": "personal", "entities": [], "confidence": 0.8}]\n```'
        result = parse_extraction_response(raw)
        assert len(result) == 1
        assert result[0]["fact_text"] == "test fact"

    def test_parse_empty_response(self):
        result = parse_extraction_response("")
        assert result == []

    def test_parse_single_object_wrapped_in_array(self):
        raw = json.dumps(
            {"fact_text": "single fact", "category": "personal", "entities": [], "confidence": 0.9}
        )
        result = parse_extraction_response(raw)
        assert len(result) == 1
        assert result[0]["fact_text"] == "single fact"

    def test_parse_filters_invalid_facts(self):
        raw = json.dumps(
            [
                {
                    "fact_text": "valid fact",
                    "category": "personal",
                    "entities": [],
                    "confidence": 0.9,
                },
                {"category": "personal", "entities": [], "confidence": 0.9},  # missing fact_text
                {
                    "fact_text": "",
                    "category": "personal",
                    "entities": [],
                    "confidence": 0.9,
                },  # empty fact_text
            ]
        )
        result = parse_extraction_response(raw)
        assert len(result) == 1

    def test_parse_clamps_confidence(self):
        raw = json.dumps(
            [
                {"fact_text": "over", "category": "personal", "entities": [], "confidence": 1.5},
                {"fact_text": "under", "category": "personal", "entities": [], "confidence": -0.3},
            ]
        )
        result = parse_extraction_response(raw)
        assert result[0]["confidence"] == 1.0
        assert result[1]["confidence"] == 0.0

    def test_parse_normalizes_category(self):
        raw = json.dumps(
            [{"fact_text": "test", "category": "PERSONAL", "entities": [], "confidence": 0.8}]
        )
        result = parse_extraction_response(raw)
        assert result[0]["category"] == "personal"


# ============== Unit Tests: Extract Facts (Mocked LLM) ==============


class TestExtractFacts:
    @pytest.mark.asyncio
    async def test_extract_facts_calls_llm(self):
        mock_response = json.dumps(
            [
                {
                    "fact_text": "Philip uses Neovim",
                    "category": "preference",
                    "entities": ["Philip"],
                    "confidence": 0.9,
                }
            ]
        )
        with patch("fact_extraction.llm_client") as mock_llm:
            mock_llm.generate = AsyncMock(return_value=mock_response)
            result = await extract_facts("Philip uses Neovim for editing.")
            assert len(result) == 1
            assert result[0]["fact_text"] == "Philip uses Neovim"
            mock_llm.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_extract_facts_handles_llm_error(self):
        with patch("fact_extraction.llm_client") as mock_llm:
            mock_llm.generate = AsyncMock(side_effect=Exception("LLM unavailable"))
            result = await extract_facts("some content")
            assert result == []


# ============== Unit Tests: Store & Search (Real DB) ==============


class TestStoreFact:
    @pytest.mark.asyncio
    async def test_store_fact_returns_id(self, test_prefix):
        fact = {
            "fact_text": f"{test_prefix} Philip uses Neovim",
            "category": "preference",
            "entities": ["Philip", "Neovim"],
            "confidence": 0.9,
        }
        fact_id = await store_fact(fact, f"{test_prefix} source content", "conversation")
        assert isinstance(fact_id, int)
        assert fact_id > 0

    @pytest.mark.asyncio
    async def test_store_fact_generates_embedding(self, test_prefix, db_conn):
        fact = {
            "fact_text": f"{test_prefix} Philip uses Neovim",
            "category": "preference",
            "entities": ["Philip", "Neovim"],
            "confidence": 0.9,
        }
        fact_id = await store_fact(fact, f"{test_prefix} source", "conversation")

        cur = db_conn.cursor()
        cur.execute("SELECT embedding FROM memory_facts WHERE id = %s", (fact_id,))
        row = cur.fetchone()
        assert row[0] is not None

    @pytest.mark.asyncio
    async def test_store_fact_preserves_metadata(self, test_prefix, db_conn):
        from psycopg2.extras import RealDictCursor

        fact = {
            "fact_text": f"{test_prefix} Philip prefers dark mode",
            "category": "preference",
            "entities": ["Philip"],
            "confidence": 0.85,
        }
        fact_id = await store_fact(fact, f"{test_prefix} source", "conversation")

        cur = db_conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT category, entities, confidence FROM memory_facts WHERE id = %s", (fact_id,)
        )
        row = cur.fetchone()
        assert row["category"] == "preference"
        assert "Philip" in row["entities"]
        assert abs(row["confidence"] - 0.85) < 0.01


class TestSearchFacts:
    @pytest.mark.asyncio
    async def test_search_finds_stored_fact(self, test_prefix):
        fact = {
            "fact_text": f"{test_prefix} The DGX Spark has 128GB unified memory",
            "category": "technical",
            "entities": ["DGX Spark"],
            "confidence": 0.95,
        }
        await store_fact(fact, f"{test_prefix} source", "conversation")

        results = await search_facts(f"{test_prefix} DGX Spark memory", limit=5)
        assert len(results) >= 1
        assert any(test_prefix in r["fact_text"] for r in results)


# ============== Integration Tests (Real LLM) ==============


@pytest.mark.slow
class TestIntegrationFactExtraction:
    @pytest.mark.asyncio
    async def test_extract_facts_from_conversation(self, sample_content):
        facts = await extract_facts(sample_content["multi_fact"])
        assert len(facts) >= 2
        for f in facts:
            assert "fact_text" in f
            assert "category" in f

    @pytest.mark.asyncio
    async def test_extract_facts_from_email(self, sample_content):
        facts = await extract_facts(sample_content["email"])
        assert len(facts) >= 1
        # Should identify entities like Samantha, Philip, Lucia's
        all_entities = []
        for f in facts:
            all_entities.extend(f.get("entities", []))
        assert len(all_entities) >= 1

    @pytest.mark.asyncio
    async def test_extract_and_store_roundtrip(self, test_prefix, sample_content):
        content = f"{test_prefix} {sample_content['technical']}"
        facts = await extract_facts(content)
        assert len(facts) >= 1

        stored_ids = []
        for f in facts:
            f["fact_text"] = f"{test_prefix} {f['fact_text']}"
            fact_id = await store_fact(f, content, "conversation")
            stored_ids.append(fact_id)

        results = await search_facts(f"{test_prefix} DGX Spark memory system", limit=10)
        assert len(results) >= 1
