-- 034: Add outcome assessment columns to agent_runs
-- Tracks semantic satisfaction beyond pass/fail status for interactive runs.
-- Used by the learning loop (Agent Architect, AutoAgent) to optimize agents.

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS outcome_assessment TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS outcome_notes TEXT;

-- Index for analytics queries filtering by outcome
CREATE INDEX IF NOT EXISTS idx_agent_runs_outcome
    ON agent_runs (outcome_assessment)
    WHERE outcome_assessment IS NOT NULL;
