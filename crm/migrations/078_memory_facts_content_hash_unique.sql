-- Migration 078: write-time uniqueness for ACTIVE memory_facts on content_hash.
--
-- content_hash already exists (migration 069) with a NON-unique btree index, so
-- nothing stops byte-identical inserts: there are thousands of exact-duplicate
-- facts (e.g. "An unknown person was detected at camera-0." stored 945x) and
-- ~80% of the table is supersession exhaust. This enforces uniqueness on the
-- ACTIVE set so store_fact can `ON CONFLICT (tenant_id, content_hash) DO NOTHING`
-- (gated by MEMORY_WRITE_DEDUP) and stop minting fresh copies of known facts.
--
-- Reversible: step 1 only flips is_active (no hard deletes); step 2's index is
-- droppable. Inactive duplicates are left untouched (the partial index ignores
-- them), so this does not disturb supersession history.

-- 1. Soft-dedup active duplicates: keep the newest active row per
--    (tenant_id, content_hash), deactivate the rest. Only ~22 groups today.
UPDATE memory_facts a
SET is_active = FALSE, updated_at = NOW()
FROM memory_facts b
WHERE a.id < b.id
  AND a.is_active = TRUE
  AND b.is_active = TRUE
  AND a.content_hash IS NOT NULL
  AND a.tenant_id = b.tenant_id
  AND a.content_hash = b.content_hash;

-- 2. Partial unique index over the active set. (Plain, not CONCURRENTLY: the
--    active dup count is tiny and the migration runner may wrap in a txn.)
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_facts_active_content_hash
    ON memory_facts (tenant_id, content_hash)
    WHERE is_active = TRUE AND content_hash IS NOT NULL;
