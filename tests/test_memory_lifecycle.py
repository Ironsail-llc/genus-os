"""Tests for robothor.memory.lifecycle — decay scoring (pure unit tests)."""

from datetime import UTC, datetime, timedelta

from robothor.memory.lifecycle import IMPORTANCE_SCHEMA, compute_decay_score


class TestComputeDecayScore:
    def test_recently_accessed(self):
        """Recently accessed memory should have high score."""
        now = datetime.now(UTC)
        score = compute_decay_score(
            last_accessed=now,
            access_count=0,
            reinforcement_count=0,
            importance_score=0.5,
        )
        # Recent + no boosts, recency ~1.0, importance_floor 0.2
        assert score > 0.8

    def test_old_memory_decays(self):
        """Old memory should have lower score."""
        old = datetime.now(UTC) - timedelta(days=30)
        score = compute_decay_score(
            last_accessed=old,
            access_count=0,
            reinforcement_count=0,
            importance_score=0.0,
        )
        assert score < 0.1

    def test_importance_provides_floor(self):
        """Important facts resist decay."""
        old = datetime.now(UTC) - timedelta(days=30)
        score = compute_decay_score(
            last_accessed=old,
            access_count=0,
            reinforcement_count=0,
            importance_score=1.0,
        )
        # importance_floor = 1.0 * 0.4 = 0.4
        assert score >= 0.4

    def test_access_boost(self):
        """Frequently accessed memories score higher."""
        now = datetime.now(UTC) - timedelta(hours=24)
        base_score = compute_decay_score(now, 0, 0, 0.5)
        boosted_score = compute_decay_score(now, 100, 0, 0.5)
        assert boosted_score > base_score

    def test_reinforcement_boost(self):
        """Reinforced memories score higher."""
        now = datetime.now(UTC) - timedelta(hours=24)
        base_score = compute_decay_score(now, 0, 0, 0.5)
        boosted_score = compute_decay_score(now, 0, 50, 0.5)
        assert boosted_score > base_score

    def test_score_clamped_to_zero_one(self):
        """Score never exceeds 1.0 or goes below 0.0."""
        now = datetime.now(UTC)
        score = compute_decay_score(now, 1000, 1000, 1.0)
        assert 0.0 <= score <= 1.0

        old = datetime.now(UTC) - timedelta(days=365)
        score = compute_decay_score(old, 0, 0, 0.0)
        assert 0.0 <= score <= 1.0

    def test_naive_datetime_handled(self):
        """Naive datetimes are treated as UTC."""
        naive = datetime.now() - timedelta(hours=1)
        # Should not raise
        score = compute_decay_score(naive, 0, 0, 0.5)
        assert 0.0 <= score <= 1.0

    def test_half_life_72_hours(self):
        """At 72 hours, recency should be approximately 0.5."""
        three_days_ago = datetime.now(UTC) - timedelta(hours=72)
        score = compute_decay_score(three_days_ago, 0, 0, 0.0)
        # Recency at half-life = 0.5, plus no boosts
        assert abs(score - 0.5) < 0.05


class TestImportanceSchema:
    def test_schema_structure(self):
        assert IMPORTANCE_SCHEMA["type"] == "object"
        assert "score" in IMPORTANCE_SCHEMA["properties"]
        assert "score" in IMPORTANCE_SCHEMA["required"]
