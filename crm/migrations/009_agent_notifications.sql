-- Migration 009: Agent-to-Agent Notifications
-- Durable, typed notification system replacing fragile status files.

BEGIN;

CREATE TABLE IF NOT EXISTS crm_agent_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT DEFAULT 'robothor-primary' REFERENCES crm_tenants(id),
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    notification_type TEXT NOT NULL CHECK (notification_type IN (
        'task_assigned', 'review_requested', 'review_approved',
        'review_rejected', 'blocked', 'unblocked',
        'agent_error', 'info', 'custom', 'escalation'
    )),
    subject TEXT NOT NULL,
    body TEXT,
    metadata JSONB DEFAULT '{}',
    task_id UUID REFERENCES crm_tasks(id),
    read_at TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notifications_inbox
    ON crm_agent_notifications(to_agent, read_at NULLS FIRST)
    WHERE acknowledged_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_notifications_task
    ON crm_agent_notifications(task_id)
    WHERE task_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_notifications_tenant
    ON crm_agent_notifications(tenant_id);

CREATE INDEX IF NOT EXISTS idx_notifications_type
    ON crm_agent_notifications(notification_type, created_at DESC);

COMMIT;
