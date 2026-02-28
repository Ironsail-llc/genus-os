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
--   - Task coordination + state machine + routines
--   - Multi-tenancy (crm_tenants + tenant_id columns)
--   - Agent notifications
--   - Health (Garmin biometric data)
--   - Agent engine (runs, steps, schedules, checkpoints, guardrails)
--   - Workflow engine (runs, steps)
--   - Vault (encrypted secret store)
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

-- ════════════════════════════════════════════════════════════════════════════
-- TASK COORDINATION (from migration 005)
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS created_by_agent TEXT;
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS assigned_to_agent TEXT;
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS priority TEXT DEFAULT 'normal';
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{}';
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS parent_task_id UUID REFERENCES crm_tasks(id);
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS resolution TEXT;

CREATE INDEX IF NOT EXISTS idx_crm_tasks_assigned_to_agent
    ON crm_tasks (assigned_to_agent) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_crm_tasks_created_by_agent
    ON crm_tasks (created_by_agent) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_crm_tasks_priority
    ON crm_tasks (priority) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_crm_tasks_parent_task_id
    ON crm_tasks (parent_task_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_crm_tasks_tags
    ON crm_tasks USING GIN (tags) WHERE deleted_at IS NULL;

-- ════════════════════════════════════════════════════════════════════════════
-- TASK STATE MACHINE (from migration 006)
-- ════════════════════════════════════════════════════════════════════════════

-- Status constraint (final set includes REVIEW)
DO $$ BEGIN
    ALTER TABLE crm_tasks ADD CONSTRAINT valid_status
        CHECK (status IN ('TODO', 'IN_PROGRESS', 'REVIEW', 'DONE'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS crm_task_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES crm_tasks(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_by TEXT,
    reason TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_task_history_task ON crm_task_history (task_id);
CREATE INDEX IF NOT EXISTS idx_task_history_created ON crm_task_history (created_at);

ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS sla_deadline_at TIMESTAMPTZ;
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS escalation_count INT DEFAULT 0;
ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;

-- ════════════════════════════════════════════════════════════════════════════
-- ROUTINES (from migration 007)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS crm_routines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    body TEXT,
    cron_expr TEXT NOT NULL,
    timezone TEXT DEFAULT 'America/New_York',
    assigned_to_agent TEXT,
    priority TEXT DEFAULT 'normal',
    tags TEXT[] DEFAULT '{}',
    person_id UUID REFERENCES crm_people(id),
    company_id UUID REFERENCES crm_companies(id),
    active BOOLEAN DEFAULT TRUE,
    next_run_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_routines_due ON crm_routines (next_run_at)
    WHERE active = TRUE AND deleted_at IS NULL;

-- ════════════════════════════════════════════════════════════════════════════
-- MULTI-TENANCY (from migration 008)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS crm_tenants (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    parent_tenant_id TEXT REFERENCES crm_tenants(id),
    settings JSONB DEFAULT '{}',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tenants_parent ON crm_tenants(parent_tenant_id) WHERE parent_tenant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tenants_active ON crm_tenants(active) WHERE active = TRUE;

INSERT INTO crm_tenants (id, display_name)
VALUES ('robothor-primary', 'Robothor Primary')
ON CONFLICT (id) DO NOTHING;

ALTER TABLE crm_people ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id);
CREATE INDEX IF NOT EXISTS idx_people_tenant ON crm_people(tenant_id) WHERE deleted_at IS NULL;

ALTER TABLE crm_companies ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id);
CREATE INDEX IF NOT EXISTS idx_companies_tenant ON crm_companies(tenant_id) WHERE deleted_at IS NULL;

ALTER TABLE crm_notes ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id);
CREATE INDEX IF NOT EXISTS idx_notes_tenant ON crm_notes(tenant_id) WHERE deleted_at IS NULL;

ALTER TABLE crm_tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON crm_tasks(tenant_id) WHERE deleted_at IS NULL;

ALTER TABLE crm_task_history ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id);
CREATE INDEX IF NOT EXISTS idx_task_history_tenant ON crm_task_history(tenant_id);

ALTER TABLE crm_routines ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id);
CREATE INDEX IF NOT EXISTS idx_routines_tenant ON crm_routines(tenant_id) WHERE deleted_at IS NULL;

ALTER TABLE crm_conversations ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id);
CREATE INDEX IF NOT EXISTS idx_conversations_tenant ON crm_conversations(tenant_id);

ALTER TABLE crm_messages ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id);
CREATE INDEX IF NOT EXISTS idx_messages_tenant ON crm_messages(tenant_id);

