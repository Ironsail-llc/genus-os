-- ============================================================================
-- Robothor Initial Schema Migration
-- ============================================================================
-- Database: robothor_memory
-- Run: psql -U $ROBOTHOR_DB_USER -d $ROBOTHOR_DB_NAME -f 001_init.sql
--
-- This migration creates all tables for:
--   - Vector extension (pgvector)
--   - Short-term and long-term memory (tiered storage)
--   - Structured fact store with lifecycle management
--   - Entity knowledge graph (entities + relations)
--   - Contact identity resolution
--   - Agent working memory blocks
--   - Ingestion dedup and watermarks
--   - Audit logging
--   - CRM (people, companies, notes, tasks, conversations, messages)
--   - Telemetry (service health metrics)
-- ============================================================================

BEGIN;

-- ── Extensions ─────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ════════════════════════════════════════════════════════════════════════════
-- MEMORY SYSTEM
-- ════════════════════════════════════════════════════════════════════════════

-- ── Short-Term Memory (48h TTL, auto-expires) ──────────────────────────────

CREATE TABLE IF NOT EXISTS short_term_memory (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    content_type VARCHAR(50) NOT NULL,
    embedding vector(1024),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '48 hours'),
    accessed_at TIMESTAMPTZ DEFAULT NOW(),
    access_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_stm_expires ON short_term_memory (expires_at);
CREATE INDEX IF NOT EXISTS idx_stm_content_type ON short_term_memory (content_type);

-- ── Long-Term Memory (permanent, importance-scored) ────────────────────────

CREATE TABLE IF NOT EXISTS long_term_memory (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    summary TEXT,
    content_type VARCHAR(50) NOT NULL,
    embedding vector(1024),
    metadata JSONB DEFAULT '{}',
    original_date TIMESTAMPTZ,
    archived_at TIMESTAMPTZ DEFAULT NOW(),
    source_tier2_ids INTEGER[]
);

CREATE INDEX IF NOT EXISTS idx_ltm_content_type ON long_term_memory (content_type);
CREATE INDEX IF NOT EXISTS idx_ltm_archived_at ON long_term_memory (archived_at);

-- ── Fact Store (structured facts with lifecycle) ───────────────────────────

