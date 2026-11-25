"""Tests for the WS-1 recall-ranking fixes in `robothor.memory.facts`.

Pure-function coverage (no DB / no Ollama): the wide-rerank flag plumbing and
the cross-encoder-verdict soft boost inside `_blend_rank`. The end-to-end
candidate-pool / episode-merge behaviour is exercised by the live eval suite.
"""

from __future__ import annotations

from typing import Any

from robothor.memory.facts import (
    _blend_rank,
    _episode_merge_enabled,
    _rerank_wide_enabled,
)


def _row(
    fid: int, text: str, sim: float, verdict: str, *, age: float = 3600.0, imp: float = 0.5
) -> dict[str, Any]:
    return {
        "id": fid,
        "fact_text": text,
        "similarity": sim,
        "rerank_relevant": verdict,
        "age_seconds": age,
        "importance_score": imp,
        "access_count": 0,
        "category": "event",
        "entities": [],
        "superseded_by": None,
    }


class TestFlags:
    def test_rerank_wide_flag(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_RERANK_WIDE", "1")
        assert _rerank_wide_enabled() is True
        monkeypatch.setenv("MEMORY_RERANK_WIDE", "off")
        assert _rerank_wide_enabled() is False
        monkeypatch.delenv("MEMORY_RERANK_WIDE", raising=False)
        assert _rerank_wide_enabled() is False  # default OFF

    def test_episode_merge_flag(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_EPISODE_MERGE", "yes")
        assert _episode_merge_enabled() is True
        monkeypatch.delenv("MEMORY_EPISODE_MERGE", raising=False)
        assert _episode_merge_enabled() is False  # default OFF


class TestVerdictBoost:
    """A lower-cosine 'yes' fact must overtake a higher-cosine 'no' fact ONLY
    when MEMORY_RERANK_WIDE is on — this is the operator's fresh-fact rescue."""

    def test_yes_rescues_lower_cosine_when_wide_on(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("MEMORY_RERANK_WIDE", "1")
        monkeypatch.delenv("MEMORY_TEMPORAL_COHERENCE", raising=False)
        rows = [_row(1, "alpha apple", 0.74, "no"), _row(2, "bravo banana", 0.70, "yes")]
        out = _blend_rank(rows, limit=5)
        assert out[0]["id"] == 2  # +0.10 verdict bonus overcomes the 0.04 cosine gap

    def test_no_rescue_when_wide_off(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MEMORY_RERANK_WIDE", raising=False)
        monkeypatch.delenv("MEMORY_TEMPORAL_COHERENCE", raising=False)
        rows = [_row(1, "alpha apple", 0.74, "no"), _row(2, "bravo banana", 0.70, "yes")]
        out = _blend_rank(rows, limit=5)
        assert out[0]["id"] == 1  # higher cosine wins; verdict ignored

    def test_verdict_absent_is_harmless(self, monkeypatch: Any) -> None:
        # Rows without a rerank_relevant key (non-reranked path) must not crash.
        monkeypatch.setenv("MEMORY_RERANK_WIDE", "1")
        rows = [_row(1, "alpha apple", 0.74, "no"), _row(2, "bravo banana", 0.70, "no")]
        for r in rows:
            del r["rerank_relevant"]
        out = _blend_rank(rows, limit=5)
        assert out[0]["id"] == 1


def _plain(fid: int, *, sim: float | None, rank: int | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": fid,
        "fact_text": f"distinct fact number {fid} about topic {fid * 7}",
        "age_seconds": 3600.0,
        "importance_score": 0.5,
        "access_count": 0,
        "category": "event",
        "entities": [],
        "superseded_by": None,
    }
    if sim is not None:
        row["similarity"] = sim
    if rank is not None:
        row["fused_rank"] = rank
    return row


class TestSingleRelevanceScale:
    """Relevance is one 0..1 scale: the rerank score when there is one, else a
    function of the fused rank. Cosine similarity never enters the blend, so a
    dense hit and a keyword-only hit are compared on the same footing."""

    def test_cosine_does_not_outweigh_fused_rank(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MEMORY_RERANK_WIDE", raising=False)
        monkeypatch.delenv("MEMORY_TEMPORAL_COHERENCE", raising=False)
        # Fused rank 0 is a dense hit with a low cosine; rank 1 is keyword-only.
        rows = [_plain(1, sim=0.30, rank=0), _plain(2, sim=None, rank=1)]
        out = _blend_rank(rows, limit=5)
        assert out[0]["id"] == 1, "the fused order, not the cosine, sets relevance"

    def test_rerank_score_is_the_relevance(self, monkeypatch: Any) -> None:
        monkeypatch.delenv("MEMORY_RERANK_WIDE", raising=False)
        monkeypatch.delenv("MEMORY_TEMPORAL_COHERENCE", raising=False)
        a = _plain(1, sim=0.90, rank=0)
        a["rerank_score"] = 0.20
        b = _plain(2, sim=None, rank=1)
        b["rerank_score"] = 0.95
        out = _blend_rank([a, b], limit=5)
        assert out[0]["id"] == 2
        assert out[0]["relevance"] == 0.95

    def test_similarity_present_or_absent_gives_the_same_relevance(self) -> None:
        dense = _plain(1, sim=0.95, rank=3)
        keyword_only = _plain(2, sim=None, rank=3)
        _blend_rank([dense, keyword_only], limit=5)
        assert dense["relevance"] == keyword_only["relevance"]
        assert 0.0 < dense["relevance"] <= 1.0
