-- Migration 021: Deep Reason step type
-- Expands step_type CHECK to include 'deep_reason', 'replan', 'error_recovery'
-- for /deep mode runs and agentic autonomy features.

BEGIN;

ALTER TABLE agent_run_steps DROP CONSTRAINT IF EXISTS agent_run_steps_step_type_check;
ALTER TABLE agent_run_steps ADD CONSTRAINT agent_run_steps_step_type_check
    CHECK (step_type IN (
        'llm_call', 'tool_call', 'tool_result', 'error',
        'planning', 'verification', 'checkpoint', 'scratchpad',
        'escalation', 'guardrail', 'spawn_agent', 'plan_proposal',
        'replan', 'error_recovery', 'deep_reason'
    ));

COMMIT;
