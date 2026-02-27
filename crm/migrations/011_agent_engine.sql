-- Migration 011: Agent Engine Tables
-- Run tracking, step audit trail, and schedule state for the Python agent engine.

BEGIN;

-- ─── agent_runs: One row per execution attempt ───────────────────────

CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id),
    agent_id TEXT NOT NULL,

    trigger_type TEXT NOT NULL CHECK (trigger_type IN (
        'cron', 'hook', 'event', 'manual', 'telegram'
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

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_status
    ON agent_runs(agent_id, status);

CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant
    ON agent_runs(tenant_id);

CREATE INDEX IF NOT EXISTS idx_agent_runs_created
    ON agent_runs(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_runs_correlation
    ON agent_runs(correlation_id)
    WHERE correlation_id IS NOT NULL;

-- ─── agent_run_steps: Append-only audit trail per step ───────────────

CREATE TABLE IF NOT EXISTS agent_run_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_number INTEGER NOT NULL,

    step_type TEXT NOT NULL CHECK (step_type IN (
        'llm_call', 'tool_call', 'tool_result', 'error'
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

CREATE INDEX IF NOT EXISTS idx_agent_run_steps_run
    ON agent_run_steps(run_id, step_number);

-- ─── agent_schedules: Runtime schedule state ─────────────────────────

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

CREATE INDEX IF NOT EXISTS idx_agent_schedules_tenant
    ON agent_schedules(tenant_id);

COMMIT;
