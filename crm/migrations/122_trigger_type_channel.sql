-- 122_trigger_type_channel.sql
--
-- Add 'channel' to agent_runs_trigger_type_check.
--
-- A plugin channel that receives (genus-teams is the first) drives runs, and
-- TriggerType cannot grow a member per installed distribution -- so every
-- plugin channel's inbound run carries the one value 'channel'. Without this
-- the CHECK rejects create_run and the entire run tree, steps and sub-agents
-- included, becomes invisible to accounting: the failure migration 097 was
-- written to end, and which test_schema_drift.py now keeps from recurring.
--
-- Reusing 'slack' or 'webhook' was the alternative and is worse: this column is
-- what the dashboard and the cost breakdown read to say where a run came from,
-- and a Teams message recorded as Slack is a wrong answer stated confidently.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'agent_runs_trigger_type_check'
        AND conrelid = 'agent_runs'::regclass
    ) THEN
        ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_trigger_type_check;
        ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_trigger_type_check
            CHECK (trigger_type IN (
                'cron', 'hook', 'event', 'manual', 'telegram', 'webchat',
                'slack', 'workflow', 'sub_agent', 'federation',
                'webhook', 'ide', 'channel_event', 'channel'
            ));
    END IF;
EXCEPTION WHEN undefined_table THEN
    -- agent_runs may not exist yet on a fresh install with a different order.
    NULL;
END $$;
