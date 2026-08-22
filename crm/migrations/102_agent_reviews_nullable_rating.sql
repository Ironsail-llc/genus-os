-- Migration 102: agent_reviews.rating becomes NULL-able — "no grade" is a value.
--
-- The nightly goal review computed `rating = achievement["rating"] or 3`.
-- `compute_achievement_score` deliberately returns rating=None when nothing
-- measurable exists, with a comment explaining that a number would be a
-- fabrication; the caller invented one anyway because this column was NOT
-- NULL and `create_review` clamped everything into 1-5. Every consumer then
-- read a mid-range pass where there was no evidence at all.
--
-- On 2026-08-21 that produced 20 of 20 auto-review rows at 3/5 with
-- categories->'score' = null and feedback "Goal achievement: not measured".
--
-- The CHECK (rating BETWEEN 1 AND 5) is left in place: SQL CHECK constraints
-- pass on NULL, so a NULL rating is accepted and any non-NULL rating stays
-- bounded. Every aggregate over this column (AVG in analytics.py and
-- dal.get_review_summary, the confidence-weighted judge SUM in goals.py)
-- already ignores or now explicitly excludes NULLs.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.agent_reviews') IS NULL THEN
        RAISE NOTICE 'agent_reviews not present; skipping 102';
        RETURN;
    END IF;

    ALTER TABLE agent_reviews ALTER COLUMN rating DROP NOT NULL;

    -- Retire the historical fabrications. Narrowly scoped on purpose: only
    -- auto-review rows that recorded a JSON-null score (i.e. the reviewer
    -- itself said it measured nothing) and were stamped with the invented
    -- neutral 3. Rows carrying a real score are untouched.
    UPDATE agent_reviews
       SET rating = NULL
     WHERE reviewer = 'auto-review'
       AND reviewer_type = 'system'
       AND rating = 3
       AND categories -> 'score' = 'null'::jsonb;
END
$$;

COMMENT ON COLUMN agent_reviews.rating IS
    '1-5 grade, or NULL when the reviewer could not measure the agent. '
    'NULL means "no grade", never "average" — do not COALESCE it to a number.';

COMMIT;
