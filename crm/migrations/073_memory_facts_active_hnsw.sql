-- Migration 073: partial HNSW index on active facts (fixes vector recall collapse)
--
-- Root cause (found on the live instance, 2026-05-30): pgvector 0.6.0 HNSW does
-- POST-filtering — it fetches its ~ef_search nearest candidates from the index
-- first, then applies the WHERE clause. memory_facts had ~104k rows but only
-- ~20k active; the ~84k inactive/superseded near-duplicate vectors crowded the
-- index, so `ORDER BY embedding <=> q WHERE is_active AND tenant_id` returned as
-- few as 1 row (exact seqscan returned a full 30). Result: semantic memory
-- search surfaced weak, near-random hits while structured blocks looked fine.
--
-- Fix: a partial HNSW index over active rows only. The planner prefers it for
-- the common `WHERE is_active` retrieval path, so the index no longer wastes its
-- candidate budget on dead rows. The original full index is kept for the rare
-- active_only=False path.
--
-- NOTE: the live index was built with CREATE INDEX CONCURRENTLY (no write lock)
-- on 2026-05-30; this migration is for reproducibility / fresh installs. On a
-- populated DB prefer the CONCURRENTLY form out-of-band to avoid a write lock.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_facts_embedding_active
    ON memory_facts USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200)
    WHERE is_active;

COMMIT;

-- Follow-up (not yet at scale, same latent pattern): memory_episodes,
-- memory_insights, memory_procedures, and memory_vault all filter on an
-- active/status flag over an HNSW index. Convert their embedding indexes to
-- partial-on-active before those tables accumulate inactive rows.
