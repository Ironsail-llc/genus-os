-- Migration 080: allow reviewer_type='judge' on agent_reviews.
--
-- The goal-judge spine writes one review per graded run with
-- reviewer_type='judge' so its signal is separable from operator/agent/system/
-- buddy reviews. The prior CHECK (031 + infra 036) omitted 'judge', so every
-- judge verdict INSERT failed silently. This adds 'judge' as a superset of the
-- existing values.
--
-- Idempotent: DROP ... IF EXISTS + re-ADD, and no-op if the table is absent.
-- (memory-100 also ships an equivalent 076_judge_reviewer_type on its own
-- branch; both are idempotent supersets, so applying either/both is safe.)

DO $$
BEGIN
    IF to_regclass('public.agent_reviews') IS NULL THEN
        RAISE NOTICE 'agent_reviews not present; skipping 080';
        RETURN;
    END IF;

    ALTER TABLE agent_reviews
        DROP CONSTRAINT IF EXISTS agent_reviews_reviewer_type_check;

    ALTER TABLE agent_reviews
        ADD CONSTRAINT agent_reviews_reviewer_type_check
        CHECK (reviewer_type = ANY (
            ARRAY['operator'::text, 'agent'::text, 'system'::text, 'buddy'::text, 'judge'::text]
        ));
END $$;
