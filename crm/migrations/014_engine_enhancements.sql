-- Migration 014: Agent Engine v2 Enhancements
-- Adds budget tracking, checkpoint persistence, and guardrail audit trail.

BEGIN;

-- ─── Expand step_type CHECK for new step types ──────────────────────

ALTER TABLE agent_run_steps DROP CONSTRAINT IF EXISTS agent_run_steps_step_type_check;
ALTER TABLE agent_run_steps ADD CONSTRAINT agent_run_steps_step_type_check
    CHECK (step_type IN (
        'llm_call', 'tool_call', 'tool_result', 'error',
        'planning', 'verification', 'checkpoint', 'scratchpad',
        'escalation', 'guardrail'
    ));

-- ─── Budget columns on agent_runs ───────────────────────────────────

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS token_budget INTEGER DEFAULT 0;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS cost_budget_usd NUMERIC(10, 6) DEFAULT 0;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS budget_exhausted BOOLEAN DEFAULT FALSE;

-- ─── Expand trigger_type CHECK for webchat + workflow ───────────────

ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_trigger_type_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_trigger_type_check
    CHECK (trigger_type IN (
        'cron', 'hook', 'event', 'manual', 'telegram', 'webchat', 'workflow'
    ));

-- ─── Checkpoints: mid-run state snapshots for resume ────────────────

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

-- ─── Guardrail audit trail ──────────────────────────────────────────

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

COMMIT;
