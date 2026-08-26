-- Migration 109: make entity recall indexable.
--
-- `_build_entity_context` (robothor/engine/warmup.py) matched entities with
--     EXISTS (SELECT 1 FROM unnest(entities) e WHERE lower(e) = lower($1))
-- which no index can serve. Every agent turn ran that up to five times, and
-- each run was a full scan of the tenant's active facts.
--
-- Measured on this instance (169,248 rows, 29,446 active for the tenant):
--   * 13,155 shared buffers touched per call = 105 MB, against a 128 MB
--     shared_buffers — one query sweeps 82% of the buffer pool, five times
--     per warmup, ~350 agent runs a day.
--   * _build_entity_context median 262 ms, versus 2.7 ms for every other
--     warmup section. The engine's own logs show `cron main` SETUP at a
--     median of 495 ms while agents that never reach entity recall run
--     15-40 ms.
--
-- The existing `idx_facts_entities` GIN cannot help: it indexes the raw
-- array, and the predicate lowercases each element. Dropping lower() instead
-- of indexing it is NOT equivalent — 646 of 14,714 distinct entity names
-- collapse under lower() on this instance ('Ironsail', 'IRONSAIL',
-- 'IroNsail', 'IronSail'), and a plain `entities && ARRAY[name]` was measured
-- losing 1,191 of 3,222 matched rows (37%).
--
-- So index the lowered form. An IMMUTABLE wrapper is required because GIN
-- expression indexes may only contain immutable expressions, and
-- array_agg(lower(x)) over unnest is not inferable as such.
--
-- Measured after: 145 ms -> 3.7 ms for the same five candidates, returning
-- identical rows.
--
-- NOTE: on a populated database build the index out-of-band with
-- CREATE INDEX CONCURRENTLY to avoid a write lock; this plain form is for
-- fresh installs and reproducibility (same convention as migration 073).

CREATE OR REPLACE FUNCTION lower_entities(text[])
RETURNS text[]
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
STRICT
AS $$ SELECT array_agg(lower(x)) FROM unnest($1) AS x $$;

CREATE INDEX IF NOT EXISTS idx_facts_entities_lower
    ON memory_facts USING gin (lower_entities(entities));

-- Date-windowed fact reads (working_context) had no supporting index and
-- scanned the tenant's rows for every window.
CREATE INDEX IF NOT EXISTS idx_facts_tenant_created
    ON memory_facts (tenant_id, created_at DESC);
