-- Migration 033: Memory System Multi-Tenancy
-- Adds tenant_id to all memory tables (memory_facts, memory_entities,
-- memory_relations, memory_insights, agent_memory_blocks, contact_identifiers,
-- ingested_items, ingestion_watermarks).  Creates tenant_users table for
-- per-user tenant routing (Telegram → tenant_id).
--
-- All existing data is backfilled to 'robothor-primary' via DEFAULT.
-- UNIQUE constraints are widened to include tenant_id.

BEGIN;

-- ═══════════════════════════════════════════════════════════════════════════
-- 1. memory_facts
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'robothor-primary'
    REFERENCES crm_tenants(id);

CREATE INDEX IF NOT EXISTS idx_facts_tenant
    ON memory_facts(tenant_id) WHERE is_active = TRUE;

-- ═══════════════════════════════════════════════════════════════════════════
-- 2. memory_entities
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE memory_entities
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'robothor-primary'
    REFERENCES crm_tenants(id);

-- Widen UNIQUE(name, entity_type) → UNIQUE(tenant_id, name, entity_type)
ALTER TABLE memory_entities
    DROP CONSTRAINT IF EXISTS memory_entities_name_entity_type_key;
ALTER TABLE memory_entities
    ADD CONSTRAINT memory_entities_tenant_name_type_key
    UNIQUE(tenant_id, name, entity_type);

CREATE INDEX IF NOT EXISTS idx_entities_tenant
    ON memory_entities(tenant_id);

-- ═══════════════════════════════════════════════════════════════════════════
-- 3. memory_relations
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE memory_relations
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'robothor-primary'
    REFERENCES crm_tenants(id);

-- Widen UNIQUE(source, target, type) → UNIQUE(tenant_id, source, target, type)
ALTER TABLE memory_relations
    DROP CONSTRAINT IF EXISTS memory_relations_source_entity_id_target_entity_id_relation_key;
ALTER TABLE memory_relations
    ADD CONSTRAINT memory_relations_tenant_src_tgt_rel_key
    UNIQUE(tenant_id, source_entity_id, target_entity_id, relation_type);

-- ═══════════════════════════════════════════════════════════════════════════
-- 4. memory_insights
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE memory_insights
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'robothor-primary'
    REFERENCES crm_tenants(id);

CREATE INDEX IF NOT EXISTS idx_insights_tenant
    ON memory_insights(tenant_id) WHERE is_active = TRUE;

-- ═══════════════════════════════════════════════════════════════════════════
-- 5. agent_memory_blocks
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE agent_memory_blocks
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'robothor-primary'
    REFERENCES crm_tenants(id);

-- Widen UNIQUE(block_name) → UNIQUE(tenant_id, block_name)
ALTER TABLE agent_memory_blocks
    DROP CONSTRAINT IF EXISTS agent_memory_blocks_block_name_key;
ALTER TABLE agent_memory_blocks
    ADD CONSTRAINT agent_memory_blocks_tenant_block_key
    UNIQUE(tenant_id, block_name);

-- ═══════════════════════════════════════════════════════════════════════════
-- 6. contact_identifiers
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE contact_identifiers
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'robothor-primary'
    REFERENCES crm_tenants(id);

-- Widen UNIQUE(channel, identifier) → UNIQUE(tenant_id, channel, identifier)
ALTER TABLE contact_identifiers
    DROP CONSTRAINT IF EXISTS contact_identifiers_channel_identifier_key;
ALTER TABLE contact_identifiers
    ADD CONSTRAINT contact_identifiers_tenant_channel_id_key
    UNIQUE(tenant_id, channel, identifier);

-- ═══════════════════════════════════════════════════════════════════════════
-- 7. ingested_items
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE ingested_items
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'robothor-primary'
    REFERENCES crm_tenants(id);

-- Widen UNIQUE(source_name, item_id) → UNIQUE(tenant_id, source_name, item_id)
ALTER TABLE ingested_items
    DROP CONSTRAINT IF EXISTS ingested_items_source_name_item_id_key;
ALTER TABLE ingested_items
    ADD CONSTRAINT ingested_items_tenant_source_item_key
    UNIQUE(tenant_id, source_name, item_id);

-- ═══════════════════════════════════════════════════════════════════════════
-- 8. ingestion_watermarks
-- ═══════════════════════════════════════════════════════════════════════════

-- PK changes from (source_name) → (tenant_id, source_name)
ALTER TABLE ingestion_watermarks
    DROP CONSTRAINT IF EXISTS ingestion_watermarks_pkey;

ALTER TABLE ingestion_watermarks
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'robothor-primary'
    REFERENCES crm_tenants(id);

ALTER TABLE ingestion_watermarks
    ADD PRIMARY KEY (tenant_id, source_name);

-- ═══════════════════════════════════════════════════════════════════════════
-- 9. tenant_users — per-user tenant routing
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS tenant_users (
    id SERIAL PRIMARY KEY,
    telegram_user_id TEXT,
    telegram_username TEXT,
    display_name TEXT NOT NULL,
    tenant_id TEXT NOT NULL REFERENCES crm_tenants(id),
    role TEXT NOT NULL DEFAULT 'user',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(telegram_user_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_users_tenant ON tenant_users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenant_users_active ON tenant_users(is_active) WHERE is_active = TRUE;

COMMIT;
