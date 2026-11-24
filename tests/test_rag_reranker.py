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
            return 1.0

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
            return 0.0

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
            return 1.0

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


# ── Graded relevance (memory recall ranking) ────────────────────────────────
# A single yes/no token gave the blend nothing to work with, and the old code
# then re-sorted each verdict group by cosine similarity — so a hit found only
# by the keyword leg (no `similarity`) always sorted last. The reranker now
# produces a 0..1 score per candidate and orders by that score alone.


def _generate_body(yes: float | None, no: float | None, text: str = "yes") -> dict:
    top = []
    if yes is not None:
        top.append({"token": "yes", "logprob": yes})
    if no is not None:
        top.append({"token": "no", "logprob": no})
    return {"response": text, "logprobs": [{"token": text, "logprob": 0.0, "top_logprobs": top}]}


class _FakePostClient:
    def __init__(self, body):
        self.body = body
        self.payloads = []

    async def post(self, url, json):
        self.payloads.append(json)
        body = self.body

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return body

        return _Resp()


class TestGradedScore:
    @pytest.mark.asyncio
    async def test_pair_score_is_graded_from_yes_no_logprobs(self):
        likely = _FakePostClient(_generate_body(yes=-0.2, no=-1.8))
        unlikely = _FakePostClient(_generate_body(yes=-2.5, no=-0.1, text="no"))

        hi = await reranker.rerank_pair(likely, "q", "d")
        lo = await reranker.rerank_pair(unlikely, "q", "d")

        assert isinstance(hi, float) and isinstance(lo, float)
        assert 0.0 <= lo < 0.5 < hi <= 1.0
        assert likely.payloads[0].get("logprobs") is True, "the request must ask for logprobs"

    @pytest.mark.asyncio
    async def test_pair_score_falls_back_to_the_verdict_without_logprobs(self):
        """An Ollama older than logprobs support answers with text only."""
        yes = await reranker.rerank_pair(_FakePostClient({"response": "yes"}), "q", "d")
        no = await reranker.rerank_pair(_FakePostClient({"response": "no"}), "q", "d")
        assert yes == 1.0
        assert no == 0.0

    @pytest.mark.asyncio
    async def test_missing_opposite_token_is_bounded_not_zero(self):
        """Measured on the local Qwen3-Reranker-0.6B: in 15 of 40 pairs only one
        of "yes"/"no" appears in the top logprobs, which scored a flat 1.0 or 0.0
        and erased the grade exactly where it separates candidates. The absent
        token is bounded by the lowest listed logprob instead."""
        confident_yes = {
            "response": "yes",
            "logprobs": [
                {
                    "token": "yes",
                    "top_logprobs": [
                        {"token": "yes", "logprob": -0.1},
                        {"token": "Yes", "logprob": -2.4},
                        {"token": "true", "logprob": -6.0},
                    ],
                }
            ],
        }
        confident_no = {
            "response": "no",
            "logprobs": [
                {
                    "token": "no",
                    "top_logprobs": [
                        {"token": "no", "logprob": -0.05},
                        {"token": "No", "logprob": -3.0},
                        {"token": "false", "logprob": -8.0},
                    ],
                }
            ],
        }
        hi = await reranker.rerank_pair(_FakePostClient(confident_yes), "q", "d")
        lo = await reranker.rerank_pair(_FakePostClient(confident_no), "q", "d")
        assert 0.99 < hi < 1.0
        assert 0.0 < lo < 0.01

    @pytest.mark.asyncio
    async def test_a_more_certain_no_scores_lower(self):
        def body(no_lp: float, floor: float) -> dict:
            return {
                "response": "no",
                "logprobs": [
                    {
                        "token": "no",
                        "top_logprobs": [
                            {"token": "no", "logprob": no_lp},
                            {"token": "other", "logprob": floor},
                        ],
                    }
                ],
            }

        certain = await reranker.rerank_pair(_FakePostClient(body(-0.01, -12.0)), "q", "d")
        unsure = await reranker.rerank_pair(_FakePostClient(body(-0.3, -5.0)), "q", "d")
        assert certain < unsure

    @pytest.mark.asyncio
    async def test_request_asks_for_the_widest_top_logprobs(self):
        client = _FakePostClient(_generate_body(yes=-0.2, no=-1.8))
        await reranker.rerank_pair(client, "q", "d")
        assert client.payloads[0]["top_logprobs"] == 20

    def test_default_instruction_is_about_memory_not_web_search(self):
        prompt = build_reranker_prompt("q", "d")
        assert "web search" not in prompt.lower()
        assert "memor" in prompt.lower()

    def test_candidate_cap_is_a_named_constant(self, monkeypatch):
        monkeypatch.delenv("MEMORY_RERANK_MAX_CANDIDATES", raising=False)
        assert reranker.RERANK_MAX_CANDIDATES == 16
        assert reranker._max_candidates() == reranker.RERANK_MAX_CANDIDATES


class TestRerankOrdering:
    @pytest.mark.asyncio
    async def test_orders_by_score_never_by_cosine(self, monkeypatch):
        scores = {"low-cos best": 0.95, "high-cos worst": 0.10, "mid": 0.5}

        async def fake_pair(client, query, doc):
            return scores[doc]

        monkeypatch.setattr(reranker, "rerank_pair", fake_pair)
        monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
        results = [
            {"content": "high-cos worst", "similarity": 0.99},
            {"content": "mid", "similarity": 0.5},
            {"content": "low-cos best", "similarity": 0.01},
        ]
        out = await reranker.rerank("q", results, top_k=3)
        assert [r["content"] for r in out] == ["low-cos best", "mid", "high-cos worst"]
        assert [r["rerank_score"] for r in out] == [0.95, 0.5, 0.10]

    @pytest.mark.asyncio
    async def test_keyword_only_hit_is_scored_like_any_other(self, monkeypatch):
        """No `similarity` key must not mean 'sort last'."""

        async def fake_pair(client, query, doc):
            return 0.9 if doc == "keyword only" else 0.3

        monkeypatch.setattr(reranker, "rerank_pair", fake_pair)
        monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
        results = [{"content": f"dense {i}", "similarity": 0.9 - i / 100} for i in range(5)]
        results.append({"content": "keyword only"})
        out = await reranker.rerank("q", results, top_k=3)
        assert out[0]["content"] == "keyword only"

    @pytest.mark.asyncio
    async def test_equal_scores_keep_the_fused_order(self, monkeypatch):
        async def fake_pair(client, query, doc):
            return 0.5

        monkeypatch.setattr(reranker, "rerank_pair", fake_pair)
        monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
        results = [{"content": f"d{i}", "similarity": i / 10} for i in range(4)]
        out = await reranker.rerank("q", results, top_k=4)
        assert [r["content"] for r in out] == ["d0", "d1", "d2", "d3"]

    @pytest.mark.asyncio
    async def test_a_failed_pair_falls_back_to_fused_order(self, monkeypatch):
        async def fake_pair(client, query, doc):
            if doc == "d2":
                raise RuntimeError("reranker down")
            return 0.9

        monkeypatch.setattr(reranker, "rerank_pair", fake_pair)
        monkeypatch.setattr(reranker, "check_reranker_available", AsyncMock(return_value=True))
        results = [{"content": f"d{i}", "similarity": 1 - i / 10} for i in range(4)]
        out = await reranker.rerank_with_fallback("q", [dict(r) for r in results], top_k=3)
        assert [r["content"] for r in out] == ["d0", "d1", "d2"]
        assert all(r.get("rerank_score") is None for r in out)
