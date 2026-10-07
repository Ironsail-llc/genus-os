-- Migration 144: coding_jobs — durable Claude Code jobs (robothor/engine/coding/jobs.py).
--
-- One row per job an agent started with claude_code_start. The engine owns the
-- job as an asyncio task; this row is what survives a restart: on startup every
-- row still 'queued' or 'running' is resumed (with `claude --resume
-- <session_id>` when Claude Code had announced its session). Evidence a 'done'
-- job carries — verify exit code, output sha256, commit sha — lives in
-- result->'evidence' in the shape robothor/engine/session_goal.py validates.
CREATE TABLE IF NOT EXISTS coding_jobs (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    goal_id TEXT,
    task_id TEXT,
    repo_path TEXT NOT NULL,
    worktree_path TEXT NOT NULL DEFAULT '',
    branch TEXT,
    base_ref TEXT NOT NULL DEFAULT 'HEAD',
    base_sha TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL CHECK (mode IN ('code', 'review', 'readonly')),
    model TEXT,
    task TEXT NOT NULL,
    acceptance JSONB NOT NULL DEFAULT '{}'::jsonb,
    json_schema JSONB,
    max_rounds INTEGER NOT NULL DEFAULT 3,
    max_budget_usd NUMERIC(10, 4),
    grant_github BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    session_id TEXT,
    rounds INTEGER NOT NULL DEFAULT 0,
    cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    events_tail JSONB NOT NULL DEFAULT '[]'::jsonb,
    pending_followups JSONB NOT NULL DEFAULT '[]'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

-- Startup resume reads the unfinished rows; status/wait read by (tenant, id).
CREATE INDEX IF NOT EXISTS coding_jobs_unfinished
    ON coding_jobs (tenant_id, created_at)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS coding_jobs_agent
    ON coding_jobs (tenant_id, agent_id, created_at DESC);

ALTER TABLE coding_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE coding_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON coding_jobs;
CREATE POLICY tenant_isolation ON coding_jobs
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- Rollback:
--   DROP TABLE IF EXISTS coding_jobs;
