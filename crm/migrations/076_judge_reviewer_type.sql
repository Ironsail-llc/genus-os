-- Migration 076: extend agent_reviews.reviewer_type to allow 'judge' (Phase 1).
--
-- BUG-1: the canonical runner `python -m robothor.db.migrate` scans ONLY
-- crm/migrations/. The original judge constraint lived in infra/migrations/039,
-- which db.migrate never applies — so a fresh `db.migrate apply` left the
-- reviewer_type CHECK without 'judge' and every goal-judge insert failed. This
-- moves it into the scanned dir. Guarded so it's a no-op on a DB that hasn't
-- created agent_reviews yet (that table comes from infra/migrations/031).

DO $$
BEGIN
    IF to_regclass('public.agent_reviews') IS NOT NULL THEN
        ALTER TABLE agent_reviews DROP CONSTRAINT IF EXISTS agent_reviews_reviewer_type_check;
        ALTER TABLE agent_reviews ADD CONSTRAINT agent_reviews_reviewer_type_check
            CHECK (reviewer_type = ANY (ARRAY[
                'operator', 'agent', 'system', 'buddy', 'judge'
            ]::text[]));
        CREATE INDEX IF NOT EXISTS idx_agent_reviews_judge
            ON agent_reviews (agent_id, created_at)
            WHERE reviewer_type = 'judge';
    END IF;
END $$;
