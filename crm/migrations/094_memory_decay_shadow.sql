-- Migration 094: Shadow columns for the decay repair, and a prune manifest
--
-- WHY
--   compute_decay_score takes five inputs and three of them have no writer:
--     reinforcement_count  0 of 25,853 active rows are non-zero
--     access_count        24 of 25,853 (0.09%)
--     last_accessed        2 rows differ from created_at, so "recency" is age
--   Only importance_score and outcome_failures carry signal, which makes the
--   retirement decision close to arbitrary. The archival sweep also gates on
--   access_count = 0 as a "never used" guard — true for 99.9% of rows, so it
--   protects nothing selective.
--
--   The real usage signal was being destroyed nightly until migration 092;
--   fact_access_log holds 12,432 rows covering 3,409 distinct facts, so a
--   backfill lights up 142x more rows than the 24 currently set.
--
-- WHY SHADOW COLUMNS
--   access_count is also read on the retrieval path (facts._blend_rank weights
--   it 0.05), so backfilling it in place silently re-ranks search results at
--   the same moment it changes decay. Writing to shadow columns keeps the two
--   concerns separable: decay can consume the repaired values while retrieval
--   keeps its current behaviour, and the ranking change can then be measured
--   on its own against the frozen question set.
--
--   It also makes the backfill reversible by ignoring a column rather than
--   restoring a table.
--
-- SAFETY
--   The backfill is monotonically safer for pruning: access_boost only rises
--   and last_accessed_shadow >= created_at, so no fact can become *more*
--   prunable than it is today. Nothing is retired as a side effect of this
--   migration; it adds columns and a manifest column only.
--
-- IDEMPOTENT: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.
-- Rollback:
--   ALTER TABLE memory_facts DROP COLUMN IF EXISTS access_count_shadow, ...;
--   ALTER TABLE memory_facts_audit DROP COLUMN IF EXISTS snapshot;

BEGIN;

ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS access_count_shadow  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_accessed_shadow TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS decay_score_shadow   DOUBLE PRECISION;

-- Partial: only the rows the backfill actually lit up are interesting.
CREATE INDEX IF NOT EXISTS idx_facts_access_shadow
    ON memory_facts(access_count_shadow)
    WHERE access_count_shadow > 0;

-- prune_low_quality_facts already RETURNs the rows it deactivates and then
-- discards them, so a soft delete that is reversible in principle has no
-- manifest to reverse *from*. memory_facts_audit is the right home — it is
-- append-only, retained indefinitely, and already indexed on
-- (fact_id, snapshot_at) and (reason, snapshot_at) — but it has nowhere to put
-- the pre-prune scoring state needed to review a retirement after the fact.
ALTER TABLE memory_facts_audit
    ADD COLUMN IF NOT EXISTS snapshot JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMIT;
