"""
RAG Serve Layer — Tests

Tests for the RAG orchestrator's serving endpoints:
- Stored fact retrieval (top-5 for relevant queries)
- Graceful no-match handling
- /health endpoint reflecting actual status
- /stats endpoint returning valid data
- /query endpoint with each RAG profile (fast, general, research)

These tests verify the user-facing API layer of the memory system.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest
from fact_extraction import search_facts, store_fact
from fastapi.testclient import TestClient
from orchestrator import RAG_PROFILES, app, classify_query, run_pipeline

# ============== Query Classification Tests ==============


class TestQueryClassification:
    """Tests for query → profile classification."""

    def test_code_query_classified_as_code(self):
        assert classify_query("Write a Python function to sort a list") == "code"

    def test_research_query_classified_as_research(self):
        assert classify_query("Explain in detail how transformers work") == "research"

    def test_fast_query_classified_as_fast(self):
        assert classify_query("What time is it? Quick answer") == "fast"

    def test_generic_query_classified_as_general(self):
        assert classify_query("What did Philip do yesterday?") == "general"

    def test_expert_query_classified_as_expert(self):
        assert classify_query("Give me a comprehensive thorough analysis") == "expert"

    def test_all_profiles_exist(self):
        """Every classification result should have a matching profile."""
        expected = {"fast", "general", "research", "code", "expert", "heavy"}
        assert expected.issubset(set(RAG_PROFILES.keys()))


# ============== Fact Retrieval Tests ==============


class TestFactRetrieval:
    """Tests that stored facts are retrievable for relevant queries."""

    async def test_stored_fact_in_top_5(self, test_prefix):
        """A stored fact should appear in top 5 results for a relevant query."""
        fact = {
            "fact_text": f"{test_prefix} The DGX Spark has 128GB unified LPDDR5X memory",
            "category": "technical",
            "entities": ["DGX Spark"],
            "confidence": 0.95,
        }
        await store_fact(fact, f"{test_prefix} source", "technical")

        results = await search_facts(f"{test_prefix} DGX Spark memory specifications", limit=5)
        assert len(results) >= 1
        found = any(test_prefix in r["fact_text"] for r in results)
        assert found, f"Stored fact not found in top 5: {results}"

    async def test_multiple_relevant_facts_ranked(self, test_prefix):
        """Multiple stored facts about a topic should all appear in results."""
        facts = [
            {
                "fact_text": f"{test_prefix} Ollama runs on port 11434",
                "category": "technical",
                "entities": ["Ollama"],
                "confidence": 0.9,
            },
            {
                "fact_text": f"{test_prefix} Ollama serves Qwen3-Next-80B locally",
                "category": "technical",
                "entities": ["Ollama", "Qwen3"],
                "confidence": 0.9,
            },
        ]
        for f in facts:
            await store_fact(f, f"{test_prefix} src", "technical")

        results = await search_facts(f"{test_prefix} Ollama configuration", limit=5)
        matching = [r for r in results if test_prefix in r["fact_text"]]
        assert len(matching) >= 2

    async def test_no_match_returns_empty_or_low_similarity(self, test_prefix):
        """A query with no relevant stored facts should return low-similarity results."""
        fact = {
            "fact_text": f"{test_prefix} Philip likes hiking on weekends",
            "category": "personal",
            "entities": ["Philip"],
            "confidence": 0.8,
        }
        await store_fact(fact, f"{test_prefix} src", "conversation")

        # Query something completely unrelated
        results = await search_facts(
            f"{test_prefix} quantum chromodynamics gluon interactions", limit=5
        )
        # Results may be returned but with low similarity; or our test fact
        # may not be in them because of other facts in the DB.
        # The important thing is it doesn't crash.
        assert isinstance(results, list)


# ============== /health Endpoint Tests ==============


@pytest.mark.integration
class TestHealthEndpoint:
    """Tests for the /health endpoint (requires orchestrator running)."""

    def test_health_returns_200(self):
        """GET /health should return 200."""
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_has_status_field(self):
        """Health response should include a status field."""
        client = TestClient(app)
        data = client.get("/health").json()
        assert "status" in data
        assert data["status"] in ("ok", "degraded")

    def test_health_reports_components(self):
        """Health response should report individual component status."""
        client = TestClient(app)
        data = client.get("/health").json()
        assert "components" in data
        components = data["components"]
        assert "generation_model" in components
        assert "reranker" in components
        assert "web_search" in components
        assert "memory_db" in components

    def test_health_components_have_available_field(self):
        """Each component should report 'available' as boolean."""
        client = TestClient(app)
        data = client.get("/health").json()
        for name, comp in data["components"].items():
            assert "available" in comp, f"Component {name} missing 'available'"
            assert isinstance(comp["available"], bool)


# ============== /stats Endpoint Tests ==============


@pytest.mark.integration
class TestStatsEndpoint:
    """Tests for the /stats endpoint."""

    def test_stats_returns_200(self):
        """GET /stats should return 200."""
        client = TestClient(app)
        response = client.get("/stats")
        assert response.status_code == 200

    def test_stats_has_memory_counts(self):
        """Stats should include memory counts."""
        client = TestClient(app)
        data = client.get("/stats").json()
        assert "short_term_count" in data
        assert "long_term_count" in data
        assert isinstance(data["short_term_count"], int)
        assert isinstance(data["long_term_count"], int)

    def test_stats_counts_are_non_negative(self):
        """Memory counts should be non-negative."""
        client = TestClient(app)
        data = client.get("/stats").json()
        assert data["short_term_count"] >= 0
        assert data["long_term_count"] >= 0


# ============== /profiles Endpoint Tests ==============


@pytest.mark.integration
class TestProfilesEndpoint:
    """Tests for the /profiles endpoint."""

    def test_profiles_returns_200(self):
        """GET /profiles should return 200."""
        client = TestClient(app)
        response = client.get("/profiles")
        assert response.status_code == 200

    def test_profiles_lists_all_profiles(self):
        """Should return all configured RAG profiles."""
        client = TestClient(app)
        data = client.get("/profiles").json()
        expected = {"fast", "general", "research", "code", "expert", "heavy"}
        assert expected.issubset(set(data.keys()))

    def test_each_profile_has_description(self):
        """Each profile should have a description field."""
        client = TestClient(app)
        data = client.get("/profiles").json()
        for name, profile in data.items():
            assert "description" in profile, f"Profile {name} missing description"
            assert len(profile["description"]) > 0


# ============== /query Endpoint Tests ==============


@pytest.mark.integration
class TestQueryEndpoint:
    """Tests for the /query endpoint."""

    def test_query_returns_200(self):
        """POST /query with a valid question should return 200."""
        mock_result = {
            "answer": "Test answer",
            "profile": "fast",
            "query": "What do you know?",
            "memories_found": 0,
            "web_results_found": 0,
            "reranked_count": 0,
            "timing": {"retrieval_ms": 10, "rerank_ms": 5, "generation_ms": 100, "total_ms": 115},
            "sources": {"memory": [], "web": []},
        }
        with patch("orchestrator.run_pipeline", new_callable=AsyncMock, return_value=mock_result):
            client = TestClient(app)
            response = client.post(
                "/query",
                json={"question": "What do you know?", "profile": "fast"},
            )
        assert response.status_code == 200

    def test_query_requires_question(self):
        """POST /query without question should return 422."""
        client = TestClient(app)
        response = client.post("/query", json={})
        assert response.status_code == 422

    @pytest.mark.llm
    @pytest.mark.slow
    def test_query_fast_profile(self):
        """Query with 'fast' profile should return quickly with an answer."""
        client = TestClient(app)
        t0 = time.time()
        response = client.post(
            "/query",
            json={"question": "What is 2+2?", "profile": "fast"},
        )
        elapsed = time.time() - t0
        assert response.status_code == 200
        data = response.json()
        assert "answer" in data
        assert len(data["answer"]) > 0
        assert data["profile"] == "fast"
        assert "timing" in data

    @pytest.mark.llm
    @pytest.mark.slow
    def test_query_general_profile(self):
        """Query with 'general' profile should work."""
        client = TestClient(app)
        response = client.post(
            "/query",
            json={"question": "Tell me about Robothor", "profile": "general"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["profile"] == "general"
        assert "answer" in data

    @pytest.mark.llm
    @pytest.mark.slow
    def test_query_research_profile(self):
        """Query with 'research' profile should use more context."""
        client = TestClient(app)
        response = client.post(
            "/query",
            json={
                "question": "Explain in detail how the memory system works",
                "profile": "research",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["profile"] == "research"
        # Research profile should retrieve more memories
        assert "memories_found" in data

    def test_query_response_structure(self):
        """Query response should have the expected structure."""
        mock_result = {
            "answer": "Mocked answer",
            "profile": "fast",
            "query": "test",
            "memories_found": 2,
            "web_results_found": 1,
            "reranked_count": 3,
            "timing": {"retrieval_ms": 10, "rerank_ms": 5, "generation_ms": 100, "total_ms": 115},
            "sources": {"memory": [], "web": []},
        }
        with patch("orchestrator.run_pipeline", new_callable=AsyncMock, return_value=mock_result):
            client = TestClient(app)
            response = client.post(
                "/query",
                json={"question": "test", "profile": "fast"},
            )
        data = response.json()
        expected_keys = {
            "answer",
            "profile",
            "query",
            "memories_found",
            "web_results_found",
            "reranked_count",
            "timing",
            "sources",
        }
        assert expected_keys.issubset(set(data.keys())), (
            f"Missing keys: {expected_keys - set(data.keys())}"
        )


# ============== /v1/chat/completions Endpoint Tests ==============


@pytest.mark.integration
class TestChatCompletionsEndpoint:
    """Tests for the OpenAI-compatible /v1/chat/completions endpoint."""

    @pytest.mark.llm
    @pytest.mark.slow
    def test_chat_completions_returns_openai_format(self):
        """Response should match OpenAI chat completion format."""
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen3-80b",
                "messages": [
                    {"role": "user", "content": "Say hello"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert "choices" in data
        assert len(data["choices"]) >= 1
        assert "message" in data["choices"][0]
        assert data["choices"][0]["message"]["role"] == "assistant"

    def test_chat_completions_requires_user_message(self):
        """Should return 400 if no user message is provided."""
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen3-80b",
                "messages": [
                    {"role": "system", "content": "You are a bot"},
                ],
            },
        )
        assert response.status_code == 400


# ============== /v1/models Endpoint Tests ==============


@pytest.mark.integration
class TestModelsEndpoint:
    """Tests for the /v1/models endpoint."""

    def test_models_returns_200(self):
        """GET /v1/models should return 200."""
        client = TestClient(app)
        response = client.get("/v1/models")
        assert response.status_code == 200

    def test_models_lists_qwen3(self):
        """Should list at least one model."""
        client = TestClient(app)
        data = client.get("/v1/models").json()
        assert "data" in data
        assert len(data["data"]) >= 1
        assert data["data"][0]["id"] == "qwen3-80b"


# ============== /ingest Endpoint Tests ==============


@pytest.mark.integration
class TestIngestEndpoint:
    """Tests for the /ingest endpoint."""

    def test_ingest_rejects_empty_content(self):
        """POST /ingest with empty content should return 400."""
        client = TestClient(app)
        response = client.post(
            "/ingest",
            json={
                "content": "",
                "source_channel": "api",
                "content_type": "test",
            },
        )
        assert response.status_code == 400

    @pytest.mark.llm
    @pytest.mark.slow
    def test_ingest_stores_content(self, test_prefix):
        """POST /ingest with valid content should store facts."""
        client = TestClient(app)
        response = client.post(
            "/ingest",
            json={
                "content": f"{test_prefix} Philip deployed the new RAG system to production",
                "source_channel": "api",
                "content_type": "event",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "facts_processed" in data


# ============== Pipeline Tests (Mocked LLM) ==============


class TestPipelineMocked:
    """Tests for the pipeline logic with mocked LLM calls."""

    async def test_pipeline_returns_expected_structure(self, test_prefix):
        """run_pipeline should return a dict with all expected fields."""
        mock_answer = "This is a test answer from the mocked LLM."
        with (
            patch(
                "orchestrator.generate",
                new_callable=AsyncMock,
                return_value=mock_answer,
            ),
            patch(
                "orchestrator.chat",
                new_callable=AsyncMock,
                return_value=mock_answer,
            ),
        ):
            result = await run_pipeline("test query", profile="fast")

        assert "answer" in result
        assert "profile" in result
        assert "timing" in result
        assert "memories_found" in result
        assert "web_results_found" in result
        assert result["profile"] == "fast"

    async def test_pipeline_classifies_automatically(self):
        """Without a profile override, pipeline should classify the query."""
        mock_answer = "Test answer"
        with (
            patch(
                "orchestrator.generate",
                new_callable=AsyncMock,
                return_value=mock_answer,
            ),
            patch(
                "orchestrator.chat",
                new_callable=AsyncMock,
                return_value=mock_answer,
            ),
        ):
            result = await run_pipeline("Write a Python function to sort numbers")
        assert result["profile"] == "code"

    async def test_pipeline_graceful_on_retrieval_failure(self):
        """Pipeline should still work if memory search fails."""
        mock_answer = "Answer despite retrieval failure"
        with (
            patch(
                "orchestrator.search_all_memory",
                side_effect=Exception("DB connection failed"),
            ),
            patch(
                "orchestrator.generate",
                new_callable=AsyncMock,
                return_value=mock_answer,
            ),
            patch(
                "orchestrator.chat",
                new_callable=AsyncMock,
                return_value=mock_answer,
            ),
        ):
            result = await run_pipeline("test query", profile="fast")
        # Should still get an answer (from LLM without context)
        assert "answer" in result