CREATE TABLE IF NOT EXISTS memory_facts (
    id SERIAL PRIMARY KEY,
    fact_text TEXT NOT NULL,
    category VARCHAR(50) NOT NULL,
    entities TEXT[] DEFAULT '{}',
    confidence FLOAT DEFAULT 1.0,
    source_content TEXT,
    source_type VARCHAR(50),
    source_channel VARCHAR(50),
    embedding vector(1024),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    superseded_by INTEGER REFERENCES memory_facts(id),
    is_active BOOLEAN DEFAULT TRUE,
    -- Lifecycle columns
    access_count INTEGER DEFAULT 0,
    last_accessed TIMESTAMPTZ DEFAULT NOW(),
    importance_score FLOAT DEFAULT 0.5,
    decay_score FLOAT DEFAULT 1.0,
    reinforcement_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_facts_category ON memory_facts (category);
CREATE INDEX IF NOT EXISTS idx_facts_active ON memory_facts (is_active);
CREATE INDEX IF NOT EXISTS idx_facts_importance ON memory_facts (importance_score DESC);
CREATE INDEX IF NOT EXISTS idx_facts_entities ON memory_facts USING GIN (entities);
CREATE INDEX IF NOT EXISTS idx_facts_created ON memory_facts (created_at DESC);

-- ── Entity Knowledge Graph ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memory_entities (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,
    aliases TEXT[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW(),
    mention_count INTEGER DEFAULT 1,
    UNIQUE(name, entity_type)
);

CREATE INDEX IF NOT EXISTS idx_entities_type ON memory_entities (entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON memory_entities (name);
CREATE INDEX IF NOT EXISTS idx_entities_mention ON memory_entities (mention_count DESC);

CREATE TABLE IF NOT EXISTS memory_relations (
    id SERIAL PRIMARY KEY,
    source_entity_id INTEGER REFERENCES memory_entities(id) ON DELETE CASCADE,
    target_entity_id INTEGER REFERENCES memory_entities(id) ON DELETE CASCADE,
    relation_type VARCHAR(100) NOT NULL,
    metadata JSONB DEFAULT '{}',
    fact_id INTEGER REFERENCES memory_facts(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    confidence FLOAT DEFAULT 1.0,
    UNIQUE(source_entity_id, target_entity_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_relations_source ON memory_relations (source_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON memory_relations (target_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_type ON memory_relations (relation_type);

-- ── Contact Identity Resolution ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS contact_identifiers (
    id SERIAL PRIMARY KEY,
    channel VARCHAR(50) NOT NULL,
    identifier VARCHAR(255) NOT NULL,
    display_name TEXT,
    person_id UUID,
    memory_entity_id INTEGER REFERENCES memory_entities(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(channel, identifier)
);

CREATE INDEX IF NOT EXISTS idx_ci_person ON contact_identifiers (person_id);
CREATE INDEX IF NOT EXISTS idx_ci_entity ON contact_identifiers (memory_entity_id);
CREATE INDEX IF NOT EXISTS idx_ci_channel ON contact_identifiers (channel);

-- ── Agent Working Memory Blocks ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_memory_blocks (
    id SERIAL PRIMARY KEY,
    block_name VARCHAR(100) NOT NULL UNIQUE,
    block_type VARCHAR(50) NOT NULL DEFAULT 'text',
    content TEXT DEFAULT '',
    max_chars INTEGER DEFAULT 5000,
    last_written_at TIMESTAMPTZ DEFAULT NOW(),
    last_read_at TIMESTAMPTZ,
    read_count INTEGER DEFAULT 0,
    write_count INTEGER DEFAULT 0
);

-- Seed default memory blocks
INSERT INTO agent_memory_blocks (block_name, block_type, max_chars) VALUES
    ('persona', 'system', 3000),
    ('user_profile', 'system', 5000),
    ('working_context', 'ephemeral', 5000),
    ('operational_findings', 'persistent', 5000),
    ('contacts_summary', 'persistent', 5000)
ON CONFLICT (block_name) DO NOTHING;

-- ── Ingestion Dedup and Watermarks ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ingested_items (
    id SERIAL PRIMARY KEY,
    source_name VARCHAR(100) NOT NULL,
    item_id VARCHAR(255) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    fact_ids INTEGER[] DEFAULT '{}',
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_name, item_id)
);

CREATE INDEX IF NOT EXISTS idx_ingested_source ON ingested_items (source_name);
CREATE INDEX IF NOT EXISTS idx_ingested_at ON ingested_items (ingested_at);

CREATE TABLE IF NOT EXISTS ingestion_watermarks (
    source_name VARCHAR(100) PRIMARY KEY,
    last_ingested_at TIMESTAMPTZ,
    items_ingested INTEGER DEFAULT 0,
    last_error TEXT,
    error_count INTEGER DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ════════════════════════════════════════════════════════════════════════════
-- AUDIT LOG
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    event_type VARCHAR(50) NOT NULL,
    category VARCHAR(50),
    actor VARCHAR(100) DEFAULT 'robothor',
    action TEXT NOT NULL,
    details JSONB,
    source_channel VARCHAR(50),
    target VARCHAR(255),
    status VARCHAR(20) DEFAULT 'ok',
    session_key VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log (event_type);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_category ON audit_log (category);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor);

-- ════════════════════════════════════════════════════════════════════════════
-- CRM TABLES
-- ════════════════════════════════════════════════════════════════════════════

-- ── Companies ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_companies (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    domain_name TEXT,
    employees INTEGER,
    address_street1 TEXT,
    address_street2 TEXT,
    address_city TEXT,
    address_state TEXT,
    address_postcode TEXT,
    address_country TEXT,
    linkedin_url TEXT,
    annual_recurring_revenue_micros BIGINT,
    annual_recurring_revenue_currency TEXT,
    ideal_customer_profile BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crm_companies_name ON crm_companies (name);
CREATE INDEX IF NOT EXISTS idx_crm_companies_updated_at ON crm_companies (updated_at);

-- ── People ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_people (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    first_name TEXT,
    last_name TEXT,
    email TEXT,
    additional_emails JSONB,
    phone TEXT,
    phone_country_code TEXT,
    phone_calling_code TEXT,
    additional_phones JSONB,
    job_title TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    linkedin_url TEXT,
    x_url TEXT,
    company_id UUID REFERENCES crm_companies(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crm_people_name ON crm_people (first_name, last_name);
CREATE INDEX IF NOT EXISTS idx_crm_people_email ON crm_people (email);
CREATE INDEX IF NOT EXISTS idx_crm_people_updated_at ON crm_people (updated_at);
CREATE INDEX IF NOT EXISTS idx_crm_people_company_id ON crm_people (company_id);

-- Add FK from contact_identifiers to crm_people
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_ci_person_id'
    ) THEN
        ALTER TABLE contact_identifiers
            ADD CONSTRAINT fk_ci_person_id
            FOREIGN KEY (person_id) REFERENCES crm_people(id);
    END IF;
END $$;

-- ── Notes ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_notes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL DEFAULT '',
    body TEXT,
    person_id UUID REFERENCES crm_people(id),
    company_id UUID REFERENCES crm_companies(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crm_notes_person_id ON crm_notes (person_id);
CREATE INDEX IF NOT EXISTS idx_crm_notes_company_id ON crm_notes (company_id);
CREATE INDEX IF NOT EXISTS idx_crm_notes_updated_at ON crm_notes (updated_at);

-- ── Tasks ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_tasks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL DEFAULT '',
    body TEXT,
    status TEXT DEFAULT 'TODO',
    due_at TIMESTAMPTZ,
    assignee_id UUID,
    person_id UUID REFERENCES crm_people(id),
    company_id UUID REFERENCES crm_companies(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crm_tasks_status ON crm_tasks (status);
CREATE INDEX IF NOT EXISTS idx_crm_tasks_person_id ON crm_tasks (person_id);
CREATE INDEX IF NOT EXISTS idx_crm_tasks_updated_at ON crm_tasks (updated_at);

-- ── Conversations ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_conversations (
    id SERIAL PRIMARY KEY,
    person_id UUID REFERENCES crm_people(id),
    status TEXT NOT NULL DEFAULT 'open',
    inbox_name TEXT NOT NULL DEFAULT '',
    messages_count INTEGER NOT NULL DEFAULT 0,
    last_activity_at TIMESTAMPTZ,
    additional_attributes JSONB,
    custom_attributes JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_crm_conversations_status ON crm_conversations (status);
CREATE INDEX IF NOT EXISTS idx_crm_conversations_person_id ON crm_conversations (person_id);
CREATE INDEX IF NOT EXISTS idx_crm_conversations_last_activity ON crm_conversations (last_activity_at);

-- ── Messages ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS crm_messages (
    id SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES crm_conversations(id),
    content TEXT,
    message_type TEXT NOT NULL DEFAULT 'incoming',
    private BOOLEAN NOT NULL DEFAULT FALSE,
    sender_name TEXT,
    sender_type TEXT,
    content_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_crm_messages_conversation_id ON crm_messages (conversation_id);
CREATE INDEX IF NOT EXISTS idx_crm_messages_created_at ON crm_messages (created_at);

-- ════════════════════════════════════════════════════════════════════════════
-- TELEMETRY
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS telemetry (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    service VARCHAR(100) NOT NULL,
    metric VARCHAR(100) NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    unit VARCHAR(30),
    details JSONB
);

CREATE INDEX IF NOT EXISTS idx_telemetry_service ON telemetry (service);
CREATE INDEX IF NOT EXISTS idx_telemetry_metric ON telemetry (metric);
CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp ON telemetry (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_service_metric ON telemetry (service, metric, timestamp DESC);

-- ════════════════════════════════════════════════════════════════════════════
-- VECTOR INDEXES (IVFFlat)
-- ════════════════════════════════════════════════════════════════════════════
-- IVFFlat indexes require existing data to train. On a fresh database, these
-- will be created but may not be effective until data is loaded.
-- For production, rebuild after initial data load:
--   REINDEX INDEX idx_stm_embedding;
-- ════════════════════════════════════════════════════════════════════════════

-- Use cosine distance (<=> operator) for normalized embeddings
CREATE INDEX IF NOT EXISTS idx_stm_embedding
    ON short_term_memory USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_ltm_embedding
    ON long_term_memory USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_facts_embedding
    ON memory_facts USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ════════════════════════════════════════════════════════════════════════════
-- SEARCH FUNCTION
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION search_memories(
    query_embedding vector(1024),
    limit_count INTEGER DEFAULT 10,
    search_short_term BOOLEAN DEFAULT TRUE,
    search_long_term BOOLEAN DEFAULT TRUE
)
RETURNS TABLE(
    id INTEGER,
    content TEXT,
    content_type VARCHAR(50),
    similarity FLOAT,
    source_tier VARCHAR(20),
    created_at TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT * FROM (
        SELECT
            stm.id,
            stm.content,
            stm.content_type,
            1 - (stm.embedding <=> query_embedding) AS similarity,
            'short_term'::VARCHAR(20) AS source_tier,
            stm.created_at
        FROM short_term_memory stm
        WHERE search_short_term AND stm.embedding IS NOT NULL
            AND stm.expires_at > NOW()
        ORDER BY stm.embedding <=> query_embedding
        LIMIT limit_count

        UNION ALL

        SELECT
            ltm.id,
            ltm.content,
            ltm.content_type,
            1 - (ltm.embedding <=> query_embedding) AS similarity,
            'long_term'::VARCHAR(20) AS source_tier,
            ltm.archived_at AS created_at
        FROM long_term_memory ltm
        WHERE search_long_term AND ltm.embedding IS NOT NULL
        ORDER BY ltm.embedding <=> query_embedding
        LIMIT limit_count
    ) combined
    ORDER BY similarity DESC
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql;

COMMIT;
