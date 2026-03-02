-- Migration 020: Plan Mode
-- Adds plan_state column to chat_sessions and expands step_type CHECK.

BEGIN;

-- ─── Plan state on chat sessions ─────────────────────────────────────
-- Stores the active PlanState as JSONB so it persists across restarts.

ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS plan_state JSONB;

-- ─── Expand step_type CHECK to include plan_proposal ─────────────────

ALTER TABLE agent_run_steps DROP CONSTRAINT IF EXISTS agent_run_steps_step_type_check;
ALTER TABLE agent_run_steps ADD CONSTRAINT agent_run_steps_step_type_check
    CHECK (step_type IN (
        'llm_call', 'tool_call', 'tool_result', 'error',
        'planning', 'verification', 'checkpoint', 'scratchpad',
        'escalation', 'guardrail', 'spawn_agent', 'plan_proposal'
    ));

COMMIT;
