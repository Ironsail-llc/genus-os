-- Robothor Database Initialization
--
-- This script runs automatically when the PostgreSQL container starts
-- for the first time. It creates the pgvector extension and all
-- required tables for the memory system.

-- Enable pgvector for semantic search
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- Memory Facts -- structured facts with vector embeddings
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_facts (
    id SERIAL PRIMARY KEY,
    fact_text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'personal',
    entities TEXT[] DEFAULT '{}',
    confidence FLOAT DEFAULT 1.0,
    source_content TEXT,
    source_type TEXT DEFAULT 'api',
    embedding vector(1024),
    metadata JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for vector similarity search
CREATE INDEX IF NOT EXISTS idx_memory_facts_embedding
    ON memory_facts USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Index for filtering by category and active status
CREATE INDEX IF NOT EXISTS idx_memory_facts_category
    ON memory_facts (category, is_active);

-- ============================================================
-- Memory Entities -- knowledge graph nodes
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_entities (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    aliases TEXT[] DEFAULT '{}',
    mention_count INT DEFAULT 1,
    last_seen TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(name, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_memory_entities_name
    ON memory_entities (lower(name));

-- ============================================================
-- Memory Relations -- knowledge graph edges
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_relations (
    id SERIAL PRIMARY KEY,
    source_entity_id INT REFERENCES memory_entities(id) ON DELETE CASCADE,
    target_entity_id INT REFERENCES memory_entities(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    fact_id INT REFERENCES memory_facts(id) ON DELETE SET NULL,
    confidence FLOAT DEFAULT 1.0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_entity_id, target_entity_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_memory_relations_source
    ON memory_relations (source_entity_id);

CREATE INDEX IF NOT EXISTS idx_memory_relations_target
    ON memory_relations (target_entity_id);
