-- Migration 036: Allow reviewer_type='buddy' on agent_reviews
--
-- Buddy writes one review per sampled run with reviewer_type='buddy'
-- so downstream code can filter its signal from operator and system reviews.
-- The old CHECK constraint allowed only operator/agent/system, which made
-- Buddy's inserts fail silently.

BEGIN;

ALTER TABLE agent_reviews
    DROP CONSTRAINT IF EXISTS agent_reviews_reviewer_type_check;

ALTER TABLE agent_reviews
    ADD CONSTRAINT agent_reviews_reviewer_type_check
    CHECK (reviewer_type = ANY (ARRAY['operator'::text, 'agent'::text, 'system'::text, 'buddy'::text]));

COMMIT;
