-- 029: Add cache token tracking columns to agent_runs and agent_run_steps
-- Supports Anthropic prompt caching visibility and accurate cost tracking

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS cache_creation_tokens INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER DEFAULT 0;

ALTER TABLE agent_run_steps
    ADD COLUMN IF NOT EXISTS cache_creation_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER;
