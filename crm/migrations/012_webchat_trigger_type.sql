-- Add 'webchat' to agent_runs trigger_type check constraint
-- Supports the Helm chat endpoints (POST /chat/send)

ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_trigger_type_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_trigger_type_check
  CHECK (trigger_type = ANY (ARRAY['cron', 'hook', 'event', 'manual', 'telegram', 'webchat']));
