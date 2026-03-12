-- 027_auto_task.sql — Link agent_runs to CRM tasks for auto-task lifecycle
-- Adds task_id FK so every auto-task agent run is tracked in the CRM.

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS task_id UUID REFERENCES crm_tasks(id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_task_id ON agent_runs(task_id) WHERE task_id IS NOT NULL;
