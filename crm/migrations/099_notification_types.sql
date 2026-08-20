-- 099_notification_types.sql
--
-- Recreate crm_agent_notifications_notification_type_check with every
-- notification_type the engine writes.
--
-- 'alert_digest' and 'alert_fallback' (severity-routed alerts, engine
-- alerts.py) and 'workflow_failure' (workflow.py failure paging) were added
-- to the Python side after migration 031 last rebuilt this constraint, so
-- those INSERTs failed the CHECK and the notifications were silently lost.
-- The drift test robothor/engine/tests/test_schema_drift.py keeps this
-- constraint and the engine's write sites in lockstep from now on.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'crm_agent_notifications_notification_type_check'
        AND conrelid = 'crm_agent_notifications'::regclass
    ) THEN
        ALTER TABLE crm_agent_notifications
            DROP CONSTRAINT crm_agent_notifications_notification_type_check;
        ALTER TABLE crm_agent_notifications
            ADD CONSTRAINT crm_agent_notifications_notification_type_check
            CHECK (notification_type IN (
                'task_assigned', 'review_requested', 'review_approved',
                'review_rejected', 'blocked', 'unblocked',
                'agent_error', 'info', 'custom', 'escalation',
                'alert_digest', 'alert_fallback', 'workflow_failure'
            ));
    END IF;
EXCEPTION WHEN undefined_table THEN
    -- crm_agent_notifications may not exist on fresh installs with a
    -- different migration order
    NULL;
END $$;
