-- ============================================================================
-- Migration 026: Consolidation Tracking & Memory Insights
-- ============================================================================
-- Adds:
--   1. consolidated_at column to memory_facts (intra-day consolidation tracking)
--   2. memory_insights table (cross-domain insight discovery)
-- ============================================================================

BEGIN;

-- ── Intra-day consolidation tracking ─────────────────────────────────────────

ALTER TABLE memory_facts ADD COLUMN IF NOT EXISTS consolidated_at TIMESTAMPTZ;

-- Backfill: mark all existing active facts as already consolidated
-- so the first intra-day run doesn't try to re-consolidate everything.
UPDATE memory_facts SET consolidated_at = updated_at
WHERE is_active = TRUE AND consolidated_at IS NULL;

-- ── Cross-domain insight store ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_insights (
    id SERIAL PRIMARY KEY,
    insight_text TEXT NOT NULL,
    source_fact_ids INTEGER[] NOT NULL DEFAULT '{}',
    categories TEXT[] DEFAULT '{}',
    entities TEXT[] DEFAULT '{}',
    embedding vector(1024),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE,
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_insights_active ON memory_insights (is_active)
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_insights_created ON memory_insights (created_at DESC);

-- HNSW index for vector search on insights
CREATE INDEX IF NOT EXISTS idx_insights_embedding
    ON memory_insights USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=200);

COMMIT;
