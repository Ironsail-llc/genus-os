"""Tests for supersession-aware temporal recall (R1 / #44).

The weak link: `_blend_rank` is a linear blend dominated by relevance, with no
supersession signal — so when two `decision` facts about the same topic are
co-ingested (identical created_at/age), the older one can out-rank the newer
"current" decision on relevance alone. These tests pin the fix:

- With MEMORY_TEMPORAL_COHERENCE on, the later same-topic decision wins
  (read-time supersession inference via insertion order, since co-ingested
  facts share created_at — only `id` distinguishes them).
- With the flag off, ranking is unchanged (relevance order preserved).
- A fact explicitly marked superseded_by is demoted regardless.
"""

from __future__ import annotations

from robothor.memory.facts import _blend_rank

# Mirrors the eval case `temporal-storage-decision`: two co-ingested decisions
# sharing {Alice, Helios}; the older (Postgres) has HIGHER similarity.
_OLDER = {
    "fact_text": "Alice decided to use Postgres for Helios storage.",
    "similarity": 0.845,
    "entities": ["Alice", "Helios", "Postgres"],
    "id": 1,
    "category": "decision",
    "importance_score": 0.5,
    "age_seconds": 0.23,
    "access_count": 0,
    "created_at": None,
    "superseded_by": None,
}
_NEWER = {
    "fact_text": "Alice later decided to switch Helios storage to SQLite.",
    "similarity": 0.823,
    "entities": ["Alice", "Helios", "SQLite"],
    "id": 2,
    "category": "decision",
    "importance_score": 0.5,
    "age_seconds": 0.23,
    "access_count": 0,
    "created_at": None,
    "superseded_by": None,
}


def _clone(d):
    return dict(d)


def test_coherence_on_prefers_later_same_topic_decision(monkeypatch):
    monkeypatch.setenv("MEMORY_TEMPORAL_COHERENCE", "1")
    ranked = _blend_rank([_clone(_OLDER), _clone(_NEWER)], limit=5)
    assert "SQLite" in ranked[0]["fact_text"], (
        "later same-topic decision must rank first with coherence on"
    )


def test_coherence_off_preserves_relevance_order(monkeypatch):
    monkeypatch.setenv("MEMORY_TEMPORAL_COHERENCE", "0")
    ranked = _blend_rank([_clone(_OLDER), _clone(_NEWER)], limit=5)
    # Unchanged behavior: higher-similarity (older) fact stays on top.
    assert "Postgres" in ranked[0]["fact_text"]


def test_explicit_superseded_is_demoted(monkeypatch):
    monkeypatch.setenv("MEMORY_TEMPORAL_COHERENCE", "1")
    older = _clone(_OLDER)
    older["superseded_by"] = 99  # conflict resolution already marked it stale
    ranked = _blend_rank([older, _clone(_NEWER)], limit=5)
    assert "SQLite" in ranked[0]["fact_text"]


def test_unrelated_decisions_not_cross_superseded(monkeypatch):
    """Two decisions sharing only a person (1 entity) are different topics —
    coherence must NOT treat the higher-id one as superseding the other."""
    monkeypatch.setenv("MEMORY_TEMPORAL_COHERENCE", "1")
    hire = {
        "fact_text": "Alice decided to hire Bob as security lead.",
        "similarity": 0.90,
        "entities": ["Alice", "Bob"],
        "id": 1,
        "category": "decision",
        "importance_score": 0.5,
        "age_seconds": 0.23,
        "access_count": 0,
        "created_at": None,
        "superseded_by": None,
    }
    storage = _clone(_NEWER)  # shares only {Alice} with `hire`
    storage["id"] = 2
    ranked = _blend_rank([hire, storage], limit=5)
    # `hire` (higher relevance, different topic) must NOT be demoted as superseded.
    assert "hire Bob" in ranked[0]["fact_text"]
