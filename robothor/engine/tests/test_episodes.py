"""Tests for episodic memory (robothor.memory.episodes)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robothor.memory.episodes import _cluster_facts, _jaccard


def _fact(fid: int, minutes_offset: int, entities: list[str], text: str = "x") -> dict:
    """Build a fact dict suitable for _cluster_facts."""
    base = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)
    return {
        "id": fid,
        "fact_text": text,
        "entities": entities,
        "created_at": base + timedelta(minutes=minutes_offset),
        "source_type": "conversation",
        "category": "personal",
    }


class TestJaccard:
    def test_identical_sets(self):
        assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint_sets(self):
        assert _jaccard({"a"}, {"b"}) == 0.0

    def test_partial_overlap(self):
        # intersect=1, union=3 → 0.333...
        assert abs(_jaccard({"a", "b"}, {"b", "c"}) - 1 / 3) < 1e-6

    def test_empty_sets_treated_as_identical(self):
        assert _jaccard(set(), set()) == 1.0


class TestClusterFacts:
    def test_empty_returns_empty(self):
        assert _cluster_facts([]) == []

    def test_single_fact_drops_below_minimum(self):
        """One fact cannot form an episode (min=2)."""
        result = _cluster_facts([_fact(1, 0, ["a"])])
        assert result == []

    def test_close_time_same_entity_clusters_together(self):
        facts = [
            _fact(1, 0, ["sarah", "meeting"]),
            _fact(2, 30, ["sarah", "meeting"]),
            _fact(3, 60, ["sarah", "report"]),
        ]
        clusters = _cluster_facts(facts)
        assert len(clusters) == 1
        assert [f["id"] for f in clusters[0]] == [1, 2, 3]

    def test_temporal_break_splits(self):
        """Gap > 6h breaks the cluster even with shared entities."""
        facts = [
            _fact(1, 0, ["sarah"]),
            _fact(2, 30, ["sarah"]),
            _fact(3, 8 * 60, ["sarah"]),  # 8h later
            _fact(4, 8 * 60 + 30, ["sarah"]),
        ]
        clusters = _cluster_facts(facts)
        assert len(clusters) == 2
        assert [f["id"] for f in clusters[0]] == [1, 2]
        assert [f["id"] for f in clusters[1]] == [3, 4]

    def test_entity_drift_splits(self):
        """Within the temporal window, very different entities split."""
        facts = [
            _fact(1, 0, ["sarah", "meeting"]),
            _fact(2, 30, ["sarah", "meeting"]),
            _fact(3, 60, ["dan", "release"]),  # Disjoint entities
            _fact(4, 90, ["dan", "release"]),
        ]
        clusters = _cluster_facts(facts)
        assert len(clusters) == 2

    def test_sub_minimum_clusters_dropped(self):
        """Clusters smaller than _MIN_EPISODE_FACTS are dropped."""
        facts = [
            _fact(1, 0, ["sarah"]),
            _fact(2, 30, ["sarah"]),
            _fact(3, 8 * 60, ["dan"]),  # lone fact after temporal break
        ]
        clusters = _cluster_facts(facts)
        assert len(clusters) == 1
        assert [f["id"] for f in clusters[0]] == [1, 2]


class TestSearchEpisodesFallback:
    """search_episodes must degrade gracefully when embedding fails."""

    @pytest.mark.asyncio
    async def test_returns_empty_on_embedding_failure(self, monkeypatch):
        from robothor.memory import episodes

        async def _boom(*args, **kwargs):
            raise RuntimeError("ollama down")

        monkeypatch.setattr("robothor.memory.episodes.llm_client.get_embedding_async", _boom)

        result = await episodes.search_episodes("any query", limit=3, tenant_id="test")
        assert result == []
