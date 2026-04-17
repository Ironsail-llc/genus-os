-- Migration 044: Cross-request breadcrumbs (7-day agent scratchpad)
--
-- An agent can persist mid-task state ("started investigation X, found Y,
-- next step Z") that survives across runs. The runner loads the latest
-- breadcrumbs for the agent into warmup context at run start. Default TTL
-- is 7 days; pruning is part of nightly lifecycle.

BEGIN;

CREATE TABLE IF NOT EXISTS agent_breadcrumbs (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default'
        REFERENCES crm_tenants(id),
    agent_id TEXT NOT NULL,
    run_id TEXT,
    content JSONB NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '7 days',
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_breadcrumbs_tenant_agent
    ON agent_breadcrumbs(tenant_id, agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_breadcrumbs_expiry
    ON agent_breadcrumbs(expires_at);

COMMIT;
