-- 097_trigger_type_channel_event.sql
--
-- Recreate agent_runs_trigger_type_check with every TriggerType value from
-- robothor/engine/models.py.
--
-- 'channel_event' (channel-bus wake of main), 'slack', 'webhook', and 'ide'
-- were added to the Python enum after migration 025 last rebuilt this
-- constraint, so runs with those trigger types failed the CHECK, create_run
-- was rejected, and the whole run tree (steps, sub-agents) became invisible
-- to accounting. The drift test robothor/engine/tests/test_schema_drift.py
-- keeps this constraint and the enum in lockstep from now on.

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
                'webhook', 'ide', 'channel_event'
            ));
    END IF;
EXCEPTION WHEN undefined_table THEN
    -- agent_runs table may not exist on fresh installs with different migration order
    NULL;
END $$;
