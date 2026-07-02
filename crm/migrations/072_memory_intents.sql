-- Migration 072: Prospective / Intent memory
--
-- Everything else in the memory system is retrospective (what happened).
-- memory_intents models what the operator is *working toward* and persists
-- it across sessions, so the main agent's heartbeat can proactively advance
-- standing objectives.
--
-- This is parallel to session_goal (crm_tasks + session_goal_meta), which is
-- per-run and engineering-evidence-gated. Intents are longer-lived business
-- objectives. `stated` intents are active immediately; `inferred` intents
-- (proposed by the nightly LLM pass) start as 'proposed' and only become
-- active after HMAC-gated confirmation.
--
-- Gated at the application layer by ROBOTHOR_RIP_14_ENABLED.

BEGIN;

CREATE TABLE IF NOT EXISTS memory_intents (
    id               BIGSERIAL PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    horizon          TEXT NOT NULL DEFAULT 'ongoing',  -- ongoing | this_quarter | this_week | dated
    due_at           TIMESTAMPTZ,
    status           TEXT NOT NULL DEFAULT 'active',    -- proposed | active | dormant | achieved | dropped
    priority         SMALLINT NOT NULL DEFAULT 3,       -- 1 (high) .. 5 (low)
    source           TEXT NOT NULL DEFAULT 'stated',    -- stated | inferred
    confidence       FLOAT8 NOT NULL DEFAULT 0.5,
    embedding        vector(1024),
    linked_goal_ids  INTEGER[] NOT NULL DEFAULT '{}',
    linked_fact_ids  INTEGER[] NOT NULL DEFAULT '{}',
    last_advanced_at TIMESTAMPTZ,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT memory_intents_status CHECK
        (status IN ('proposed', 'active', 'dormant', 'achieved', 'dropped')),
    CONSTRAINT memory_intents_horizon CHECK
        (horizon IN ('ongoing', 'this_quarter', 'this_week', 'dated')),
    CONSTRAINT memory_intents_source CHECK (source IN ('stated', 'inferred'))
);

CREATE INDEX IF NOT EXISTS idx_intents_active
    ON memory_intents (tenant_id, priority, last_advanced_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_intents_status
    ON memory_intents (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_intents_emb
    ON memory_intents USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);
-- Dedup by title within a tenant so re-stating an intent upserts.
CREATE UNIQUE INDEX IF NOT EXISTS idx_intents_dedup
    ON memory_intents (tenant_id, md5(title));

COMMIT;
