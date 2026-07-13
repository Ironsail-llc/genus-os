-- Memory v4 schema consolidation
-- Archives legacy short/long-term tables (frozen since Feb 3, 2026),
-- adds tsv column for BM25, switches to HNSW index, drops dead function.
-- Idempotent — safe to run on both fresh and existing databases.
BEGIN;

-- Preserve legacy rows under explicit archive names.  The old migration used
-- destructive table removal with CASCADE based only on an operational comment, which could
-- destroy an upgraded installation's memory without a machine-enforced gate.
-- Renaming removes the obsolete runtime names while making recovery possible.
DO $$
BEGIN
  IF to_regclass('public.short_term_memory') IS NOT NULL THEN
    IF to_regclass('public.migration_archive_023_short_term_memory') IS NOT NULL THEN
      RAISE EXCEPTION
        'both short_term_memory and migration_archive_023_short_term_memory exist; refusing ambiguous migration';
    END IF;
    ALTER TABLE short_term_memory RENAME TO migration_archive_023_short_term_memory;
  END IF;

  IF to_regclass('public.long_term_memory') IS NOT NULL THEN
    IF to_regclass('public.migration_archive_023_long_term_memory') IS NOT NULL THEN
      RAISE EXCEPTION
        'both long_term_memory and migration_archive_023_long_term_memory exist; refusing ambiguous migration';
    END IF;
    ALTER TABLE long_term_memory RENAME TO migration_archive_023_long_term_memory;
  END IF;
END $$;

-- Add tsvector column for BM25 keyword search
ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', fact_text)) STORED;
CREATE INDEX IF NOT EXISTS idx_facts_tsv ON memory_facts USING GIN(tsv);

-- Replace IVFFlat with HNSW (better recall, no training data needed)
DROP INDEX IF EXISTS idx_facts_embedding;
CREATE INDEX IF NOT EXISTS idx_facts_embedding ON memory_facts
  USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=200);

-- Drop legacy unified search function (referenced dropped tables)
DROP FUNCTION IF EXISTS search_memories(vector, integer, boolean, boolean);

COMMIT;
