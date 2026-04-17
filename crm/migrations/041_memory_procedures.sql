-- Migration 041: Procedural Memory (skill library)
--
-- Agents record "how to do X" as first-class procedures — named, versioned,
-- with explicit steps, prerequisites, applicability tags, and outcome
-- tracking. Future runs find_procedure(task) before acting to reuse proven
-- playbooks instead of re-deriving.

BEGIN;

CREATE TABLE IF NOT EXISTS memory_procedures (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default'
        REFERENCES crm_tenants(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    steps TEXT[] NOT NULL DEFAULT '{}',
    prerequisites TEXT[] NOT NULL DEFAULT '{}',
    applicable_tags TEXT[] NOT NULL DEFAULT '{}',
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    embedding vector(1024),
    created_by_agent TEXT NOT NULL DEFAULT 'unknown',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, name)
);

CREATE INDEX IF NOT EXISTS idx_procedures_tenant
    ON memory_procedures(tenant_id)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_procedures_tags
    ON memory_procedures USING GIN(applicable_tags)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_procedures_embedding
    ON memory_procedures USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

COMMIT;
