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
