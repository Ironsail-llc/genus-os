-- Migration 031: Add 'escalation' notification type
-- The email-classifier uses notificationType="escalation" but the CHECK
-- constraint didn't include it, causing silent notification failures.

BEGIN;

ALTER TABLE crm_agent_notifications
    DROP CONSTRAINT IF EXISTS crm_agent_notifications_notification_type_check;

ALTER TABLE crm_agent_notifications
    ADD CONSTRAINT crm_agent_notifications_notification_type_check
    CHECK (notification_type IN (
        'task_assigned', 'review_requested', 'review_approved',
        'review_rejected', 'blocked', 'unblocked',
        'agent_error', 'info', 'custom', 'escalation'
    ));

COMMIT;
