"""Tests for robothor.rag.reranker — prompt building and structure (pure unit tests)."""

from unittest.mock import AsyncMock

import pytest

from robothor.rag import reranker
from robothor.rag.reranker import RERANKER_MODEL, build_reranker_prompt


class TestBuildRerankerPrompt:
    def test_contains_query(self):
        prompt = build_reranker_prompt("test query", "test document")
        assert "test query" in prompt

    def test_contains_document(self):
        prompt = build_reranker_prompt("query", "document text here")
        assert "document text here" in prompt

    def test_chatml_format(self):
        prompt = build_reranker_prompt("q", "d")
        assert "<|im_start|>system" in prompt
        assert "<|im_end|>" in prompt
        assert "<|im_start|>user" in prompt
        assert "<|im_start|>assistant" in prompt

    def test_think_tags_prefilled(self):
        """Pre-filled think tags skip reasoning for direct yes/no output."""
        prompt = build_reranker_prompt("q", "d")
        assert "<think>\n\n</think>" in prompt

    def test_yes_no_instruction(self):
        prompt = build_reranker_prompt("q", "d")
        assert '"yes"' in prompt or "yes" in prompt.lower()
        assert '"no"' in prompt or "no" in prompt.lower()

    def test_document_truncated(self):
        long_doc = "x" * 5000
        prompt = build_reranker_prompt("q", long_doc)
        # Document should be truncated to 3000 chars
        assert "x" * 3001 not in prompt

    def test_custom_instruction(self):
        prompt = build_reranker_prompt("q", "d", instruction="Custom instruction")
        assert "Custom instruction" in prompt


class TestRerankerModel:
    def test_model_is_string(self):
        assert isinstance(RERANKER_MODEL, str)

    def test_model_has_reranker(self):
        assert "reranker" in RERANKER_MODEL.lower() or "Reranker" in RERANKER_MODEL


# ── Latency work (2026-08-24) ────────────────────────────────────────────────
# The competitive sweep measured search_facts at warm p50 1,314ms — and a
# per-component breakdown put 1,215ms of it in this module: one Ollama
# generate call per candidate (37ms/pair), EVERY fused candidate scored
# (30-60 of them), fully serialized server-side (OLLAMA_NUM_PARALLEL=1), plus
# an /api/tags availability probe on every single search. Embedding was 35ms,
# the vector leg 9ms, BM25 2ms: the reranker was 97% of the flagship
# feature's latency.


class TestCandidateCap:
    """Scoring 60 candidates buys nothing: the pool arrives RRF-ordered, so
    pairs past the cap were already ranked out by both retrieval legs."""

    @pytest.mark.asyncio
    async def test_only_the_cap_is_scored(self, monkeypatch):
        scored = []

        async def fake_pair(client, query, doc):
            scored.append(doc)
            return "yes"

        monkeypatch.setattr(reranker, "rerank_pair", fake_pair)
        monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
        monkeypatch.setenv("MEMORY_RERANK_MAX_CANDIDATES", "16")

        results = [{"content": f"doc{i}", "similarity": 1 - i / 100} for i in range(60)]
        out = await reranker.rerank("q", results, top_k=10)

        assert len(scored) == 16, f"scored {len(scored)} pairs, cap is 16"
        assert len(out) == 10

    @pytest.mark.asyncio
    async def test_cap_keeps_rrf_order_head(self, monkeypatch):
        """The cap must take the FIRST N (RRF-best), never a random slice."""
        scored = []

        async def fake_pair(client, query, doc):
            scored.append(doc)
            return "no"

        monkeypatch.setattr(reranker, "rerank_pair", fake_pair)
        monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
        monkeypatch.setenv("MEMORY_RERANK_MAX_CANDIDATES", "4")

        results = [{"content": f"doc{i}", "similarity": 0.5} for i in range(10)]
        await reranker.rerank("q", results, top_k=3)
        assert scored == ["doc0", "doc1", "doc2", "doc3"]

    @pytest.mark.asyncio
    async def test_cap_zero_disables_the_cap(self, monkeypatch):
        scored = []

        async def fake_pair(client, query, doc):
            scored.append(doc)
            return "yes"

        monkeypatch.setattr(reranker, "rerank_pair", fake_pair)
        monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
        monkeypatch.setenv("MEMORY_RERANK_MAX_CANDIDATES", "0")

        results = [{"content": f"doc{i}", "similarity": 0.5} for i in range(40)]
        await reranker.rerank("q", results, top_k=5)
        assert len(scored) == 40


class TestAvailabilityCache:
    """An /api/tags roundtrip per search is pure tax — the reranker's
    availability does not change between searches milliseconds apart."""

    @pytest.mark.asyncio
    async def test_availability_is_cached_within_ttl(self, monkeypatch):
        calls = []

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"models": [{"name": reranker.RERANKER_MODEL}]}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                calls.append(url)
                return FakeResp()

        monkeypatch.setattr(reranker.httpx, "AsyncClient", FakeClient)
        reranker._availability_cache_clear()

        assert await reranker.check_reranker_available() is True
        assert await reranker.check_reranker_available() is True
        assert len(calls) == 1, "second check within the TTL must not re-probe"

    @pytest.mark.asyncio
    async def test_availability_reprobes_after_ttl(self, monkeypatch):
        calls = []

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"models": [{"name": reranker.RERANKER_MODEL}]}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                calls.append(url)
                return FakeResp()

        monkeypatch.setattr(reranker.httpx, "AsyncClient", FakeClient)
        reranker._availability_cache_clear()

        await reranker.check_reranker_available()
        reranker._availability_cache_age_for_tests(seconds=999)
        await reranker.check_reranker_available()
        assert len(calls) == 2
