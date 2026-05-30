"""Tests for `robothor.memory.fusion.rrf_fuse`."""

from __future__ import annotations

from robothor.memory.fusion import rrf_fuse


class TestRRFFuse:
    def test_item_in_both_lists_ranks_first(self) -> None:
        a = [{"source": "fact", "id": 1}, {"source": "fact", "id": 2}]
        b = [{"source": "fact", "id": 2}, {"source": "fact", "id": 3}]
        fused = rrf_fuse([a, b])
        # id=2 appears in both → highest summed RRF score
        assert fused[0]["id"] == 2
        assert {r["id"] for r in fused} == {1, 2, 3}

    def test_annotates_rrf_score(self) -> None:
        fused = rrf_fuse([[{"source": "fact", "id": 1}]])
        assert "rrf_score" in fused[0]
        assert fused[0]["rrf_score"] > 0

    def test_same_id_different_source_do_not_collide(self) -> None:
        a = [{"source": "fact", "id": 5}]
        b = [{"source": "intent", "id": 5}]
        fused = rrf_fuse([a, b])
        assert len(fused) == 2
        assert {r["source"] for r in fused} == {"fact", "intent"}

    def test_anon_items_kept_distinct(self) -> None:
        a = [{"text": "x"}, {"text": "y"}]  # no source/id
        fused = rrf_fuse([a])
        assert len(fused) == 2

    def test_empty_input(self) -> None:
        assert rrf_fuse([]) == []
        assert rrf_fuse([[]]) == []