-- ════════════════════════════════════════════════════════════════════════════
-- AGENT NOTIFICATIONS (from migration 009)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS crm_agent_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id),
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    notification_type TEXT NOT NULL CHECK (notification_type IN (
        'task_assigned', 'review_requested', 'review_approved',
        'review_rejected', 'blocked', 'unblocked',
        'agent_error', 'info', 'custom'
    )),
    subject TEXT NOT NULL,
    body TEXT,
    metadata JSONB DEFAULT '{}',
    task_id UUID REFERENCES crm_tasks(id),
    read_at TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notifications_inbox
    ON crm_agent_notifications(to_agent, read_at NULLS FIRST)
    WHERE acknowledged_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_notifications_task
    ON crm_agent_notifications(task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notifications_tenant
    ON crm_agent_notifications(tenant_id);
CREATE INDEX IF NOT EXISTS idx_notifications_type
    ON crm_agent_notifications(notification_type, created_at DESC);

-- ════════════════════════════════════════════════════════════════════════════
-- HEALTH TABLES (from migration 010)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS health_heart_rate (
    timestamp BIGINT PRIMARY KEY,
    heart_rate INTEGER NOT NULL,
    source TEXT DEFAULT 'monitoring'
);
CREATE INDEX IF NOT EXISTS idx_health_hr_ts ON health_heart_rate(timestamp);

CREATE TABLE IF NOT EXISTS health_stress (
    timestamp BIGINT PRIMARY KEY,
    stress_level INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_stress_ts ON health_stress(timestamp);

CREATE TABLE IF NOT EXISTS health_body_battery (
    timestamp BIGINT PRIMARY KEY,
    level INTEGER NOT NULL,
    charged INTEGER,
    drained INTEGER
);
CREATE INDEX IF NOT EXISTS idx_health_bb_ts ON health_body_battery(timestamp);

CREATE TABLE IF NOT EXISTS health_spo2 (
    timestamp BIGINT PRIMARY KEY,
    spo2_value INTEGER NOT NULL,
    reading_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_spo2_ts ON health_spo2(timestamp);

CREATE TABLE IF NOT EXISTS health_respiration (
    timestamp BIGINT PRIMARY KEY,
    respiration_rate REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_resp_ts ON health_respiration(timestamp);

CREATE TABLE IF NOT EXISTS health_hrv (
    timestamp BIGINT PRIMARY KEY,
    hrv_value REAL,
    reading_type TEXT,
    status TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_hrv_ts ON health_hrv(timestamp);

CREATE TABLE IF NOT EXISTS health_sleep (
    date DATE PRIMARY KEY,
    start_timestamp BIGINT,
    end_timestamp BIGINT,
    total_sleep_seconds INTEGER,
    deep_sleep_seconds INTEGER,
    light_sleep_seconds INTEGER,
    rem_sleep_seconds INTEGER,
    awake_seconds INTEGER,
    score INTEGER,
    quality TEXT,
    raw_data JSONB
);

CREATE TABLE IF NOT EXISTS health_steps (
    date DATE PRIMARY KEY,
    total_steps INTEGER NOT NULL,
    goal INTEGER,
    distance_meters REAL,
    calories INTEGER,
    timestamp BIGINT
);

CREATE TABLE IF NOT EXISTS health_resting_heart_rate (
    date DATE PRIMARY KEY,
    resting_hr INTEGER NOT NULL,
    timestamp BIGINT
);

CREATE TABLE IF NOT EXISTS health_daily_summary (
    date DATE PRIMARY KEY,
    calories_total INTEGER,
    calories_active INTEGER,
    calories_bmr INTEGER,
    floors_climbed INTEGER,
    intensity_minutes INTEGER,
    raw_data JSONB
);

CREATE TABLE IF NOT EXISTS health_training_status (
    date DATE PRIMARY KEY,
    training_status TEXT,
    training_status_phrase TEXT,
    vo2max_running REAL,
    vo2max_cycling REAL,
    training_load_7_day INTEGER,
    training_load_28_day INTEGER,
    recovery_time_hours INTEGER,
    raw_data JSONB
);

CREATE TABLE IF NOT EXISTS health_activities (
    activity_id BIGINT PRIMARY KEY,
    name TEXT,
    activity_type TEXT,
    start_timestamp BIGINT NOT NULL,
    duration_seconds INTEGER,
    distance_meters REAL,
    calories INTEGER,
    avg_heart_rate INTEGER,
    max_heart_rate INTEGER,
    avg_pace REAL,
    elevation_gain REAL,
    vo2max REAL,
    training_effect_aerobic REAL,
    training_effect_anaerobic REAL,
    training_load INTEGER,
    raw_data JSONB
);
CREATE INDEX IF NOT EXISTS idx_health_act_start ON health_activities(start_timestamp);

CREATE TABLE IF NOT EXISTS health_sync_log (
    id SERIAL PRIMARY KEY,
    sync_timestamp BIGINT NOT NULL,
    metric_type TEXT NOT NULL,
    records_synced INTEGER,
    status TEXT,
    error_message TEXT
);

-- ════════════════════════════════════════════════════════════════════════════
-- AGENT ENGINE (from migration 011, 012, 014)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id),
    agent_id TEXT NOT NULL,

    trigger_type TEXT NOT NULL CHECK (trigger_type IN (
        'cron', 'hook', 'event', 'manual', 'telegram', 'webchat', 'workflow'
    )),
    trigger_detail TEXT,
    correlation_id UUID,

    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'running', 'completed', 'failed', 'timeout', 'cancelled'
    )),

    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,

    model_used TEXT,
    models_attempted TEXT[],
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    total_cost_usd NUMERIC(10, 6) DEFAULT 0,

    system_prompt_chars INTEGER DEFAULT 0,
    user_prompt_chars INTEGER DEFAULT 0,
    tools_provided TEXT[],

    output_text TEXT,
    error_message TEXT,
    error_traceback TEXT,

    delivery_mode TEXT,
    delivery_status TEXT,
    delivered_at TIMESTAMPTZ,
    delivery_channel TEXT,

    token_budget INTEGER DEFAULT 0,
    cost_budget_usd NUMERIC(10, 6) DEFAULT 0,
    budget_exhausted BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_status ON agent_runs(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant ON agent_runs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_correlation ON agent_runs(correlation_id) WHERE correlation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS agent_run_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,

    step_type TEXT NOT NULL CHECK (step_type IN (
        'llm_call', 'tool_call', 'tool_result', 'error',
        'planning', 'verification', 'checkpoint', 'scratchpad',
        'escalation', 'guardrail'
    )),

    tool_name TEXT,
    tool_input JSONB,
    tool_output JSONB,

    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,

    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,

    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_run_steps_run ON agent_run_steps(run_id, step_number);

CREATE TABLE IF NOT EXISTS agent_schedules (
    agent_id TEXT PRIMARY KEY,
    tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    cron_expr TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'America/Grenada',

    timeout_seconds INTEGER NOT NULL DEFAULT 600,

    last_run_at TIMESTAMPTZ,
    last_run_id UUID REFERENCES agent_runs(id),
    last_status TEXT,
    last_duration_ms INTEGER,
    next_run_at TIMESTAMPTZ,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,

    model_primary TEXT,
    model_fallbacks TEXT[],
    delivery_mode TEXT,
    delivery_channel TEXT,
    delivery_to TEXT,
    session_target TEXT,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_schedules_tenant ON agent_schedules(tenant_id);

-- ── Checkpoints: mid-run state snapshots for resume (from migration 014) ─

CREATE TABLE IF NOT EXISTS agent_run_checkpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,
    messages JSONB NOT NULL DEFAULT '[]',
    scratchpad JSONB,
    plan JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_run_checkpoints_run
    ON agent_run_checkpoints(run_id, step_number DESC);

-- ── Guardrail audit trail (from migration 014) ──────────────────────────

CREATE TABLE IF NOT EXISTS agent_guardrail_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,
    guardrail_name TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('blocked', 'warned', 'allowed')),
    tool_name TEXT,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_guardrail_events_run
    ON agent_guardrail_events(run_id);

-- ════════════════════════════════════════════════════════════════════════════
-- WORKFLOW ENGINE (from migration 013)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS workflow_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'robothor-primary',
    workflow_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL DEFAULT 'manual',
    trigger_detail TEXT,
    correlation_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'timeout', 'cancelled')),
    steps_total INTEGER NOT NULL DEFAULT 0,
    steps_completed INTEGER NOT NULL DEFAULT 0,
    steps_failed INTEGER NOT NULL DEFAULT 0,
    steps_skipped INTEGER NOT NULL DEFAULT 0,
    context JSONB DEFAULT '{}',
    error_message TEXT,
    duration_ms INTEGER,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS workflow_run_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    step_type TEXT NOT NULL CHECK (step_type IN ('agent', 'tool', 'condition', 'transform', 'noop')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
    agent_id TEXT,
    agent_run_id UUID,
    tool_name TEXT,
    tool_input JSONB,
    tool_output JSONB,
    condition_branch TEXT,
    output_text TEXT,
    error_message TEXT,
    duration_ms INTEGER,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_status ON workflow_runs(workflow_id, status);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_tenant ON workflow_runs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_created ON workflow_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_correlation ON workflow_runs(correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_run_steps_run ON workflow_run_steps(run_id);

-- ════════════════════════════════════════════════════════════════════════════
-- VAULT (secret store)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS vault_secrets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'robothor-primary',
    key TEXT NOT NULL,
    encrypted_value BYTEA NOT NULL,
    category TEXT DEFAULT 'credential',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, key)
);

CREATE INDEX IF NOT EXISTS idx_vault_tenant_category ON vault_secrets(tenant_id, category);

COMMIT;
