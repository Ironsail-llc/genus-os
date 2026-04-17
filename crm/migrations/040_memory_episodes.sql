-- Migration 040: Memory Episodes (time-bucketed event clusters)
--
-- Adds memory_episodes table for SOTA episodic memory.
-- An episode is a temporal+entity cluster of related facts — "what happened
-- together during a given period." Built nightly from memory_facts by
-- robothor/memory/episodes.py::build_episodes_from_facts.
--
-- Retrieval: search_facts(include_episodes=True) merges episode summaries
-- via RRF alongside facts.

BEGIN;

CREATE TABLE IF NOT EXISTS memory_episodes (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default'
        REFERENCES crm_tenants(id),
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    summary_embedding vector(1024),
    entity_ids INTEGER[] NOT NULL DEFAULT '{}',
    fact_ids INTEGER[] NOT NULL DEFAULT '{}',
    source_types TEXT[] NOT NULL DEFAULT '{}',
    fact_count INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (end_time >= start_time)
);

CREATE INDEX IF NOT EXISTS idx_episodes_tenant_time
    ON memory_episodes(tenant_id, start_time DESC)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_episodes_tenant_entities
    ON memory_episodes USING GIN(entity_ids)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_episodes_embedding
    ON memory_episodes USING hnsw (summary_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

COMMIT;
