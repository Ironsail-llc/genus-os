-- Managed Agents integration — new tables only.
-- Does NOT modify any existing tables.
BEGIN;

-- Cache mapping: tenant+resource → Managed Agents API resource ID.
-- Prevents redundant agent/environment/memory-store creation on every run.
CREATE TABLE IF NOT EXISTS ma_tenant_resources (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL REFERENCES crm_tenants(id),
    resource_type   TEXT NOT NULL,      -- 'agent', 'environment', 'memory_store'
    resource_name   TEXT NOT NULL,      -- our local identifier (e.g. agent_id or env name)
    ma_resource_id  TEXT NOT NULL,      -- Managed Agents API resource ID
    ma_version      INTEGER DEFAULT 1,  -- agent version (for cache invalidation)
    ma_config       JSONB DEFAULT '{}', -- snapshot of config used to create the resource
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(tenant_id, resource_type, resource_name)
);

CREATE INDEX IF NOT EXISTS idx_ma_tenant_resources_lookup
    ON ma_tenant_resources(tenant_id, resource_type);

-- Run history for Managed Agents sessions (separate from agent_runs).
CREATE TABLE IF NOT EXISTS ma_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL DEFAULT 'robothor-primary',
    agent_id        TEXT NOT NULL,
    ma_session_id   TEXT NOT NULL,
    input_message   TEXT,
    output_text     TEXT,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    total_cost_usd  NUMERIC(10, 6) DEFAULT 0,
    tool_calls      JSONB DEFAULT '[]',
    outcome_result  TEXT,               -- 'satisfied', 'needs_revision', 'max_iterations_reached', 'failed'
    duration_ms     INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ma_runs_agent   ON ma_runs(agent_id);
CREATE INDEX IF NOT EXISTS idx_ma_runs_tenant  ON ma_runs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ma_runs_session ON ma_runs(ma_session_id);
CREATE INDEX IF NOT EXISTS idx_ma_runs_created ON ma_runs(created_at DESC);

COMMIT;
