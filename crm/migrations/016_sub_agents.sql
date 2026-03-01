-- 016_sub_agents.sql
-- Add sub-agent (nested agent spawning) support to agent_runs.
--
-- New columns:
--   parent_run_id  — links child runs to their parent
--   nesting_depth  — 0 for top-level, increments per spawn level
--
-- Expands trigger_type CHECK to include 'sub_agent'.
-- Expands step_type CHECK to include 'spawn_agent'.

BEGIN;

-- Add parent tracking columns
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS parent_run_id UUID REFERENCES agent_runs(id) ON DELETE SET NULL;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS nesting_depth INTEGER DEFAULT 0;

-- Index for finding children of a run
CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_run_id) WHERE parent_run_id IS NOT NULL;

-- Expand trigger_type CHECK to include sub_agent
ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_trigger_type_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_trigger_type_check
    CHECK (trigger_type IN ('cron', 'hook', 'event', 'manual', 'telegram', 'webchat', 'workflow', 'sub_agent'));

-- Expand step_type CHECK to include spawn_agent
ALTER TABLE agent_run_steps DROP CONSTRAINT IF EXISTS agent_run_steps_step_type_check;
ALTER TABLE agent_run_steps ADD CONSTRAINT agent_run_steps_step_type_check
    CHECK (step_type IN (
        'llm_call', 'tool_call', 'tool_result', 'error',
        'planning', 'verification', 'checkpoint', 'scratchpad',
        'escalation', 'guardrail', 'spawn_agent'
    ));

COMMIT;
