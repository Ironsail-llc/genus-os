-- Migration 150: pr_reviews.requested_at -- the review queue's order
-- (robothor/pr_review/store.py).
--
-- list_pending sorted by updated_at, which moves whenever a row is touched for
-- any reason (a poll refreshing the title, a Chat reply), so a request could
-- lose its place to a newer one. requested_at is set when a trigger goes from
-- none to one -- the Chat message's own time for a Chat request -- and never
-- on a later touch. It also starts pr_review_status's request-to-answer
-- latency. Rows queued before this migration take their updated_at, the order
-- they had; rows with no time sort last.
ALTER TABLE pr_reviews ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ;

UPDATE pr_reviews SET requested_at = updated_at
 WHERE pending_trigger <> '' AND requested_at IS NULL;

DROP INDEX IF EXISTS pr_reviews_pending;
CREATE INDEX IF NOT EXISTS pr_reviews_pending
    ON pr_reviews (tenant_id, requested_at) WHERE pending_trigger <> '';

-- Rollback:
--   DROP INDEX IF EXISTS pr_reviews_pending;
--   CREATE INDEX IF NOT EXISTS pr_reviews_pending
--       ON pr_reviews (tenant_id, updated_at) WHERE pending_trigger <> '';
--   ALTER TABLE pr_reviews DROP COLUMN IF EXISTS requested_at;
