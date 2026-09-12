-- 116_benchmark_digest_notification_type.sql
--
-- Add 'benchmark_digest' to crm_agent_notifications_notification_type_check.
--
-- Alerts raised inside a benchmark child run are no longer written as
-- 'alert_digest': the heartbeat's alert reader selects by type
-- (robothor/engine/warmup.py::ALERT_DIGEST_TYPES) and 135 digest rows from
-- graded runs in fourteen hours turned the operator's first message of the day
-- into a triage of alerts about agents that were being tested, not failing.
--
-- The rows are still written, in their own type, so a suite that trips the
-- runaway-token guard every night stays a visible finding about the suite.
--
-- Rebuilds the whole constraint rather than appending, the same way 099 did:
-- a CHECK cannot be extended in place. Keep this list in step with
-- robothor/engine/tests/test_schema_drift.py, which fails when the engine
-- writes a type the newest definition of this constraint does not allow.

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
                'alert_digest', 'alert_fallback', 'workflow_failure',
                'benchmark_digest'
            ));
    END IF;
EXCEPTION WHEN undefined_table THEN
    -- crm_agent_notifications may not exist on fresh installs with a
    -- different migration order
    NULL;
END $$;
