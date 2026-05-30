-- Migration 074: partial HNSW indexes for the remaining vector stores
--
-- Extends the migration-073 fix (memory_facts) to every other vector store that
-- filters retrieval on is_active over an HNSW index. pgvector 0.6.0 HNSW
-- post-filters (fetch ef_search nearest, THEN apply WHERE), so once a store
-- accumulates inactive/superseded rows its semantic recall collapses to a
-- handful of hits. Building the index partial-on-active keeps dead rows out of
-- the candidate budget. Done preemptively while these tables are still small.
--
-- (memory_intents is intentionally left non-partial: it's status-based, tiny,
-- and carries no inactive backlog. Revisit if it grows.)
--
-- NOTE: built live on 2026-05-30 with CREATE INDEX CONCURRENTLY (no write lock);
-- this migration is for reproducibility / fresh installs.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_episodes_embedding_active
    ON memory_episodes USING hnsw (summary_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200) WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_insights_embedding_active
    ON memory_insights USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200) WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_procedures_embedding_active
    ON memory_procedures USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200) WHERE is_active;

CREATE INDEX IF NOT EXISTS idx_vault_caption_emb_active
    ON memory_vault USING hnsw (caption_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200) WHERE is_active;

COMMIT;
