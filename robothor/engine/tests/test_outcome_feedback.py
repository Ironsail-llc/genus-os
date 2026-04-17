"""Tests for outcome-driven fact invalidation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from robothor.memory.lifecycle import compute_decay_score
from robothor.memory.outcomes import compute_outcome_penalty


class TestOutcomePenalty:
    def test_zero_failures_no_penalty(self):
        assert compute_outcome_penalty(0) == 0.0

    def test_one_failure_applies_per_failure_penalty(self):
        assert compute_outcome_penalty(1) == pytest.approx(0.1)

    def test_penalty_caps_at_max(self):
        # _MAX_PENALTY is 0.4, so 10 failures should clamp
        assert compute_outcome_penalty(10) == pytest.approx(0.4)


class TestDecayFactorsOutcomeFailures:
    def _base_args(self):
        # Mid-range score (neither saturated at 1.0 nor floored at 0.0) so
        # we can observe the effect of the outcome penalty.
        return {
            "last_accessed": datetime.now(UTC) - timedelta(hours=72),
            "access_count": 1,
            "reinforcement_count": 0,
            "importance_score": 0.5,
        }

    def test_no_failures_matches_baseline(self):
        # Two calls pick up slightly different datetime.now() values, so we
        # allow a tiny tolerance — the point is that outcome_failures=0 is
        # functionally indistinguishable from the default.
        args = self._base_args()
        no_failures = compute_decay_score(**args, outcome_failures=0)
        default = compute_decay_score(**args)
        assert abs(no_failures - default) < 1e-4

    def test_failures_reduce_decay_score(self):
        args = self._base_args()
        healthy = compute_decay_score(**args, outcome_failures=0)
        wounded = compute_decay_score(**args, outcome_failures=2)
        assert wounded < healthy

    def test_score_stays_non_negative(self):
        score = compute_decay_score(
            last_accessed=datetime.now(UTC) - timedelta(days=30),
            access_count=0,
            reinforcement_count=0,
            importance_score=0.1,
            outcome_failures=20,
        )
        assert score >= 0.0


@pytest.mark.integration
class TestLogAndBump:
    """End-to-end round-trip against real DB, with cleanup."""

    @pytest.mark.asyncio
    async def test_log_then_bump_cycle(self, monkeypatch):
        """Insert two test facts, log access, bump failure, verify counts."""
        from robothor.db.connection import get_connection
        from robothor.memory import outcomes

        # Create two test facts with unique markers so cleanup is safe.
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO memory_facts
                    (fact_text, category, confidence, tenant_id, is_active, source_type)
                VALUES
                    ('__test_outcome_a__', 'personal', 0.8, 'test', TRUE, 'test'),
                    ('__test_outcome_b__', 'personal', 0.8, 'test', TRUE, 'test')
                RETURNING id
                """
            )
            fact_ids = [row[0] for row in cur.fetchall()]

        test_run_id = "__test_outcome_run__"
        try:
            outcomes.log_fact_access(test_run_id, fact_ids)
            result = outcomes.bump_failure_for_run(test_run_id)
            assert result["facts_touched"] == 2

            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT outcome_failures FROM memory_facts WHERE id = ANY(%s)",
                    (fact_ids,),
                )
                failures = [row[0] for row in cur.fetchall()]
            assert all(f == 1 for f in failures)
        finally:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM fact_access_log WHERE run_id = %s", (test_run_id,))
                cur.execute("DELETE FROM memory_facts WHERE id = ANY(%s)", (fact_ids,))
