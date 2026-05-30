-- Migration 039: Allow reviewer_type='judge' on agent_reviews
--
-- The goal-judge (self-improvement Phase 1) writes one review per sampled run
-- with reviewer_type='judge' and categories.dimension='goal_achievement'.
-- goals.py reads those rows as the new spine of the achievement score. The
-- constraint from migration 036 allowed only operator/agent/system/buddy, so
-- the judge's inserts would fail. Extend it.

BEGIN;

ALTER TABLE agent_reviews
    DROP CONSTRAINT IF EXISTS agent_reviews_reviewer_type_check;

ALTER TABLE agent_reviews
    ADD CONSTRAINT agent_reviews_reviewer_type_check
    CHECK (reviewer_type = ANY (ARRAY[
        'operator'::text,
        'agent'::text,
        'system'::text,
        'buddy'::text,
        'judge'::text
    ]));

-- Speed the judge's idempotency probe (skip runs already judged) and goals.py's
-- windowed read of judge rows.
CREATE INDEX IF NOT EXISTS idx_agent_reviews_judge
    ON agent_reviews (agent_id, created_at)
    WHERE reviewer_type = 'judge';

COMMIT;
