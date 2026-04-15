-- Migration 031: Agent reviews — formal feedback storage
-- Supports operator reviews, automated system reviews (buddy, auto-agent),
-- and agent-to-agent reviews. Feeds into Buddy scoring effectiveness dimension.

BEGIN;

CREATE TABLE IF NOT EXISTS agent_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL DEFAULT 'robothor-primary',
    agent_id TEXT NOT NULL,
    run_id UUID,
    reviewer TEXT NOT NULL,
    reviewer_type TEXT NOT NULL
        CHECK (reviewer_type IN ('operator', 'agent', 'system')),
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    categories JSONB,
    feedback TEXT,
    action_items TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reviews_agent
    ON agent_reviews (agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reviews_tenant
    ON agent_reviews (tenant_id, agent_id);

COMMIT;
